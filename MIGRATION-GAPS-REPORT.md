# Limark → Leadplus-corelabs Migration — Defects & Gaps Report

**Scope:** the migration that ported Limark features into the modular `Leadplus-corelabs` codebase
(GitHub PRs **#41** and **#42**, merged to `main`). This report lists the defects/gaps that shipped
with that migration and broke the `main` build, so they can be avoided next time.

**Impact:** `main` was left in a **non-compiling** state (14 compile errors, plus latent failures
that only surfaced after those were fixed). Because deploy runs on push-to-`main`, the production
build/deploy was red.

**Resolution:** fixed in PRs **#44** and **#45** (`main` now compiles, all 485 tests + boundary
test pass, and the app boots on PostgreSQL in prod `validate` mode). Details per item below.

---

## Root cause (one paragraph)
The migrated feature code was authored against the **old, pre-modularization package layout** and
copied into the new modular structure with each file's `package` line updated, **but its internal
`import`s and cross-references were not updated to the new module locations**, and the code was **not
adapted to the module-boundary rules**. On top of that, several **supporting artifacts the code
depends on were not brought along** (classpath resources, config keys, DB schema, a test mock), and
a few features were **only half-wired** (frontend hook without its API function / backend endpoint).
Finally, it was **merged without a green-build gate**, so all of this reached `main`.

---

## Defects (grouped)

