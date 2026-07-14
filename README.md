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
2. **Modular-monolith architecture** — the 11 modules, the `*Module` facade pattern, the boundary rule & enforcement, events, cycle resolution ✅
3. **Identity & multi-tenancy** — `auth` (JWT/login/signup/OAuth) + `workspace` (tenant→workspace→user); how a request is authenticated and scoped ✅
4. **The LeadGen engine** — `search → campaign → outreach → tracking` (leads, sequences, the send cron, reply/bounce/unsubscribe) ✅
5. **The RFQ marketplace** — `buyer / vendor / rfq` (onboarding, RFQ→quotation lifecycle, collaborators) ✅
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

---

# Step 2 — The modular-monolith architecture

This is the backbone of the whole system. If you understand this step deeply, the rest of the
codebase reads itself. The goal of the design: **get the module boundaries of a future
microservices/Python split right *now*, while still shipping one easy-to-run Spring Boot app.**

## 2.1 The physical layout (a module on disk)
Every module lives under `src/main/java/ai/leadplus/<module>/` (package root is `ai.leadplus`, **not** `com.leadplus`) and always has the same shape:

```
leadgen/campaign/
├── CampaignModule.java        ← the ONE public class other modules may import (the facade)
├── CONTEXT.md                 ← the module's contract: Owns (tables), Public interface, Business rules
├── controller/                ← REST endpoints (@RestController)
├── service/                   ← business logic (@Service) + the module's DTOs/enums/events
├── repository/                ← Spring Data JPA interfaces (@Repository)
└── model/                     ← JPA @Entity classes (the tables this module OWNS)
```

Two files matter most when you start on a module:
- **`CONTEXT.md`** — read it *first*. It states which **tables the module owns**, its **public interface**, and its **business rules**. (⚠️ these are partly stale — some say the `*Module` is "Planned"; it isn't. Trust the code — see ISSUES.md X2.)
- **`<Name>Module.java`** — the module's single public door.

## 2.2 The rule (the single most important thing in this repo)

> **A module may NEVER import another module's `Service`, `Repository`, `Client`, or `Entity`.**
> Cross-module access happens only through the target module's public **`<Name>Module`** facade,
> or by listening to an **event** it publishes.

What a module **is** allowed to import from another module (the "module-API surface"):
1. The other module's **`<Name>Module`** class.
2. The **DTOs / models / enums / events** that the facade's methods accept or return (these are the contract types — mostly in the other module's `service/` package).
3. A small **shared allowlist**: `application.{common,exception}`, `domain.common`, `api.common.datetime`, `api.configurations` (bootstrap), and the validators in `shared/workspace/controller/common`.

Everything else across a module boundary is forbidden. The intuition: a module's `model/`
(entities) and `repository/` (tables) are its **private data**; nobody reaches into another
module's database — they *ask* via the facade.

## 2.3 The facade in practice
Here's a **real** example — the outreach send-cron. It needs data from campaign, tracking, and
workspace. Look at what it imports:

```java
// leadgen/outreach/service/CampaignOrchestratorService.java
import ai.leadplus.leadgen.campaign.CampaignModule;          // ✅ facade
import ai.leadplus.leadgen.tracking.TrackingModule;          // ✅ facade
import ai.leadplus.shared.workspace.WorkspaceModule;         // ✅ facade
import ai.leadplus.leadgen.campaign.service.CampaignDto;     // ✅ a DTO the facade returns
import ai.leadplus.shared.workspace.service.MailboxDto;      // ✅ a DTO the facade returns
// NOT allowed (and absent): campaign.repository.CampaignRepository, campaign.model.Campaign, …
```

And the calls are all through the facade:
```java
campaignModule.getTopCampaignContactToMail();      // instead of touching CampaignRepository
trackingModule.validateContactEligibility(...);    // instead of touching ContactOutreachStatusRepository
workspaceModule.getMailbox(mailboxId);             // instead of touching MailboxRepository
```

**What a `*Module` class actually is:** a thin `@Component` that *delegates* to the module's own
internal services and maps internal entities → public DTOs. It adds no logic; it's a **published
contract**. Example shape:

```java
@Component
@RequiredArgsConstructor
public class CampaignModule {                    // the ONLY class outsiders import
    private final CampaignContactService campaignContactService;   // internal, private
    public Optional<CampaignContactInfoDto> getTopCampaignContactToMail() {
        return campaignContactService.getTopContactToMail();       // delegate
    }
    // ... ~20-60 such methods per module
}
```

`WorkspaceModule` is the biggest (~60 methods, the multi-tenancy foundation); `portal/buyer`
owns no tables and has no facade yet (it's a thin orchestration layer over other facades).

## 2.4 Events — the *other* cross-module channel
Facades are for **"I need data now" (synchronous pull)**. Events are for **"something happened,
whoever cares can react" (asynchronous, decoupled push)**. Used when a hard dependency would
create a cycle or unwanted coupling.

Rules for events here:
- **Payloads carry IDs + at most cheap primitives — never DTOs.** Listeners re-fetch fresh state
  via the owning module's facade. (This keeps modules from coupling on each other's data shapes.)
- Publish with Spring's `ApplicationEventPublisher`; consume with `@EventListener` (often `@Async`).

