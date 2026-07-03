# LeadPlus — Issues, Gaps & Tech-Debt Register

A living log of problems, risks, and gaps found in the LeadPlus platform while learning it.
**Append new findings as we proceed** (add a row to the right section; keep IDs sequential).

**Legend**
- Severity: 🔴 High · 🟠 Medium · 🟡 Low / smell
- Status: 🟢 Fixed · 🔵 Open · ⚪ Deferred / tracked elsewhere
- "Where": path is relative to `/home/jathin/Corelabs` (the real codebase) unless noted.

> Context for these findings lives in the course (`README.md`) and Claude Code memory
> at `/home/jathin/.claude/projects/-home-jathin-Corelabs/memory/`.

---

## 1. Data layer & schema design

| # | Issue | Sev | Status | Where / notes |
|---|-------|-----|--------|---------------|
| D1 | **No foreign-key constraints anywhere** — 65 tables, 0 FKs (verified in `schema.sql` and live DB). Even *intra-module* relationships (e.g. `quotation`→`request_for_quote`) have no FK. Integrity is 100% the app's job, but app enforcement is lax (soft-deletes, no orphan cleanup) → you get the downsides of no-FK without the compensating discipline. Legacy of the Mongo→Postgres migration. | 🟠 | 🔵 | `Leadplus-corelabs/leadplus-service/src/main/resources/schema.sql`. Cross-module FKs are *intentionally* absent (aligns with modular/services split) — only intra-module ones are the concern. |
| D2 | **Essentially no secondary indexes** — `schema.sql` has 0 `CREATE INDEX`; live DB has only 3 (all UNIQUE constraints). Hot tables scanned every minute (e.g. `campaign_contact` looked up by `campaign_id`+`status`) have **only the PK index** → full table scans on `*_id`/status/email/tenant_id lookups. Degrades non-linearly with data growth. **This is the most production-risky item.** | 🔴 | 🔵 | An orphaned `db/changelog/002-indexes.yaml` *defines* indexes but was never applied. Add btree indexes on hot `*_id`/status/email/tenant_id columns. |
| D3 | **No single source of truth for schema** — Liquibase & Flyway both on classpath but **disabled**; `schema.sql` is de-facto truth; `ddl-auto: validate` checks tables/columns but **not indexes**, so prod can silently drift on indexes and nobody notices. | 🟠 | 🔵 | Decide: Liquibase/Flyway *or* `schema.sql`, then enforce in CI. Re-enabling Liquibase on existing DBs needs baselining. |
| D4 | **Soft-delete via `active boolean`** — every query must remember `WHERE active = true`; forgetting it is a classic bug; makes unique constraints awkward. | 🟡 | 🔵 | Pattern-wide (lead_company, lead_contact, rfq, vendor, …). |
| D5 | **Denormalized JSONB/`text` columns** for embedded objects (`email_data`, address/answers/lead-filter JSON converters) — lose queryability/indexability on contents; can't constrain. | 🟡 | 🔵 | Pragmatic (Mongo heritage); fine for read-mostly blobs. |
| D6 | **Postgres array columns** (`varchar[]` for segments/tags/keywords) — non-standard, portability tax (H2 rejected `varchar[]` during the boot smoke test). | 🟡 | 🔵 | Works on Postgres; a friction point for tests/other engines. |

## 2. Security & correctness (from full-codebase audit; mostly un-ticketed)