### A. Compile errors — stale/old package paths & missing symbols
| Defect | File(s) | Fix |
|--------|---------|-----|
| Imports old Limark path `ai.leadplus.infrastructure.springai.SpringAiClient` (that package doesn't exist in the modular tree) | `leadgen/search/service/AiColumnMapperService.java` | Route AI through `AIServicesModule` (the correct modular path) |
| Imports non-existent `ai.leadplus.api.common.UserValidator` | `leadgen/search/controller/ContactController.java`, `TenantContactImportController.java` | Correct to `shared.workspace.controller.common.UserValidator` |
| Repository references its entity `TenantDataSource` but never imports it | `shared/workspace/repository/TenantDataSourceRepository.java` | Add the missing import |
| Uses `DataSource.MANUAL` but the enum only defined `APOLLO` | `leadgen/search/model/DataSource.java` | Add the `MANUAL` value |
| Calls repository query methods that don't exist (`findByEmailInAndActiveTrue`, `findByDomainInAndActiveTrue`, `findByNameIgnoreCaseAndActiveTrue`) | `LeadContactRepository`, `LeadCompanyRepository` | Add the derived-query methods |
| Calls `MailgunEmailService.sendSystemEmail(...)` which was never ported | `leadgen/campaign/service/CampaignSummaryService.java` → `outreach/service/MailgunEmailService.java` | Port the method (from Limark) + expose via `OutreachModule` |

### B. Module-boundary violations (12 total)
Migrated code in `search`, `campaign`, and `admin` reached **directly into other modules' internals**
(`*Service`/`*Repository`/`*Client`) instead of going through the public `*Module` facades — which
`ModuleBoundariesTest` fails the build on.
| Area | Violation | Fix |
|------|-----------|-----|
| `search` `ContactAddService`, `TenantContactImportService` | direct use of admin's `LeadFileImportService` + repos, and workspace's `TenantDataSourceService`, `AwsS3Client`, `TenantContactMetadataRepository`, `TenantDataSourceRepository` | Route through `AdminModule` / `WorkspaceModule` (facade methods added) |
| `campaign` `CampaignSummaryService` | direct use of search's `LeadContactRepository`/`LeadCompanyRepository`, workspace's `TenantContactMetadataRepository`, outreach's `MailgunEmailService` | Route through `SearchModule`/`WorkspaceModule`/`OutreachModule` (entity→DTO conversion) |
| `admin` `AdminCampaignController` | direct use of campaign's `CampaignSummaryService` | Route through `CampaignModule` |

### C. Missing supporting artifacts (caused runtime/boot failures, not compile errors)
| Defect | Detail | Fix |
|--------|--------|-----|
| **Classpath resources not copied** | `EmailPreviewService`/`AiColumnMapperService` load `campaign-summary-email-template-preview/{preview,error}.html` and `prompts/column-mapping-system-prompt.md` at class-init; the files weren't migrated → `ExceptionInInitializerError` crashes boot | Copy the resource files from Limark |
| **Missing config key** | new `spring-ai-starter-model-anthropic` dependency auto-configures an `anthropicApi` bean that requires `spring.ai.anthropic.api-key` at startup; the key was never added → boot fails | Add `spring.ai.anthropic.api-key` (env-overridable) |
| **`schema.sql` not updated** | new entity `TenantDataSource` (table `tenant_data_source`) and new columns `lead_file_import.tenant_id` / `source_label` were not added to `schema.sql`; with `ddl-auto: validate`, prod boot would fail on missing table/columns | Add the table + columns to `schema.sql` |

### D. Test not updated
| Defect | File | Fix |
|--------|------|-----|
| Migration added event publishing to `CampaignEmailService.createBasicCampaignEmail`/`deleteCampaignEmail`, but the test's mock set wasn't updated (missing `ApplicationEventPublisher` mock) → 3 NPE test failures | `CampaignEmailServiceTest.java` | Add the missing `@Mock` |

### E. Incomplete feature wiring (front-to-back)
| Defect | Detail | Fix |
|--------|--------|-----|
| The `useDataSources` React hook + `apiEndpoints.leads.dataSources` entry were migrated into the portal, but **the `getDataSources` API function was never added**, and **the backend `GET /leads/data-sources` endpoint was never added** → the leadgen search page crashed at build ("export getDataSources was not found"), and the call would 404 | portal `lib/api/leadSearch.api.ts`; backend `LeadController` (+ `WorkspaceModule`/`TenantDataSourceService`) | Add the API function + the endpoint (routed through `WorkspaceModule`) |

### F. Process gap (the systemic one)
| Defect | Detail | Recommendation |
|--------|--------|----------------|
| **Merged to `main` with a red build.** All of A–E shipped because CI didn't block a non-compiling / non-booting / test-failing merge. This is the root systemic issue. | PRs #41/#42 | Add a **required CI gate on PRs** (not just push-to-main): `./gradlew test` (compile + unit + `ModuleBoundariesTest`) **and** the full-context boot smoke test (`RUN_CONTEXT_TESTS=true`) must pass before merge. |

---

## Residual items from the migration (not build-breaking, but left behind)
| Item | Detail | Suggested action |
|------|--------|------------------|
| **Unused Anthropic dependency** | The migration added `spring-ai-starter-model-anthropic` + `spring.ai.anthropic.api-key` (part of the "Agent Factory" deps), but **no code uses Anthropic** — all AI runs on OpenAI. It auto-configures an `anthropicApi` bean that *requires* a key at startup (this caused defect M4). We added a dummy key to unblock boot, so it's carrying a mandatory secret for a feature that doesn't exist. | Remove the dependency until Agent Factory actually lands, or disable its auto-config. |
| **Other Agent-Factory deps unused** | `slack bolt-socket-mode`, `javaparser`, `jgit`, plus the SonarCloud / OWASP-dependency-check plugins were added in the same setup commit but have no feature code behind them yet. | Confirm they're intended scaffolding; otherwise trim to reduce attack surface / build weight. |

**Everything in sections A–F above is fixed** (`main` compiles, all tests + boundary test pass, boots
on PostgreSQL in `validate` mode). The two residual items above are follow-ups, not blockers.

---

## Summary checklist for future Limark→Corelabs migrations
When porting a feature from Limark into the modular codebase, verify **all** of these before opening the PR:
1. **Imports updated** to the modular package locations (not the old `infrastructure.*` / `api.common.*` paths).
2. **Module boundaries respected** — no direct `*Service`/`*Repository`/`*Client`/entity imports across modules; go through the target `*Module` facade or an event. Run `ModuleBoundariesTest`.
3. **All referenced symbols exist** — enum values, repository methods, ported helper methods (e.g. `sendSystemEmail`).
4. **Supporting artifacts brought along** — classpath resources (prompts/templates), new config keys, and `schema.sql` changes for any new entity/column.
5. **Tests updated** for new dependencies/behavior (mocks, event publishers).
6. **Feature wired end-to-end** — frontend API function + backend endpoint both present, not just the hook.
7. **Green gate** — `./gradlew test` and the boot smoke test pass locally, and CI blocks the merge otherwise.
