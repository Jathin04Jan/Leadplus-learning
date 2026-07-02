# LeadPlus — Architecture Course

A step-by-step course to fully understand the **LeadPlus** platform before working on it.
Written for a software architect / developer joining the project. Each **Step** is taught in
depth; read it, ask follow-ups, then advance to the next.

> **Continuity:** if you're resuming (or a fresh AI session is picking this up), read
> [`CONTEXT.md`](./CONTEXT.md) first — it records where we are and how to continue.

**How to read locally:** `less README.md` (arrows / PgUp / PgDn to scroll, `/word` to search,
`q` to quit; `G` = bottom, `g` = top). On GitHub it renders as this page.

---

## Roadmap

1. **Big picture** — domain, deployables, tech stack, architectural style, an end-to-end request trace ✅
2. **Modular-monolith architecture** — the 11 modules, the `*Module` facade pattern, the boundary rule & enforcement, events, cycle resolution
3. **Identity & multi-tenancy** — `auth` (JWT/login/signup/OAuth) + `workspace` (tenant→workspace→user); how a request is authenticated and scoped
4. **The LeadGen engine** — `search → campaign → outreach → tracking` (leads, sequences, the send cron, reply/bounce/unsubscribe)
5. **The RFQ marketplace** — `buyer / vendor / rfq` (onboarding, RFQ→quotation lifecycle, collaborators)
6. **The AI module** — how all AI funnels through `AIServicesModule`, prompts, chat memory, the Python-extraction seam
7. **Cross-cutting** — security filter chains, exceptions, timezones, events, schedulers, S3/email infra
8. **The frontend** — Next.js structure, token/auth flow, module gating, the data-fetching pattern
9. **Build / run / deploy** — Gradle, the `schema.sql` + `validate` model, CI/CD (jib→ECR→ECS), config & secrets
10. **Migration context** — the modular-monolith refactor, phases, Java-now/Python-later, known issues

*(✅ = written below)*

---

# Step 1 — The big picture

## What the product is
**LeadPlus** is a multi-tenant B2B SaaS for manufacturing/GTM, with **two halves that share one backend**:

- **LeadGen engine** — a sales/GTM machine: find companies & contacts (Apollo-backed), save them into lists, generate multi-step email campaigns (AI-assisted), send them through the customer's own mailbox (Gmail/Outlook/etc.), and track replies/bounces/unsubscribes.
- **RFQ marketplace** — a procurement side: vendors onboard & get approved; buyers search vendors and raise **RFQs** (requests for quote); vendors respond with **quotations**; collaborators can be invited onto a request.

One company = one **tenant**, and the `tenant.modules` field decides which half (or both) that tenant sees.

## The three deployables
```
┌─────────────────────────┐        ┌──────────────────────────────┐
│  leadplus-portal         │ HTTPS  │  leadplus-service            │
│  Next.js 16 / React 19   │───────▶│  Java 21 / Spring Boot 3.5   │
│  static export → S3/CDN  │  /api  │  the modular monolith        │
└─────────────────────────┘        └───────────────┬──────────────┘
                                                    │ JPA
                                            ┌───────▼────────┐
                                            │  PostgreSQL     │  (64 tables, no FKs)
                                            └────────────────┘
   (leadplus-intelligence-service: a small Node/Mongo enrichment helper,
    still frozen in Limark/, slated to fold into a future Python AI service)
```

- **`leadplus-service`** (the one that matters most) — a **modular monolith**: a single Spring Boot deployable internally split into 11 strictly-bounded modules. The heart of the system.
- **`leadplus-portal`** — Next.js, compiled to a **static export** (pure HTML/JS on S3+CloudFront). No server of its own — all logic is client-side, all data comes from the backend over `NEXT_PUBLIC_API_URL` + `/api`.
- **Postgres** — the single operational DB. Notably **no foreign-key constraints** (legacy of an unfinished Mongo→Postgres migration); relationships are by `*_id` convention only.

## Tech stack (backend)
- Java 21, Spring Boot 3.5, Spring Data JPA, Spring Security (stateless JWT), Spring AI (OpenAI + now Anthropic).
- Build: **Gradle** (not Maven). Containerized via **jib** (no Dockerfile). Deployed to **AWS ECS**.
- Schema is **externally managed**: Hibernate runs `ddl-auto: validate` against `schema.sql` — the app does **not** create its own tables (this trips everyone up at first).
- Integrations: Gmail/Google OAuth, Microsoft Graph/Outlook, AWS SES + S3, Mailgun, Apollo (lead data), Zoho/HubSpot (CRM sync).

## The architectural style — "modular monolith"
One deployable, but internally carved into 11 modules laid out as `<module>/{controller,service,repository,model}/`. The rule that makes it "modular" rather than a big ball of mud:

> **A module may never import another module's `Service`, `Repository`, `Client`, or `Entity`. Cross-module calls go only through the target module's single public `<Name>Module` facade class** (or by listening to its published events).

So `outreach` doesn't reach into `campaign`'s repositories — it calls `CampaignModule.getTopCampaignContactToMail()`. This is **enforced by a build test** (`ModuleBoundariesTest`) that fails CI on any violation. The point: get the boundaries of a future microservices/Python split *right now*, while still shipping a single easy-to-run monolith. (The AI module is the first planned extraction into a separate Python service.)

The 11 modules in three areas:
```
Portal:   auth · portal/buyer · portal/vendor · portal/rfq
LeadGen:  leadgen/search → leadgen/campaign → leadgen/outreach → leadgen/tracking
Shared:   shared/workspace · shared/admin · shared/ai
```

## A concrete end-to-end trace — "a campaign email gets sent"
Keep this as your mental skeleton; it touches most of the system:

1. A **cron** in `outreach` (`CampaignOrchestratorService`, `@Scheduled`, ~every minute on weekdays) wakes up.
2. It asks **campaign**: `campaignModule.getTopCampaignContactToMail()` — the next contact due for an email.
3. It runs a gauntlet of guards, each crossing a module boundary via a facade:
   - **tracking**: `validateContactEligibility()` — not unsubscribed/bounced?
   - **workspace**: mailbox config + `dailySendLimit` — under quota, token not expired?
   - **campaign**: `nextValidSendTime()` — inside the campaign's (timezone-aware) sending window?
4. If all pass, **campaign** renders the email (`renderCampaignEmail` — mail-merge + the configurable mail-guard redirect), and **outreach** sends it via the tenant's provider (Gmail/Outlook/SES/SMTP/Mailgun), stamping an unsubscribe link built from `client.url`.
5. **outreach** publishes a `CampaignEmailSentEvent` (IDs only). Three modules listen: **tracking** (records status), **workspace** (increments the mailbox daily counter), **search** (writes a lead-timeline row).
6. Later, a **tracking** reply-sync cron polls Gmail/Outlook, detects a reply, flips the contact to `REPLIED`, and publishes `ReplyReceivedEvent`.

The recurring pattern: **modules never touch each other's internals — they call facades and fire events.** That single idea explains ~80% of the codebase's structure.

## Two takeaways from Step 1
1. It's **one Spring Boot app split into 11 strictly-bounded modules**.
2. The **facade (`*Module`) + events** pattern is how modules talk.
