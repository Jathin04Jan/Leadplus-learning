# Independent Verification Audit — Limark → Corelabs migration

**Date:** 2026-07-16 · **Target:** `Corelabs` `main` @ `5d93540` (merge of PR #47)
**Question asked:** the wave-2 migration was reported as *"everything is migrated and it works"* — is it?
**Answer:** the build is genuinely green; the claim is still **false**. Five gaps below, all verified
against the frozen `Limark/` tree, three of them not previously logged.

This report is the *method* + *evidence*. The findings are also filed as rows `V1`–`V5` in
[`ISSUES.md`](ISSUES.md) §10, which stays the canonical register.

---

## 1. What is actually true

Everything the report claimed about *surface* health checks out. Verified by running it:

| Check | Result |
|---|---|
| `./gradlew compileJava compileTestJava` | ✅ green (warnings only) |
| `./gradlew test` | ✅ **560 tests, 0 failures, 0 errors**, 20 skipped |
| `schema.sql` | ✅ 70 tables |
| `uq_lead_company_domain_tenant` / `uq_lead_contact_email_tenant` | ✅ present (`NULLS NOT DISTINCT`) — the W6 fix is real |
| JPA entities/tables vs Limark | ✅ 14/14, no drift |
| `@Scheduled` jobs | ✅ 10/10 migrated |
| Frontend file tree vs Limark | ✅ no Limark-only files |

So this is not a case of a fabricated claim. It is a case of **the gate being unable to see the
defect class**.

## 2. Why "green" proves so little here

Every gap below is invisible to the current gate, and each is invisible for a *different* reason.
That is the point worth internalising — there is no single check to add.

| Gap | Why the build/tests can't see it |
|---|---|
| V1 scheduler default | The kill-switch key is defined **only in the test config**, where it's `false`. Tests exercise the opposite of production. |
| V2 orphaned prop | The prop is **optional** (`isMailboxExpired?: boolean`) → `undefined` typechecks and builds clean. |
| V3 removed feature | Deleting a call site removes the code that would fail. Nothing references it to miss it. |
| V4 dead-branch migration | The migrated path *compiles and is called*. It's only wrong at **runtime, in production, against AWS**. |
| V5 search precision | Returns rows, no error. Wrong results are indistinguishable from right ones without a fixture. |

Surface metrics match *exactly* — 457 mapping annotations both sides, 368/369 routes, 14/14 tables.
That symmetry is what makes the real gaps hard to spot: the numbers all reconcile.

---

## 3. Findings

### V1 — Outreach send-cron is ON by default while the config reads as OFF 🔴

The highest-blast-radius item: it sends **real email**.

- `leadgen/outreach/service/CampaignOrchestratorService.java:47`
  `@ConditionalOnProperty(name = "app.outreach.scheduler.enabled", havingValue = "true", matchIfMissing = true)`
- `app.outreach.scheduler.enabled` **is not defined in `src/main/resources/application.yml`.**
  The `app:` block exists (`:231`) but contains only `google.analytics` and `spring-ai`.
- The **only** definition is `src/test/resources/application-contexttest.yml:51`, which sets it
  `false` — so the boot smoke test runs with the cron **off** and can never surface this.
- The legacy key `campaign.orchestration.scheduler.enabled: false` is **still present**
  (`application.yml:96`) and is **read by nothing**. It reads as intentional-and-safe while inert.
- The cron expression `campaign.orchestration.scheduler.cron` (`0 * * * * MON-FRI`) still resolves.

**Net:** with `matchIfMissing = true` and no key, the bean loads and fires every minute, Mon–Fri.
In Limark this job was **off by default** (`application/campaignorchestrator/CampaignOrchestratorService.java:61`
has no `matchIfMissing`, and its yml sets `enabled: false`). The default was silently inverted.

Partially covered by `C3` ("one flag read nowhere", 🟡). **That severity is wrong** — this isn't a
naming smell, it's an unguarded production send path. Note `Docs/RESUME-HERE.md:34` claims the flag
is "set in application.yml"; it is not.

> **Caveat:** `CLAUDE.md` explicitly documents "defaults to enabled", so the *default* may well be
> deliberate. What is not defensible is the dead `campaign.orchestration.scheduler.enabled: false`
> sitting in the yml implying the opposite. Confirm intent before changing behaviour — then delete
> the dead key either way.

### V2 — Campaign mailbox-expiry guard is dead code 🟠

`useIsMailboxExpired` has **3 call sites in Limark, 1 in Corelabs.** The hook itself survived
byte-identical (`src/hooks/useMailbox.ts:177`); only the callers were dropped.

`campaign/view/_components/Header.tsx` still declares `isMailboxExpired?: boolean` (`:41`) and
consumes it in **six** places — but `campaign/view/page.tsx:46` renders
`<Header campaign={...} onRefresh={...} isRefreshing={...} />` and never passes it. Limark passes it
(`view/page.tsx:51`). The prop is permanently `undefined`:

- `:261` `disabled={isMailboxExpired || !campaign.sendingMailboxId}` → **Launch enabled on an expired mailbox**
- `:288` `disabled={isMailboxExpired}` → **Resume enabled on an expired mailbox**
- `:205` / `:271` → the "reconnect your mailbox" tooltip can never render

`search/_components/EmailComposeSheet.tsx` migrated the same feature *correctly* — so this is a
partial migration, not a deliberate removal.

**Generalise this one.** An optional prop whose parent stopped passing it is a guard that reviews
clean, typechecks, and does nothing. Worth a lint rule or a sweep for other optional boolean props
with no supplying call site.

### V3 — `configure-sequence` lost the expiry guard and the completion redirect 🟠

`campaign/configure-sequence/page.tsx` — Corelabs has **zero** matches for `useIsMailboxExpired`,
`AlertBanner`, or the `COMPLETED` transition. Limark has all three:

- `:407` `useIsMailboxExpired(activeMailboxId)`, passed at `:484` and `:548`
- `:487` an `<AlertBanner>` linking to Communications settings
- `:194` guard narrowed from `disabled={!primaryAction.onClick || (isMailboxExpired && campaignStatus !== RUNNING)}` → `disabled={!primaryAction.onClick}`
- `:250` a `useEffect` + `prevCampaignStatusRef` redirecting to the campaign view on `COMPLETED` —
  without it, users are stranded on the sequence editor for a finished campaign

### V4 — G2 escalation: Corelabs migrated the branch that never ran 🔴 (was 🟠)

`G2` logs the missing templating as *"Not broken (both paths send mail), but a divergence."*
**That characterisation is too soft.** In Limark the SES path is **unreachable**:

- `application/aws/EmailService.java:27` — `private static final boolean IS_MAILGUN_ENABLED = true;`
- `:123-129` the Mailgun + local-HTML branch is therefore the **only** path that ever executes
- `:131` the SES `else` branch is **dead code**

Corelabs migrated **only that dead branch**: `shared/workspace/service/EmailService.java:32-39`
→ `OutreachModule.sendTemplatedEmail` → `awsSESClient`. So the live path in Corelabs is one that was
never exercised in Limark production — it is *unproven*, not merely *different*. Dropped with it:
`SystemEmailTemplate` (which carried the **per-template subject line**), `SystemEmailTemplateRenderer`,
all **11** `resources/email-templates/*.html`, and `BASE_URL` injection (`${aws.cloudfront.url}`) —
so images/links in templates lose their base.

Confirmed: `grep -rn "email-templates" Leadplus-corelabs/leadplus-service/src` → **no matches**.

**Compounding:** `safeSend` catches `Exception` and logs `warn`. Password reset, OTP verification and
workspace invites now fail **silently** — the caller succeeds, the user never receives the mail.
G2 already notes this; combined with the above it means the *entire* system-email surface is
untested, unproven, and fails quietly.

### V5 — Search returns `OR` when the user means `AND` 🔴 (new)

Directly relevant to the "search must be reliable before we go to market" priority. Two independent
defects compound in the **LeadGen lead search** (`leadgen/search`):

**(a) The match mode silently defaults to OR.**
`LeadFilterCriteria.keywordMatchMode` (`leadgen/search/service/LeadFilterCriteria.java:32`) is a plain
`@Builder` field with **no `@Builder.Default`** → `null` when unset. And:

```java
// leadgen/search/service/KeywordPredicateUtils.java:20
return criteria.getKeywordMatchMode() == KeywordMatchMode.ALL
        ? cb.and(predicateArray)
        : cb.or(predicateArray);
```

`null == ALL` is `false` → **`cb.or(...)`**. Unset means OR.

The NL assistant only sets `ALL` on explicit trigger phrases —
`prompts/lead-chat-assistant.md:86`: *"match all keywords", "must include all keywords", "all of these keywords"*.
A natural query like **"SAP manufacturing companies using Snowflake and AWS"** contains none of them.
The conjunction "and" is **not** a trigger. So the mode stays `null` → the query resolves to
`SAP OR manufacturing OR Snowflake OR AWS` — i.e. nearly every company in the pool.

**(b) Terms are substring-matched across six fields, OR'd.**
`KeywordPredicateUtils.aggregateKeywordPredicate:25-35` builds `%term%` `LIKE` over
`keywords`, `technologies`, `scrapedTechnologies`, `scrapedTools`, `scrapedServices` and the `industry`
text — all `cb.or`'d. Consequences:

- No word boundaries: `%sap%` matches **Sapient**, **sapphire**, **NetSuite-sap-connector**…
- A term matching *any* of the six fields counts as a hit, so "manufacturing" as an intended
  *industry* filter also matches a company whose *tools* mention manufacturing.
- `array_to_string(field, ',') LIKE '%…%'` is **non-sargable** → no index is usable. With `D2`
  (essentially no secondary indexes) every such query is a full scan.

**Net:** the flagship demo query is over-broad by construction *and* fuzzy-matched, on a full scan.
This is a precision defect, not a tuning problem — and it is exactly the "queries don't consistently
return the right results" symptom. Fixing (a) is small; (b) is a design change (exact/tokenised
matching per field, `@Builder.Default` → `ALL`, field-scoped terms rather than one aggregate bucket).

