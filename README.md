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