Real example — one publish, three independent reactions (fully decoupled):
```
outreach: publishes CampaignEmailSentEvent(contactId, campaignId, stepNumber, …)   // IDs only
   ├─ tracking  @EventListener → stamp last-email time / status
   ├─ workspace @EventListener → increment the mailbox's emailsSentToday counter
   └─ search    @EventListener → write a "campaign email sent" row on the lead timeline
```
The outreach module has **no idea** those three listeners exist — that's the point.
(Scale in the repo: **27** `*Event` types, **11** classes with `@EventListener`.)

The classic use is breaking a would-be cycle: **campaign ↔ tracking**. A reply detected in
tracking must flip a campaign contact to `REPLIED`. Instead of tracking depending on campaign at
construction time, tracking **publishes `ReplyReceivedEvent`** and campaign **listens**. Dependency
inverted, no cycle.

## 2.5 How the boundary is *enforced* — `ModuleBoundariesTest`
This is not a convention people remember to follow — it's a **build-failing test**. Understand
exactly how it works, including its blind spot:

`src/test/java/ai/leadplus/ModuleBoundariesTest.java` is a **source-scanning regex test** (not
ArchUnit, not bytecode). For each module it:
1. Walks every `.java` file under that module.
2. Regex-matches `import ai.leadplus.…;` lines.
3. Flags an import as a **violation** only if ALL of:
   - it belongs to a *different* module (the shared allowlist isn't in the module map, so it passes),
   - **its simple name ends in `Service`, `Repository`, or `Client`** (`INTERNAL` pattern),
   - it isn't one of the documented exemptions.
4. Asserts zero violations. There's a `@Test` for **all 11 modules** (docs saying "3–5 strict" are stale — ISSUES.md X3).

**Exemptions (6, encoded in the test):** 4 shared OAuth/label adapter clients in workspace that
outreach's send paths use, + 2 pre-existing `SpringAiClient` bypass files in campaign
(`CampaignEmailAiService`, `ContactEmailAiService` — see ISSUES.md A1).

**⚠️ The blind spot (architect, note this):** the check is a **suffix heuristic**, not a true
architecture gate. Because it only flags `*Service`/`*Repository`/`*Client`, these slip through
undetected even though the *rule* forbids them:
- cross-module **`Entity`** imports (the rule says no, the test doesn't catch it),
- controller-layer classes like another module's `VendorValidator` (rfq controllers actually import
  vendor's `VendorValidator` — a real leak the test misses),
- `*Util` helper classes from another module.
So "green boundary test" ≠ "clean boundaries." (Logged as an enforcement gap — ISSUES.md A4-adjacent.)

## 2.6 Dependency cycles & the `@Lazy` resolution
Because facades call each other, you can get **construction cycles** (Spring can't build bean A if
it needs B which needs A). Four were resolved up front, by choosing a **leaf** or inverting to events:
- `auth ↔ workspace` → **auth is the leaf** (identity only; tenant resolution lives in workspace).
- `buyer ↔ rfq` and `vendor ↔ rfq` → **rfq is the leaf** (buyer/vendor context passed *in* as parameters).
- `campaign ↔ tracking` → **event-based** (tracking publishes, campaign listens).

For the remaining back-edges, they break the *constructor* cycle with **`@Lazy`** on the injected
facade field — Spring injects a proxy and resolves the real bean on first use:
```java
@Lazy private final LeadFileImportService leadFileImportService;   // breaks admin→…→admin cycle
```
This works with Lombok's `@RequiredArgsConstructor` because `lombok.config` copies `@Lazy` from the
field onto the generated constructor parameter:
```
lombok.copyableAnnotations += org.springframework.context.annotation.Lazy
```
> War story from this repo: green unit tests once passed while the app **couldn't boot** because of
> exactly these cycles. That's why there's a full-context boot smoke test
> (`RUN_CONTEXT_TESTS=true`) — compile-green ≠ boot-green here.

## 2.7 The payoff, and how you add a feature
**Payoff:** each module is independently reasoned-about and, in principle, independently
extractable. The AI module (`shared/ai`) is the deliberate first target — same `AIServicesModule`
interface, implementation swapped for an HTTP client to a Python service later. The boundaries you
maintain today *are* the future service boundaries.

**The workflow when you add a feature (from CLAUDE.md):**
1. Read the owning module's `CONTEXT.md`.
2. Decide which **single** module owns the feature.
3. If it needs data from another module, add a method to *that* module's `<Name>Module` facade
   (or publish/consume an event) — never import its internals.
4. Update the `CONTEXT.md`, add/adjust a boundary-respecting test, keep `./gradlew test` green.

## Takeaways from Step 2
1. **Facade + events** are the only two legal cross-module channels; internals (`model`/`repository`/
   `service`) are private.
2. Boundaries are **enforced by a build test** — but it's a *suffix heuristic* with real blind spots
   (entities, validators, utils), so green ≠ perfectly clean.
3. Cycles are resolved by **leaf-designation, event-inversion, or `@Lazy`** — and boot-testing
   matters because compile-green didn't guarantee boot.
4. This structure exists to make the **future extraction** (AI → Python, and beyond) a boundary
   already drawn.

---

# Step 3 — Identity & multi-tenancy

Everything in the app is scoped to a tenant, and every request carries an identity. Two modules own
this: **`shared/workspace`** (the *who* and *where* — tenants, workspaces, users) and **`auth`**
(the *proof* — tokens, login, signup). Understanding the split between them, and how a request gets
authenticated and scoped, is the key to reading any controller in the codebase.

## 3.1 The tenancy data model — `tenant → workspace → user`
All of this is owned by **`shared/workspace`** (it owns 16 tables; these are the identity core):

```
Tenant (table: tenant)                     one customer/organization
  ├─ ownerId → User                        who created it
  ├─ modules: List<Module>                 which product halves this tenant sees  ← gates the UI
  └─ 1───N Workspace (table: workspace)    sub-spaces within a tenant (teams/brands)
                └─ dailySendLimit, cc/bcc defaults, ...

User (entity: User, table: **tenant_user**)   a person   ← note: table is tenant_user, NOT "users"
  ├─ tenantId    → their home tenant
  ├─ workspaceId → their home workspace
  ├─ roles: List<UserRole>   (CUSTOMER, VENDOR, GUEST, USER, ADMIN, TENANT_OWNER)
  ├─ status, verification tokens, identityProviders (JSON)

WorkspaceUser (table: workspace_user)      the membership junction
  └─ (tenantId, workspaceId, userId) + role (OWNER/MEMBER/WORKSPACE_ADMIN) + status (INVITED/ACCEPTED/REVOKED)
```

Two things to internalize:
- **A user has a *home* workspace, but can be a *member* of many** via `workspace_user`. There are
  therefore **two role notions**: the user's global `roles` (on `tenant_user`) and their per-workspace
  role (on `workspace_user`). Don't confuse them.
- **`tenant.modules`** is load-bearing: it's the text list that drives which half of the product
  (LeadGen / RFQ marketplace) a tenant can access. The frontend's route-gating reads it too.
- Remember from Step-1/data-layer: these are **plain `Long` FK columns, no JPA relationships, no DB
  FKs**. You never traverse an object graph — you fetch by id through `WorkspaceModule`.

## 3.2 The split: `auth` is a *leaf*, `workspace` owns identity
This is a deliberate, and initially surprising, design decision:

> **`auth` owns exactly ONE table: `refresh_token`.** Every bit of *user and credential state*
> (email, password hash, roles, tenant, verification) lives in **`workspace`'s `tenant_user`** table.

Why? To break the `auth ↔ workspace` cycle (Step 2). Auth needs users; workspace needs auth for
identity. They made **auth the leaf**: auth knows nothing about tenants, and reads user/credential
data *out of* workspace via `WorkspaceModule` (injected `@Lazy` to break the constructor cycle).
Identity crosses the boundary as **auth-owned DTOs** — `AuthUserView`, `AuthCredentials`,
`SignupCommand` — that workspace populates. So workspace never leaks its `User` entity into auth.

A nice security consequence: `AuthModule.requireRole(userId, role)` **re-loads roles from workspace's
live record**, not from the (possibly stale) token — so revoking a role takes effect immediately.

## 3.3 What `auth` actually does — and where login *really* lives
`AuthModule` (the leaf's public surface) is tiny — just 4 methods:
```java
getGoogleUserInfo(accessToken)   // resolve a Google profile (used by mailbox connect)
validateToken(token)             // signature + expiry check
getUserFromToken(token)          // extract userId
requireRole(userId, role)        // authorize against LIVE workspace roles
```

`AuthController` (`/v1/auth`, all public) handles: **sign-up**, **sign-up/vendor**, **refresh**,
**forgot-password (request + reset)**, **Google OAuth**, **verify-email**.

**The surprise:** **username/password *login* is NOT in the auth module.** It lives in the bootstrap
layer at `api/configurations/JwtAuthenticationFilter` — a Spring Security filter wired to
`POST /v1/auth/login`:
```java
jwtAuthenticationFilter.setFilterProcessesUrl("/v1/auth/login");
```
So if you go looking for "the login code" in `auth/`, you won't find it — it's a security filter in
`api/configurations/`. (This is a legitimate Spring-Security pattern, but it trips people up.)

## 3.4 The JWT — what's in the token
On successful login/refresh, `JwtService` mints an **HS-signed** access token whose claims are
(verified in `JwtService.generateToken`):
```
userId · workspaceId · tenantId · name · email · roles(List) · verified · active
```
That's the whole point: **the token carries the tenant + workspace + roles**, so downstream code
knows *who* and *which tenant* without a DB hit. Two tokens exist:
- **Access token** — short-ish lived, sent as `Authorization: Bearer …` on every request.
- **Refresh token** — the only thing `auth` persists (`refresh_token` table); used at
  `POST /v1/auth/refresh` to mint a new access token (re-reading fresh state from workspace).

## 3.5 The request lifecycle — how a request is authenticated
Security is **stateless** (no server sessions). There are **three `SecurityFilterChain`s**
(`SecurityConfiguration.java`), matched by URL:

1. **`/v1/facts/**`** → API-key chain (`ApiKeyAuthenticationFilter`) — system-to-system; `X-API-KEY` **or** `ROLE_ADMIN`.
2. **`/v1/companies/**`, `/v1/contacts/**`** → a *second* API-key chain (a different lead-ingest key) **or** `ROLE_ADMIN`.
3. **Everything else** → the default JWT chain. This is the one you care about 99% of the time:
   - `permitAll` list: `/v1/auth/**`, `/v1/unsubscribe`, swagger, `/v1/chat`, `/v1/tenants/modules`, public catalog (`/v1/services|industries|specifications`), `/v1/vendors/search`, etc.
   - `/v1/admin/**` + `/v1/prompt-specifications/**` → `hasRole("ADMIN")`.
   - `anyRequest()` → `hasAnyRole("CUSTOMER","VENDOR","GUEST","USER","ADMIN")`.

Per request, **`JwtAuthorizationFilter`** (extends `BasicAuthenticationFilter`):
- **No `Bearer` header** → treated as a **guest**: assigns/echoes an `X-Guest-Id` and continues (this is how the public marketplace / anonymous chat work).
- **With a token** → validate signature+expiry, check the `active` claim (403 if false), build the `SecurityContext` from the token. Malformed/expired → 401.

## 3.6 How multi-tenancy is *enforced* — and the gap
Two mechanisms, both **manual**:
1. **Path convention:** almost every endpoint is `\/v1/tenants/{tenantId}/workspaces/{workspaceId}/…`.
   The tenant/workspace are **in the URL**, not inferred from the token.
2. **Validators** (the allowlisted `shared/workspace/controller/common` classes — `TenantValidator`,
   `WorkspaceValidator`, `UserValidator`): a controller calls e.g. `tenantValidator.validate…()` to
   check the **authenticated user (from the token) actually belongs to the tenant/workspace in the path**.

**⚠️ The architectural gap (important):** tenant isolation is **entirely developer-discipline**. There
is **no automatic enforcement** — no Hibernate tenant filter, no Postgres row-level security, no
interceptor that injects `WHERE tenant_id = ?`. If a controller **forgets** to call the validator, or
a query forgets to filter by `tenantId`, you get **cross-tenant data access**. This is exactly the
root of the IDOR findings (ISSUES.md S1/S2) — and it's why we added **S6** during this step:
*multi-tenant isolation has no framework/DB safety net.* On a no-FK, manually-scoped schema, tenant
data separation rests on every developer remembering to scope every query. Treat that as a first-class
risk.

## 3.7 Signup provisioning — one call sets up a whole tenant
When a user signs up, a single synchronous flow provisions everything (kept synchronous *because the
JWT needs `tenantId`/`workspaceId` immediately):
```
POST /v1/auth/sign-up
  → auth: RefreshTokenService.createUser(SignupCommand)
      → workspace: WorkspaceModule.createUser(cmd)   // one transactional call
          ├─ create Tenant (owner = this user)
          ├─ create the user's home Workspace
          ├─ create the User (tenant_user) with roles
          └─ create the WorkspaceUser (OWNER membership)
      ← returns AuthUserView (auth-owned DTO)
  → mint access + refresh tokens (tenantId/workspaceId now known)
```
Vendor signup is similar but also publishes a `VendorUserRegisteredEvent`, which the **vendor** module
listens for to create the vendor row (Step-2's event pattern in action).

## Takeaways from Step 3
1. **`workspace` owns identity** (tenant→workspace→user, table `tenant_user`); **`auth` is a leaf**
   owning only `refresh_token` and reading users via `WorkspaceModule`.
2. **Login lives in a bootstrap security filter** (`api/configurations/JwtAuthenticationFilter`), not
   in the `auth` module. Signup/refresh/OAuth/reset are in `AuthController`.
3. The **JWT carries `tenantId`+`workspaceId`+`roles`** → stateless auth; refresh re-reads live state.
4. Multi-tenancy is enforced by **URL path + validators, manually** — there's **no automatic tenant
   isolation** (no RLS/Hibernate filter), which is a first-class risk (ISSUES.md S6).

---

# Step 4 — The LeadGen engine (`search → campaign → outreach → tracking`)

This is the core product and the most operationally sensitive code. Think of it as a **4-stage
assembly line**, each stage a module, connected by facades (synchronous pulls) and events
(asynchronous reactions):

```
 search            campaign             outreach            tracking
 (the leads)  →    (the plan)      →    (the send)     →    (the feedback)
 who exists        who gets what,       actually mail       what happened after:
 & their data      in what sequence,    it, via the         replies, bounces,
                   in what window        tenant's mailbox    unsubscribes
```

## 4.1 `search` — the canonical lead database
Owns 11 tables — the master record of **companies and contacts**: `lead_company`, `lead_contact`,
plus enrichment (`apollo_company_data/contact_data`), organization (`lead_list`, `lead_note`,
`lead_query`), and **activity timelines** (`lead_company_event`, `lead_contact_event`).

Key concepts:
- **Apollo-backed enrichment.** Lead data is sourced/enriched from Apollo; raw responses are stored
  then mapped. (Apollo is disabled in dev — `apollo.enabled: false`.)
- **`LeadFilterCriteria`** — the most-imported public type in the whole codebase. It's the structured
  filter (industry, size, title, location, tech, …) used to define an audience. When you "search
  leads" or "target a campaign," you're building a `LeadFilterCriteria`.
- **Data-pack gating.** Which leads a tenant may even *see/target* is controlled by admin-owned
  access policy (`DataPackGate`, a JPA `Specification`). Search doesn't re-implement the policy — it
  *asks admin* for the gating spec (`AdminModule.buildDataPackGateSpecification`) and applies it. This
  is a clean example of the facade rule: policy lives in `admin`, `search` consumes it.
- **The lead timeline.** `search` is a big **event *listener*** — it reacts to campaign/outreach/
  tracking events (`CampaignLaunchedEvent`, `CampaignEmailSentEvent`, `ReplyReceivedEvent`, …) by
  writing `lead_contact_event` rows. So a contact's timeline ("emailed step 1", "replied") is
  assembled passively from events fired by the other three modules.

### 4.1.1 How leads get *into* the pool (ingestion) — and the two-datasets trap
The lead pool doesn't fill itself. A `lead_company`/`lead_contact` row is created by exactly these
paths (everything else *enriches* an existing row):

| Path | Code | Notes |
|------|------|-------|
| **Admin bulk import** 🥇 | `LeadFileImportService.processCompanyRow/ContactRow` (admin) → `searchModule.saveCompany/Contact` | The primary seeding mechanism — the platform operator uploads a CSV/Excel of companies/contacts. |
| **Direct create API** | `LeadCompanyController` `@PostMapping` / `@PutMapping` | Programmatic/single create. |
| **Tenant contact import** | `TenantContactImportService` (search; migration feature) | A tenant imports *contacts*; new companies are auto-created as a side effect. |
| **Manual contact add** | `ContactAddService` (search) | Add one contact; its new company is created as a side effect. |

**Not creation paths** (they require the company to already exist):
- **Apollo** — people-search & org-enrich both start with `getCompanyByIdOrDomain(...)`; they *enrich*, never seed.
- **Scraper** — operates on `lead_company_job` (job postings) for existing companies.

**The lifecycle:** `SEED (import/manual)` → `ENRICH (Apollo people/org, scraper jobs)` → `READ
(tenants search the local pool)`. Apollo can't run until seeding has put the company (with a domain)
in the pool — that's the chicken-and-egg.

**⚠️ The two-datasets trap (verified):** there are **two separate "company/contact" table sets**, and
conflating them is a common mistake:

| Table set | Owner | Filled by | Purpose |
|-----------|-------|-----------|---------|
| `lead_company` / `lead_contact` | **search** | import / manual (then Apollo/scraper enrich) | the **shared prospecting pool** you *search* |
| `tenant_company` / `tenant_contact` | **workspace** | **CRM sync** (Zoho/HubSpot) — all four sync services write via `TenantCompanyService`/`TenantContactService` | a **per-tenant mirror of the tenant's own CRM** |

CRM sync feeds the *workspace* tables **only** — it does **not** add anything to the search
prospecting pool (verified: the Zoho/HubSpot sync services never touch `LeadCompany`/`LeadContact`).
So "connect your HubSpot" ≠ "add companies to prospecting." The two datasets aren't linked, so the
same real company can exist independently in both — worth remembering when building any feature that
spans "my CRM" and "the prospecting database."

## 4.2 `campaign` — the plan (the most complex module)
Owns `campaign`, `campaign_contact`, `campaign_email` (the sequence steps), `contact_email` (one-off
sends), `sequence_template`, `timezone_mapping`, `campaign_chat_memory`.

**Three status machines you must know:**
- **Campaign:** `DRAFT → PENDING_APPROVAL → APPROVED → RUNNING ⇄ PAUSED → COMPLETED`. Auto-completes
  when no ACTIVE/PENDING contacts remain.
- **Campaign contact** (a lead enrolled in a campaign): `PENDING → ACTIVE → COMPLETED` (or `UNSUBSCRIBED`/`BOUNCED`).
- **Email step:** `PENDING/RUNNING/PAUSED/COMPLETED`. Default sequence = **3 steps, delays 0 / 3 / 4 days**.

**The rule that surprises people — templates are COPIED, not referenced:**
> At launch, the immutable `EmailSequenceTemplate` steps are **copied** into mutable `CampaignEmail`
> rows on the campaign. Editing a template later **does not** change a running campaign.
This is deliberate: a launched campaign is a frozen snapshot, so template edits can't retroactively
change what in-flight recipients get.

**Timezone-aware sending windows.** A campaign has a sending window (e.g. Tue–Thu, 9am–5pm in the
*recipient's* timezone). `ContactTimezoneResolver` + `timezone_mapping` resolve each contact's IANA
timezone (from state→country), and `nextValidSendTime(...)` snaps a send to the next open slot. This
is why outreach asks campaign "when may I next send this?" rather than deciding itself.

**Mail-merge + the mail-guard** live here too (`MailMergeService`) — placeholder substitution
(`{firstName}`, `{companyName}`) and the dev "redirect all mail to one inbox" guard you made
configurable earlier.

(Also here: the two AI services — `CampaignGeneratorService`, `CampaignAgentService` — that build
campaigns from a chat prompt. They're the two documented `SpringAiClient` boundary bypasses, ISSUES.md A1.)

## 4.3 `outreach` — the send engine (highest operational risk)
**Owns no tables.** It's *stateless* — pure orchestration over campaign/tracking/workspace data. Its
heart is one class: `CampaignOrchestratorService`.

**The send cron:**
```java
@ConditionalOnProperty(name = "app.outreach.scheduler.enabled", havingValue="true", matchIfMissing=true) // ON by default
@Scheduled(cron = "${campaign.orchestration.scheduler.cron}")   // "0 * * * * MON-FRI" = every minute, weekdays
public void campaignEmailOrchestrator() {
    campaignModule.getTopCampaignContactToMail()          // ← exactly ONE contact
        .ifPresent(this::processCampaignContact);
}
```

**The 5 ordered guards** (each *delays* the contact rather than dropping it) — this is the core
business logic of sending:
1. **Previous step still ongoing?** (`campaignModule.isCurrentStepOngoing`) → delay by the email's `delayDays`.
2. **Contact eligible?** (`trackingModule.validateContactEligibility`) → not unsubscribed/bounced/throttled → else +1 day.
3. **Mailbox token expired?** → +1 day (until the user re-auths their Gmail/Outlook).
4. **Daily send limit hit?** (`emailsSentToday >= workspace.dailySendLimit`, default 30) → +1 day.
5. **Outside the sending window?** → snap `nextSendAt` to `campaignModule.nextValidSendTime(...)`.

If all pass → send, advance `currentStep`/`nextSendAt` (or mark COMPLETED), and **publish
`CampaignEmailSentEvent`**.

**Provider routing** (`sendCampaignEmail`): if `tenant.mailgunDomain` matches the mailbox domain →
**Mailgun**; else `switch(mailbox.type)` → **GMAIL / OUTLOOK / SES / SMTP**. Each provider service
sends through the *tenant's own connected mailbox* (OAuth tokens live in workspace's `mailbox` table)
and appends the unsubscribe footer.

> **⚠️ Scale bottleneck (ISSUES.md A5):** the cron sends **one email per tick, once a minute, weekdays
> only** → a hard **global** ceiling of ~**1 email/minute (~1,440/day) for the *entire platform*,
> across all tenants and campaigns combined.** For a mass-outreach product that's a serious throughput
> limit — worth understanding before you reason about capacity.

## 4.4 `tracking` — the feedback loop
Owns one table: `contact_outreach_status` (note `current_campaign_ids` is a native Postgres
`varchar[]`). Status enum `GlobalOutreachStatus`: `ACTIVE, PAUSED, COMPLETED, BOUNCED, UNSUBSCRIBED,
CONVERTED`.

**Eligibility** (`validateContactEligibility`, called by outreach guard #2):
- no email → ineligible; `BOUNCED`/`UNSUBSCRIBED` → ineligible;
- `ACTIVE` → if it's a **follow-up step** of the same campaign, bypass the throttle; else respect
  `last-email-throttle-days` (**7**);
- `COMPLETED` → respect `sequence-cooldown-days` (**90**).

**Reply & bounce detection** (`AbstractReplySyncService`, Gmail + Azure variants; own cron):
- Pages recently-SENT campaign contacts, fetches each email's thread/conversation (via
  `OutreachModule.getGmailThread` / `getAzureConversation` — so tracking doesn't hold provider
  clients), inspects later messages.
- A later message `From` == an original `To` recipient → **reply** → mark `REPLIED`, publish `ReplyReceivedEvent`.
- A mailer-daemon bounce → mark `BOUNCED`, publish `CampaignEmailBouncedEvent`.

**Unsubscribe is TENANT-WIDE**, not per-campaign: `GET /v1/unsubscribe?token=…` sets the contact
`UNSUBSCRIBED` across the whole tenant and publishes `ContactUnsubscribedEvent`. One unsubscribe stops
*all* outreach to that person for that tenant. (This is the flow you fixed the redirect link for.)

**Subtle boundary note:** tracking flips the campaign contact `Sent → REPLIED` by calling
`campaignModule.saveCampaignContact(...)` (through the facade — allowed) *and* publishes the event for
search's timeline. So a "reply" updates two modules, both via legal channels.

## 4.5 The event nervous system (how the 4 stages actually stay in sync)
The facades are the *pulls*; these events are the *pushes* that keep the pipeline coherent without
tight coupling:
```
campaign  ──CampaignLaunchedEvent──▶  tracking (register contacts) + search (timeline)
outreach  ──CampaignEmailSentEvent─▶  tracking (status) + workspace (mailbox counter) + search (timeline)
tracking  ──ReplyReceivedEvent─────▶  search (timeline)          [+ flips campaign contact via facade]
tracking  ──ContactUnsubscribedEvent▶ campaign (exclude contact)
tracking  ──CampaignEmailBouncedEvent▶ campaign (mark bounced)
campaign  ──CampaignCompletedEvent──▶ tracking (close out)
```
Notice **outreach listens to nothing** — it's a pure orchestrator/producer. And **search listens to
almost everything** — it's the passive timeline-builder. That asymmetry is the shape of the engine.

## Takeaways from Step 4
1. **Assembly line:** `search` (lead data) → `campaign` (the plan: sequences, windows, statuses) →
   `outreach` (the stateless send cron + 5 guards) → `tracking` (eligibility + reply/bounce/unsub).
2. **Templates are copied at launch**, not referenced — a running campaign is a frozen snapshot.
3. **Sends go through the tenant's own mailbox**, gated by 5 guards, one contact per minute — a real
   global throughput ceiling (A5).
4. **Unsubscribe is tenant-wide**, and the four stages stay in sync via **ID-only events**, with
   `search` as the passive timeline-builder and `outreach` as a pure producer.

---

# Step 5 — The RFQ marketplace (`buyer / vendor / rfq`)

The *other* half of the product — completely separate from the LeadGen engine. It's a **two-sided
marketplace**: **buyers** discover **vendors** and send them **RFQs** (requests for quote); vendors
respond with **quotations**. Three modules, plus heavy reliance on `admin` (the service/industry
catalog + agreement templates) and `workspace` (users, emails, attachments).

## 5.0 The actual dependency graph (verify code, not the docs)
```
        buyer ─────▶ vendor ─────▶ admin, workspace
          │            ▲
          └────────────┤
        rfq ───────────┘──────────▶ admin, workspace
       (+ ai for NL search)
```
Verified from imports:
- **buyer** → `VendorModule`, `AIServicesModule`
- **rfq** → `VendorModule`, `AdminModule`, `WorkspaceModule`
- **vendor** → `AdminModule`, `WorkspaceModule`

So within the trio, **`vendor` is the most depended-upon** (both buyer and rfq call it), and there
are **no cycles**. ⚠️ **Doc drift to note:** `rfq/CONTEXT.md` says *"depends on auth ONLY; rfq is the
leaf."* That's **false** in the code — `rfq` depends on `vendor` + `admin` + `workspace`. What's
*true* is the narrower claim that **buyer/vendor context is passed into rfq as parameters** (rfq never
calls *back* into buyer), which is how the buyer↔rfq cycle was avoided. (Logged: ISSUES.md X5.)

## 5.1 Who owns what
| Module | Owns (tables) | Role |
|--------|---------------|------|
| **`vendor`** | `vendor`, `vendor_showcase`, `vendor_agreement` | The supplier side: profile, portfolio, signed agreements |
| **`rfq`** | `request_for_quote`, `request_for_proposal`, `quotation`, `collaborator` | The transaction: requests, responses, teams |
| **`buyer`** | **nothing** | A thin edge module — just `VendorSearchController` over `VendorModule` + `AIServicesModule` (Step-2's "edge module, no tables" archetype) |

The **`vendor`** entity is a big denormalized profile: `industryIds/serviceIds/specificationIds`
(arrays of catalog ids validated against `admin`), `certifications`, `regionsCovered`, client-fit
arrays (`clientBudgets/Sizes/Industries/…`), plus JSONB-embedded `Address`, `SocialMedia`, and
onboarding `Answer` list. Status = `VendorVerificationStatus { INCOMPLETE, PENDING, APPROVED, REJECTED }`.

## 5.2 The vendor lifecycle (signup → approval → module enablement)
```
1. Vendor signup       auth: POST /v1/auth/sign-up/vendor → publishes VendorUserRegisteredEvent (IDs only)
2. Vendor row created  vendor: @EventListener handleVendorUserRegistered → createVendorFromUser(...)
                              → adminModule.assignSoftwareDataPackToVendor(...)   (gives them a starter data pack)
                       status = INCOMPLETE
3. Onboarding          vendor fills the profile (4-step wizard in the UI); catalog ids validated via AdminModule
4. Submit for review   PUT /v1/users/me/vendor/pending → updateVendorToPending → status = PENDING
5. Admin approval      admin console → AdminVendorController → vendorModule.updateVendorStatus(id, APPROVED)
                              ├─ workspaceModule.sendVendorApprovedEmail(...)
                              └─ workspaceModule.enableVendorModules(tenantId)   ← unlocks the vendor UI for the tenant
                       status = APPROVED   (or REJECTED with a review comment + rejection email)
```
Two things worth noting:
- **Approval is a cross-module *direct call*, not an event** — `updateVendorStatus(APPROVED)` calls
  `WorkspaceModule.enableVendorModules(tenantId)` synchronously. (A legacy `VendorApprovedEvent` file
  still lingers but the direct call is what's used.)
- **Only `APPROVED` vendors are ever visible to buyers** (enforced in `VendorService.searchVendors`).

## 5.3 Vendor agreement signing — the OTP flow
Vendors must sign agreements (Privacy Policy, Terms of Service) before/while operating. The
**templates & versions live in `admin`** (the `agreement` table, `AgreementType {PRIVACY_POLICY,
TERMS_OF_SERVICE}`); the **signed instance + OTP state lives in `vendor`** (`vendor_agreement`).

```
GET  /v1/vendor-agreements/{vendorId}?agreementType=…   → render agreement (adminModule.getLatestAgreementByType
                                                           + {{PLACEHOLDER}} substitution: NAME/EMAIL/COMPANY/…)
POST /v1/vendor-agreements/otp        → sendOTPToVendor: 6-digit SecureRandom, emailed via WorkspaceModule;
                                        90s resend throttle (OtpResendTooSoonException)
POST /v1/vendor-agreements/verify-otp → verifyVendorOTP: max 3 attempts, 1-hour expiry, single-use → mark signed
```
Each call first does `validateAuthenticatedVendorId(vendorId)` (ownership-checked — unlike the
showcase controller, see §5.7). Agreements are **versioned**: if admin's latest version differs from
what the vendor signed, they're re-prompted.

## 5.4 The buyer side — vendor discovery
`buyer` is just `VendorSearchController` (`/v1/vendors`), 3 endpoints:
- **`POST /search`** — filtered vendor search (industry/service/spec/certs) via `VendorModule.searchVendors`.
  Public/anonymous is supported but **capped + anonymized**: logged-out callers get `PageRequest.of(0, 5)`
  and a `VendorDetailResponse::fromDtoToAnonymous` projection (name/contact hidden) — a teaser to drive signup.
- **`POST /search/parse`** — natural-language → structured filters via `AIServicesModule.parseVendorSearchQuery`
  (the `vendor-query-parser` prompt; the *only* real AI-module method exposed as a facade call).
- **`GET /{vendorId}`** — the public vendor profile.

> ⚠️ Not built: the CONTEXT lists a full **"AI Sourcing Assistant"** chat as buyer-owned, but only the
> `search/parse` NL-parse exists — the assistant itself was never implemented (known gap).

## 5.5 The RFQ lifecycle (the transactional core)
```
POST /v1/request-for-quotes                       buyer creates an RFQ
  RequestForQuoteService.createRFQ:
    ├─ adminModule.validateServiceIds(serviceIds)      // catalog validation
    ├─ vendorModule.validateVendorIds(vendorIds)       // the invited vendors exist
    ├─ status = OPEN
    ├─ collaboratorService.createOwner(rfqId, REQUEST_FOR_QUOTE, userId)   // creator = OWNER
    └─ for each vendorId: publishEvent( CreateRequestForQuoteEvent(rfqDto, vendorId) )   // fan-out

  QuotationService.handleCreateRequestForQuoteEvent   @Async @TransactionalEventListener
    └─ per invited vendor: create a PENDING quotation "shell"   // so each vendor has a slot to fill
```
Then vendors respond (`QuotationRequestForQuoteController`, gated by `vendorValidator.getAuthenticatedVendor`):
they fill their PENDING shell → `QuotationStatus { PENDING → QUOTED }`, and the buyer
**accepts/rejects** → `ACCEPTED / REJECTED`. The RFQ itself is `RequestStatus { OPEN → CLOSED / CANCELLED }`.
Quotation **items are a JSONB list** (`QuotationItemListConverter`), not a child table — so comparison
is in-memory (Step-1's no-FK/embedded-JSON pattern).

**RFP (Request For Proposal)** is the softer sibling ("propose an approach," not just price). It's
**built on the backend** (12 endpoints: `RequestForProposalController` + collaborator + quotation
controllers) but **stubbed in the UI** — the `customer/rfps` and `vendor/rfps` pages are `ComingSoon`.
So RFP is dormant/half-shipped: API exists, no front-end.

## 5.6 Collaborators — team access on a request
Any RFQ/RFP can have collaborators (invite teammates to evaluate quotes together).
`CollaboratorRole { OWNER, EDITOR, VIEWER, BD }`, with `CollaboratorValidator`:
- **editorRoles** = {OWNER, EDITOR} · **viewerRoles** = {OWNER, EDITOR, VIEWER, BD}
- `validateOwner / validateEditor / validateViewer` read the authenticated user's collaborator record
  and gate the action. Invites go out as emails via `WorkspaceModule.sendCollaboratorInvite` with a
  link built from `${client.url}`. (`BD` = a business-development role that can view but not edit.)

## 5.7 The security gap you must know (verified live)
The collaborator role checks exist **but are applied inconsistently**:
- `RequestForQuoteCollaboratorController` (the collaborator sub-controller) **is** guarded (7 validation refs).
- **`RequestForQuoteController`** — the *main* RFQ CRUD — has **8 mappings and 0** `CollaboratorValidator`
  calls. So `GET/PUT/DELETE /request-for-quotes/{id}` and the attachment ops perform **no ownership/
  collaborator check** → any authenticated user can read/modify/delete any RFQ by id (**IDOR**). Same
  pattern on `CustomerQuotationController` and `VendorShowcaseController` (0 ownership checks).
This is **ISSUES.md S1/S2** — the concrete instance of the S6 "no automatic tenant isolation" risk.

## Takeaways from Step 5
1. A **two-sided marketplace**: `vendor` (supplier profiles + agreements), `rfq` (RFQ→quotation
   transaction + collaborators), `buyer` (a thin search edge, no tables). Real dep graph:
   buyer→vendor, rfq→vendor, vendor→admin/workspace — **no cycles** (and the "rfq is auth-only leaf"
   doc is wrong).
2. **Vendor lifecycle:** `INCOMPLETE→PENDING→APPROVED` — admin approval is a **direct** call that
   `enableVendorModules` + emails; only APPROVED vendors are buyer-visible.
3. **Agreements** = admin-owned templates, vendor-owned signed instances, gated by a **6-digit OTP**
   (90s throttle, 3 attempts, 1-hr expiry).
4. **RFQ fan-out:** create → validate → per-vendor `CreateRequestForQuoteEvent` → auto `PENDING`
   quotation per vendor → QUOTED → ACCEPTED/REJECTED. **RFP is backend-built but UI-stubbed.**
5. **Security:** collaborator checks exist but the **main RFQ CRUD skips them (IDOR, S1)** — the
   marketplace's headline risk.