Note `W8` restored the *prompt rules* for `keywordMatchMode`, which made the feature reachable — it
did not address the default or the matching semantics. Those are pre-existing, not migration damage.

---

## 4. Verified clean (no action)

- **Entities/tables** — 14/14 identical.
- **Schedulers** — 10/10. The Azure/Gmail reply-sync cron consolidation to `${reply-tracking.poll-cron}`
  resolves correctly (`application.yml:205-206`); their `@ConditionalOnProperty` still reads the
  original `azure.scheduler.enabled` / `google.scheduler.enabled` (both `true`).
- **Controller renames** — `AdminApolloLead{Company,Contact}Controller` → `ApolloLead*Controller`,
  and the `UnsubscribedContactsController` extraction: routes preserved, cosmetic.
- **`formatReasonsHtml`** — unused in *both* trees; pre-existing dead code, not a regression.
- **Prior findings confirmed fixed** — `ImportHistoryTab` is now wired
  (`tenant/settings/page.tsx:7,28`); the announcement verified-sender guard is present in
  `CreateAnnouncementDialog.tsx`.

## 5. Corelabs is *ahead* of Limark here — do not "restore" these

Flagged so nobody reverts them while closing the gaps above:

- `admin/(dashboard-protected)/layout.tsx` — adds `ProtectedRoute allowedRoles={[ADMIN]}`; **Limark had no role check.** Security hardening beyond the reference.
- `GoogleAuthButton.tsx` — guards missing `NEXT_PUBLIC_GOOGLE_CLIENT_ID` (Limark error-boundaries the login page).
- `CampaignSettings.tsx` — validates `windowEnd > windowStart`; Limark only checks `!==`, so Limark accepts an end *before* start.
- `ModuleProtectedRoute` / `DashboardShell` — add `publicModules`, `hideWorkspaceSwitcher`.

