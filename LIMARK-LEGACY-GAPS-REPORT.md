# LeadPlus Legacy (Limark) Application — Technical Gaps & Concerns

A constructive review of the **original Limark Spring Boot application** (the legacy `leadplus-service`
codebase as written by the Limark team), highlighting technical gaps, risks, and areas to harden —
intended as discussion material for a working session with the Limark team.

> **Scope note:** this is about the *legacy application itself*, **not** the later migration of its
> features into the modular `Leadplus-corelabs` codebase (that's a separate report). Findings below
> were verified directly against `Limark/leadplus-service`.

**What was reviewed:** ~970 Java files · 65 JPA entities · 82 REST controllers · Spring Boot / Java /
Gradle / PostgreSQL (JPA).

---

## Priority summary (if we fix only three things)
1. **Database has no indexes and no foreign keys** → full-table-scan performance risk + no integrity guarantees. *(§1)*
2. **Broken access control (IDOR)** on transactional endpoints — ownership checks are applied inconsistently. *(§2)*
3. **Secrets committed in source** (JWT signing secret in `application.yml`). *(§2)*

---

## 1. Database design & data integrity

| Finding | Evidence | Why it matters |
|---------|----------|----------------|
| **No foreign-key constraints** | `schema.sql`: 65 tables, **0** `FOREIGN KEY` | The DB enforces no referential integrity — orphaned rows and bad references are possible; integrity is entirely the app's responsibility, and it's enforced only loosely. |
| **Almost no indexes** | `schema.sql`: only **5** `CREATE INDEX` for **65** tables; hot lookup columns (`*_id`, `status`, `email`, `tenant_id`) are unindexed | Common lookups become **full table scans**; performance degrades non-linearly as tenants/leads/campaigns grow. The single highest-impact fix. |
| **No schema-migration tooling in use** | `spring.liquibase.enabled: false` (Liquibase on classpath but disabled); schema is a hand-maintained `schema.sql` with `ddl-auto: validate` | No versioned, repeatable schema management; environments can drift (especially on indexes, which `validate` doesn't check); no single source of truth. |

**Direction:** add indexes on the hot `*_id`/status/email/tenant columns; adopt a real migration tool
(Liquibase/Flyway) as the one authoritative mechanism; add FKs at least within a bounded context.

## 2. Security

| Finding | Evidence | Why it matters |
|---------|----------|----------------|
| **IDOR — missing ownership checks (inconsistent enforcement)** | `VendorShowcaseController` (8 mappings, **0** ownership checks); `CustomerQuotationController` (2 mappings, **0** checks); `RequestForQuoteController` (8 mappings, only **2** validation refs). Contrast: `RequestForQuoteCollaboratorController` **is** properly guarded (7 refs). | A user can read/modify/delete another tenant's resources by guessing an id. The team clearly knows the pattern (it's applied on the collaborator controller) but it isn't applied consistently across the transactional endpoints — a classic broken-object-level-authorization gap (OWASP A01). |
| **Secrets committed to source control** | `application.yml`: `jwt.secret: 23a50ad6…` (a real signing secret) hardcoded; also `PRODUCTION_MIGRATION_READY.md` contains prod-looking DB credentials | Anyone with repo access can forge JWTs / reach prod. Secrets belong in env/secret-manager, and any leaked ones must be rotated. |
| **Dead / misleading auth code** | `JwtService` reads a **`"role"` (singular)** claim that is never written (2 refs) → the role-extraction path is always empty/dead | Suggests role handling that doesn't actually work; a maintenance and correctness trap. |
| **Shared cross-tenant lead pool + a null-tenant leak** | `lead_company`/`lead_contact` are a **single global table** shared across tenants via a `tenant_ids` **array** (not per-tenant rows). Access is filtered only in queries as `WHERE (tenant_ids IS NULL OR tenant_ids @> ARRAY[:tenantId])` — so **any row with `tenant_ids IS NULL` is visible to *every* tenant**, and any query that forgets the filter exposes the whole pool | Tenant isolation for leads rests entirely on remembering the filter (no per-tenant storage, no DB-level enforcement); the `NULL`-visible-to-all rule is an active leak. Also a **data-governance/PII** concern — one tenant's sourced person data physically sits in a table others query. |
| **No framework/DB-level tenant isolation anywhere** | Verified: **0** Hibernate `@Filter`/`@FilterDef`, **0** Postgres row-level security, **0** tenant interceptors/aspects. **Every** tenant-scoped query must manually add `WHERE tenant_id = …` (and every object-scoped controller must manually validate) | Multi-tenant data separation is 100% developer discipline with no safety net — the systemic root behind both the IDOR findings and the lead-pool leak above. One forgotten scope = cross-tenant exposure. |

**Direction:** apply a consistent ownership/authorization check (a shared validator) to *every*
object-scoped endpoint; move all secrets to env/secret-manager and rotate; remove the dead role path;
reconsider the shared-pool + `NULL`-visible-to-all rule for tenant/PII isolation.

## 3. Reliability — fragile startup

| Finding | Evidence | Why it matters |
|---------|----------|----------------|
| **Prompts/templates loaded at static class-init** | 4 classes do `static final String X = FileReader.readFileContentFromClasspath("…")` | Any missing/misnamed resource throws `ExceptionInInitializerError` and **crashes the entire app at boot** with a cryptic error (not a clean, actionable message). A single packaging slip takes the whole service down. |

**Direction:** load such resources lazily / in `@PostConstruct` with clear error handling, so a missing
file fails one feature gracefully instead of the whole application.

## 4. Dead / unused capability

| Finding | Evidence | Why it matters |
|---------|----------|----------------|
| **Email open/click tracking is declared but not implemented** | `OPENED` / `EMAIL_OPENED` delivery-status values exist (6 files), but there is **no open-tracking mechanism** (no tracking pixel / click-redirect) populating them | "Open rate"–style analytics can't have real data; the enum implies a capability the product doesn't actually have. Either implement tracking or remove the dead states. |

## 5. Testing coverage

| Finding | Evidence | Why it matters |
|---------|----------|----------------|
| **Thin coverage relative to surface area; no architecture/web/security tests** | ~**64** test classes for **82** controllers + 65 entities + hundreds of services; **0** ArchUnit/boundary tests | Tests are largely service-level units. There's little web-layer (`@WebMvcTest`), security, or integration testing — which is exactly why gaps like the IDOR and the boot-fragility issues weren't caught. |

**Direction:** add web-layer + security tests around the transactional/authorization paths; add a
smoke/integration test that actually boots the context.

## 6. Architecture & maintainability

| Finding | Evidence | Why it matters |
|---------|----------|----------------|
| **No enforced internal boundaries** | Layered monolith (`api` / `application` / `domain` / `infrastructure`); **0** boundary-enforcement tests; any `application`-layer service can import any other; minor upward leaks (`application` imports `api` in 3 files) | With ~970 files and no isolation between features (campaign/outreach/vendor/rfq/…), coupling accumulates freely — making the code hard to reason about, test in isolation, and evolve/split. *(This unrestricted coupling is the primary reason a modular refactor was undertaken downstream.)* |

**Direction:** introduce feature/module boundaries with an enforcement mechanism (e.g. ArchUnit or
Spring Modulith), so cross-feature access goes through explicit interfaces rather than direct imports.

## 7. Documentation

| Finding | Evidence | Why it matters |
|---------|----------|----------------|
| **Stale, misleading docs** | `CONTRIBUTING.md`, `docs/MONGODB_INDEXES.md`, `docs/QUERY_OPTIMIZATION.md` all describe a **MongoDB** (and Maven/older-Java) world, while the app actually runs on **PostgreSQL / JPA / Gradle** | New engineers are actively misled about the datastore, build tool, and setup — a real onboarding trap. |

**Direction:** delete or clearly archive the Mongo-era docs; keep a single accurate CONTRIBUTING/run guide.

## 8. Scalability & operations

| Finding | Evidence | Why it matters |
|---------|----------|----------------|
| **Email send throughput capped at ~1/minute — globally** | `CampaignOrchestratorService.campaignEmailOrchestrator` fetches and sends **exactly one** contact per tick (`getTopCampaignContactToMail()` → `Optional`), on a `0 * * * * MON-FRI` schedule | The **entire platform** (all tenants, all campaigns) sends at most ~1 email/minute (~1,440/day, weekdays only) through a single serial global queue — no batching, no per-tenant parallelism/fairness. A hard scale wall for a mass-outreach product. |
| **No open/click tracking despite the enum implying it** | `OPENED`/`EMAIL_OPENED` delivery states exist but nothing populates them (no tracking pixel / click-redirect) | "Open/click rate" analytics have no real data (see §4). |

**Direction:** batch multiple contacts per tick and parallelize per mailbox/tenant (respecting each
mailbox's daily limit); if open/click metrics are a product goal, implement pixel/redirect tracking.

---

## One-line framing for the meeting
> "The legacy app is functionally rich, but it carries **infrastructure-level risks** — no DB
> indexes/FKs, inconsistent authorization (IDOR), committed secrets, and no enforced module
> boundaries — that we'd like to align on hardening. None are large individually; together they're
> the difference between 'works today' and 'safe to scale.'"