| # | Issue | Sev | Status | Where / notes |
|---|-------|-----|--------|---------------|
| S1 | **IDOR — RFQ/RFP/quotation main CRUD** endpoints don't call `CollaboratorValidator` (only the collaborator sub-controllers do). Any authenticated user can read/modify/delete another tenant's RFQ/quotation by guessing an id. | 🔴 | 🔵 | `portal/rfq/controller/{RequestForQuoteController,RequestForProposalController,CustomerQuotationController}` |
| S2 | **IDOR — vendor showcase** — `VendorShowcaseController` has no ownership/role check (operates purely by `showcaseId`). | 🔴 | 🔵 | `portal/vendor/controller/VendorShowcaseController` |
| S3 | **Admin write endpoints not ADMIN-gated** — `/v1/facts` (POST/PUT/DELETE), `/v1/apollo-specification`, `/v1/lead-data-packs`, `/v1/service-categories`, `/v1/specification-categories` fall through to `anyRequest().hasAnyRole(CUSTOMER,VENDOR,GUEST,USER,ADMIN)` = any authed user. | 🟠 | 🔵 | `api/configurations/SecurityConfiguration.java` |
| S4 | **Secret hygiene** — JWT secret hardcoded in `application.yml`; `PRODUCTION_MIGRATION_READY.md` has prod-looking Mongo/Postgres creds committed in git history. (Google OAuth creds were externalized to `.env` in PR #43.) | 🟠 | 🔵 | Rotate + scrub; move JWT secret to env. |
| S5 | **Dead / misleading code** — `JwtService.extractUserRoleFromToken` reads a non-existent `"role"` claim (always null); JWT `active` claim hardcoded `true` (the 403-inactive path never fires); no open/click tracking exists (`OPENED`/`EMAIL_OPENED` enums are dead → "open rate" analytics have no data). | 🟡 | 🔵 | `auth/service/JwtService.java`; outreach send paths. |

## 3. Migration breakage — Limark migration PRs #41/#42 (root cause: no green-build gate)

| # | Issue | Sev | Status | Where / notes |
|---|-------|-----|--------|---------------|
| M0 | **Process gap: the migration was merged to `main` with a red build.** All of M1–M7 below shipped because CI didn't block a non-compiling merge. This is the systemic issue to fix (require green build + boot before merge). | 🔴 | 🔵 | Add a required CI gate on PRs, not just on push-to-main. |
| M1 | main didn't compile (14 errors): migrated files used old package paths (`infrastructure.springai.SpringAiClient`, `api.common.UserValidator`), missing `TenantDataSource` import, missing `DataSource.MANUAL`, missing repo query methods. | 🔴 | 🟢 | Fixed PR #44 (`5533cfc`). |
| M2 | 12 module-boundary violations introduced (search/campaign/admin importing other modules' Service/Repo/Client). | 🟠 | 🟢 | Fixed PR #44 — routed through AdminModule/WorkspaceModule/OutreachModule/SearchModule facades. |
| M3 | Missing classpath resources not copied (`column-mapping-system-prompt.md`, `campaign-summary-email-template-preview/*.html`) → `ExceptionInInitializerError` at boot. | 🔴 | 🟢 | Fixed PR #44 (copied from Limark). |
| M4 | `spring.ai.anthropic.api-key` missing → new Anthropic starter's `anthropicApi` bean fails at startup. | 🔴 | 🟢 | Fixed PR #44 (dummy default + `ANTHROPIC_API_KEY` override). |
| M5 | `schema.sql` not updated for new entities → prod `validate` boot would fail (`tenant_data_source` table, `lead_file_import.tenant_id/source_label`). | 🔴 | 🟢 | Fixed PR #44 (`0434fe9`). |
| M6 | `CampaignEmailServiceTest` missing an `ApplicationEventPublisher` mock (migration added event publishing) → NPE test failures. | 🟡 | 🟢 | Fixed PR #44. |
| M7 | Frontend `getDataSources` export + backend `GET /leads/data-sources` endpoint both missing (hook was migrated without them) → leadgen search page crashed at build. | 🟠 | 🟢 | Fixed PR #45. |

## 4. Config & ops

| # | Issue | Sev | Status | Where / notes |
|---|-------|-----|--------|---------------|
| C1 | Mail-guard redirect was only half-configurable (campaign path read the property; announcement path hardcoded a different inbox; code default ≠ yml value; CI didn't inject the key). | 🟠 | 🟢 | Fixed PR #43 (unified + configurable). |
| C2 | Reply-tracking cadence split across two provider-specific cron keys; also `client.url` / Google OAuth secrets hardcoded in yml. | 🟡 | 🟢 | Fixed PR #43 (single `reply-tracking.poll-cron`; `${CLIENT_URL}` / `${GOOGLE_CLIENT_ID}` env; secrets → gitignored `.env`). |
| C3 | Scheduler kill-switch naming confusion — docs/CONTEXT say `app.outreach.scheduler.enabled` (which *is* the real gate, default ON), but a `campaign.orchestration.scheduler.enabled` key also exists in yml and is **read nowhere**. Setting the latter to pause the cron does nothing. | 🟡 | 🔵 | `application.yml` + `outreach/service/CampaignOrchestratorService.java` |

## 5. Documentation / code drift (trust the code, not these docs)

| # | Issue | Sev | Status | Where / notes |
|---|-------|-----|--------|---------------|
| X1 | Stale legacy docs describe a Mongo/Maven/Java-17 world that no longer exists: `CONTRIBUTING.md`, `docs/MONGODB_INDEXES.md`, `docs/QUERY_OPTIMIZATION.md`, `COMPREHENSIVE_MIGRATION.md`, `PRODUCTION_MIGRATION_READY.md`. Actual stack: Java 21 / Gradle / PostgreSQL-JPA. | 🟡 | 🔵 | Delete or clearly mark as historical. |
| X2 | Per-module `CONTEXT.md` files say the `*Module` facades are "Planned (Day 6–7)" — they're fully implemented. | 🟡 | 🔵 | Update CONTEXT.md files. |
| X3 | `ModuleBoundariesTest` enforces **all 11 modules**; docs/CONTEXT still describe "5 strict / 8 backlog". Its exemption set is 4 adapters + 2 SpringAiClient files (docs say "5 + 2 = 7"). | 🟡 | 🔵 | `src/test/java/ai/leadplus/ModuleBoundariesTest.java` |
| X4 | `Docs/RESUME-HERE.md` says Liquibase was re-enabled — it's **disabled**. | 🟡 | 🔵 | See D3. |

## 6. Architecture debt (known / tracked)

| # | Issue | Sev | Status | Where / notes |
|---|-------|-----|--------|---------------|
| A1 | Two pre-existing AI-call bypasses call `SpringAiClient` directly instead of `AIServicesModule` (`CampaignEmailAiService`, `ContactEmailAiService`) — exempted in the boundary test, tracked as a follow-up ticket. | 🟡 | ⚪ | `leadgen/campaign/service/` |
| A2 | Layer inversions: `Campaign` entity imports application-layer `LeadFilterCriteria`; admin `IndustryRepository` imports a campaign DTO. | 🟡 | 🔵 | Flagged in audits. |
| A3 | Backlog of audited boundary violations in the "8 non-strict" modules recorded in `leadplus-service/docs/migration/boundary-violations.csv` (note: the test now scans all 11, so most enforced ones are resolved). | 🟡 | ⚪ | tracking CSV |
| A4 | **`ModuleBoundariesTest` is a suffix heuristic, not a true arch gate** — it only flags cross-module imports whose simple name ends in `Service`/`Repository`/`Client`. So forbidden imports that *don't* match the suffix slip through undetected: cross-module **`Entity`** imports (the rule forbids them), controller-layer classes (e.g. rfq controllers import vendor's `VendorValidator` — a real leak), and `*Util` helpers. "Green boundary test" ≠ "clean boundaries." | 🟠 | 🔵 | `src/test/java/ai/leadplus/ModuleBoundariesTest.java` (INTERNAL regex). Consider ArchUnit for real package-dependency rules. |

## 7. AI layer (`shared/ai`)

| # | Issue | Sev | Status | Where / notes |
|---|-------|-----|--------|---------------|
| AI1 | **Unused Anthropic starter forces a mandatory config key** — `spring-ai-starter-model-anthropic` is on the classpath (from the migration's "Agent Factory" deps) and auto-configures an `anthropicApi` bean that **requires `spring.ai.anthropic.api-key` at startup**, but **no Java code uses Anthropic** — every real AI call goes through OpenAI (`gpt-4.1-mini`, `OpenAiChatModel`). So the app carries a mandatory secret for a feature that doesn't exist. Already caused boot failure `M4`. | 🟡 | 🔵 | `build.gradle`; `application.yml` (`spring.ai.anthropic.api-key`). Remove the dep until Agent Factory lands, or disable its auto-config. |
| AI2 | **Prompts/templates loaded at static class-init** — 5 classes do `static final String X = FileReader.readFileContentFromClasspath("…")`. Any missing/misnamed resource throws `ExceptionInInitializerError` and **crashes the whole app at startup** with a cryptic error (not a clean message). This is exactly what caused boot failure `M3`. | 🟠 | 🔵 | `ChatService`, `LeadChatService`, `ContactEmailAiService`, `EmailPreviewService` (×2). Move to `@PostConstruct`/lazy with a clear error message. |
| AI3 | **`MessageController.getConversations` hardcodes `MessageType.CAMPAIGN_AGENT`** — a generically-named "get conversations" endpoint only ever returns campaign-agent conversations; it's really a campaign-agent query mislabeled as generic. | 🟡 | 🔵 | `shared/ai/controller/MessageController.java` |
| AI4 | **Dead AI-disable flags + illusory graceful degradation.** `spring.spring-ai.enabled: false` and `app.spring-ai.enabled: false` are in `application.yml` but **read by no code** (dead flags). The AI beans are **unconditional** `@Component`s, so `Optional<SpringAiClient>` is never empty → `AIServicesModule.getChatCompletion`'s `.orElse(null)` ("returns null when AI not configured") is **unreachable dead code** and its javadoc is wrong. Real behavior: AI is **always on**; with the dummy staging key, AI feature calls **throw a 401 at runtime** instead of degrading. There is effectively **no working kill-switch for AI**, contrary to what the config implies. | 🟠 | 🔵 | `shared/ai/AIServicesModule.java`, `SpringAiClient.java`, `application.yml`. Note `parseVendorSearchQuery` *throws* when unavailable while `getChatCompletion` *returns null* — inconsistent, and the null path can't actually trigger. Related: A1. |

---

## How to append
When we find something new while going through the course:
1. Add a row under the right section with the next ID (e.g. `D7`, `S6`).
2. Set Severity + Status + a short "Where / notes".
3. Commit + push (`cd ~/leadplus-learning && git add -A && git commit -m "issues: add <ID>" && git push`).