`ImportPreviewTable.tsx:220` row labelling differs (`rowNumber` vs `rowNumber + 1`) — the backend
assigns `i + 1` (already 1-based), so **Corelabs is correct and Limark has the off-by-one.**
Deliberately not synced.

---

## 6. Method (repeatable)

What actually found things, in rough order of yield:

1. **Compare call sites, not definitions.** `grep -rn "<symbol>"` in *both* trees and diff the
   counts. V2/V3 were found this way — the definitions were present and identical; only the callers
   were gone. A definition-level diff shows nothing.
2. **Diff classpath resources**, not just `.java`: `find src/main/resources -type f | sed 's|.*/resources/||' | sort`, then `comm -23`. V4's 11 templates surfaced instantly.
3. **Diff `@ConditionalOnProperty` keys against what's actually defined in yml** — in *both* main and
   test configs. A key defined only in test config is a red flag by itself (V1).
4. **Check the reference's *reachability*, not just its presence.** Limark's `IS_MAILGUN_ENABLED = true`
   made an entire branch dead; migrating the dead branch is worse than migrating nothing (V4).
5. **Read enum/flag defaults against their consumer.** `null == ALL → false → OR` is invisible unless
   you read both sides together (V5).
6. **Routes vs service methods.** A surviving service method with a passing test and no route is
   invisible to the suite — the test calls the service directly (G1).

## 7. Recommended order

1. **V1** — one-line config decision, live email exposure, cheapest fix. Do first.
2. **V5(a)** — `@Builder.Default` → `ALL`. Small change, directly unblocks the GTM wedge. V5(b) is a
   design conversation, not a patch.
3. **V2 / V3** — restore the two dropped call sites and the redirect.
4. **V4** — decide the direction (port Limark's local renderer vs. commit to SES + provision the
   templates + stop swallowing failures). Requires a ruling in `Docs/SCOPE-DECISIONS.md`.
5. **G1** — the fix already exists on `jathin/fix-campaign-delete-endpoint`; it just needs merging.
   `ISSUES.md` marks it 🟢 while it is **not on `main`** — worth a status convention for
   "fixed on a branch" vs "fixed on main".

## 8. The structural conclusion

Three consecutive waves have now been declared complete and been found incomplete
(`M0-M7`, `W1-W8`, `G1-G3`, and now `V1-V5`). The diagnosis is not the bottleneck — this register is
better than most teams'. The gate is. Every gap above passes `compileJava`, `compileTestJava`,
`./gradlew test` (560 green), `ModuleBoundariesTest`, `npm run typecheck` and `npm run build`.

That is the concrete argument for the gates in `Docs/AI-AUTOMATION-PLAN.md` §6 — specifically the
**Hercules end-to-end suite**, which is the only proposed gate that would catch V2, V3 and V5, and a
**config-drift check** (every `@ConditionalOnProperty` key must be defined in the *non-test* config),
which is the only thing that would have caught V1. V1–V5 are five pieces of evidence that
"all green" is not a merge criterion for this codebase.
