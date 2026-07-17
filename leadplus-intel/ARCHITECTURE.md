# LeadPlus Intent Search — Architecture Spec

> **For the agent building this:** this document is the complete spec. Build it in the order given
> in §13. Do not skip §4 (design rules) — every rule there exists because the current production
> system violates it and is broken as a result. Do not "improve" the design by adding an LLM to the
> ranking path, an ANN index, or chunking; §4 explains why each is wrong *here*.

---

## 0. Context — read first

LeadPlus is a B2B lead-generation platform. Its existing lead search (Java/Spring, `leadgen/search`
module) is unreliable, and the team's GTM wedge depends on fixing it. Three diagnosed defects in the
shipped system:

1. **"and" means OR.** `keywordMatchMode` defaults to `ANY` at three layers; the NL assistant only
   sets `ALL` on literal phrases like "match all keywords". So *"Snowflake and AWS"* returns
   Snowflake **or** AWS.
2. **Substring matching, no word boundaries.** `LIKE '%sap%'` matches **Sapient** and **sapphire**.
   Terms are matched across six columns, all OR'd, via `array_to_string(...) LIKE` — non-sargable,
   so every query is a full scan.
3. **No relevance ranking at all.** Results sort by `updated_at DESC`. A company matching all three
   terms ranks below one matching one term that was enriched yesterday.

**This project does not fix that system.** It is a **separate MVP** proving a different thesis.

### The thesis

LeadPlus scrapes company career pages into `lead_company_job` — job postings with `description`,
`skills[]`, `requirements[]`, `technologies[]` and `posted_date`. A pipeline
(`aggregateTechToCompany`) then flattens every posting into three deduped string arrays on the
company row (`scraped_technologies`, `scraped_tools`, `scraped_services`) and **throws the rest away** —
losing which job, when it was posted, how many, and the description text.

Job postings are **buying signals**. A company posting "Senior SAP S/4HANA Architect" six days ago is
actively investing. That is a question Apollo and ZoomInfo structurally cannot answer — they index
company attributes, not hiring narratives.

**We rebuild search over that discarded signal.**

### THE CORPUS IS NOW REAL — and it invalidates several premises below

**Superseded:** the synthetic-replica deviation table that stood here. `leadplus_local` is now a
verified 1:1 clone of `leadplus_dev` (72 tables, 244,659 rows). Re-measured:

| Spec assumed | Real corpus |
|---|---|
| 22,126 jobs / 23,062 companies | 22,251 jobs (**13,082 active**) / 23,063 companies (**22,966 active**) |
| job ads carry text | **only 2,886 active jobs have a description >200 chars** — ~78% are stubs |
| `posted_date` populated | **125 of those 2,886.** The recency axis is mostly dead. Scraper gap. |
| copy-on-write duplication (2–3×) | **barely populated** — 28 tenant rows total. See §5.4's note. |
| `lead_query` COMPANY_INDUSTRY = 16,870 | **91 rows** |
| ingest $20–40 / ~2h | **$3.79 / ~2.5h** for 2,886 jobs + 487 companies |
| `lead_company_job_intent` (125 rows) | **exists, 451 rows** — another team's. See §5.8. |

**The premise this most damages — read before writing a prompt or reading a result:**

> **This corpus is not manufacturing.** Of the 2,886 extractable postings:
> **49% Hospitals and Health Care**, 15% Retail, 13% Financial Services, 7% Transportation &
> Logistics — and **7% manufacturing/industrial across all variants combined.**

Every example in §1, §5.2 and §9 is a manufacturer migrating SAP ECC. That describes **7%** of
what the pipeline actually reads. The thesis (job postings are buying signals) is unaffected —
a hospital modernizing its revenue cycle is exactly as much of a signal as a plant migrating its
ERP — but the *vocabulary*, the *examples* and the *golden set* were all built for the wrong
domain, and `prompts/job_normalizer.md` v6 had to be corrected accordingly (it told the model it
was reading manufacturing postings; it is mostly not).

Consequences already realised:
* `evals/golden.yaml` is **dead** — its labels are synthetic company ids that now point at real
  companies chosen at random. `scripts/eval.py` hard-fails rather than score it.
* The corpus is **multilingual** (German is common).

### ⚠️ The clone contains seeded demo data, and it is load-bearing in the worst way

`leadplus_dev` carries **25 fabricated companies** — `tenant_id = 29`, names prefixed
`Synthetic `, domains ending `.example` (RFC 2606, reserved and unresolvable). They own **125 of
the 2,886 extractable postings (4.3%)**, and their descriptions are one ERP template:

> *"Synthetic Northstar Components is hiring an ERP Transformation Program Manager to coordinate
> a multi-year review of the systems supporting its manufacturing operations… document the
> current SAP ECC landscape…"*

**Two things follow, and both matter more than the 4.3% suggests.**

**1. `posted_date` is 100% synthetic within the indexed corpus. The recency axis is not "mostly
dead" — it is dead, and what remains alive is fiction.**

```
extractable postings (desc > 200 chars) with a posted_date : 125
   ... belonging to synthetic companies                    : 125
   ... belonging to real companies                         :   0     <-- ZERO
```

Real dated postings do exist (22 active, 62 total) — but **not one of them carries a description
over 200 chars**, so not one is extractable. So every non-NULL `job_signal.posted_date` in this
index belongs to a company that does not exist.

The consequence is a silent, confident lie: **any query with `since_days` can only return
synthetic companies**, because they are the only rows that can pass a date filter. "Manufacturers
hiring for SAP in the last quarter" returns a ranked list of fictions, with evidence and
paraphrases, at high confidence. That is precisely the failure mode this project exists to end.

It also means the **thesis is currently unprovable on this data**. §0's wedge is *"a company
posting this six days ago is actively investing"* — recency of hiring signal. Zero real postings
carry a date. `HIRING` mode weights `recency` at **0.40**, its largest single weight; on real data
that term is 0.0 for every company. This is **not a scraper gap to be waved at** — it is the
finding: the scraper has never once captured a date on a posting it also captured text for.

**2. The other team's `lead_company_job_intent` describes ONLY these 25 fictions.**
All 451 rows, all 92 jobs, all 25 companies — every one `.example`. See §5.8.

**Neither is patched here.** The 2,886 are ingested as briefed, synthetic rows included, because
the honest move is to surface this rather than quietly drop rows and report a clean number. They
are trivially identifiable (`lead_company.domain LIKE '%.example'` / `tenant_id = 29`), so
excluding them is a one-line predicate in `fetch_jobs_to_normalize` — but whether demo data
belongs in `leadplus_dev` at all is a decision for whoever put it there, not a thing to paper
over in a derived index.

This build proves the architecture and the three defects are fixed. Whether job-intent is a
*product* is now answerable on real data for the first time — but not from this document.

---

## 1. What we're building

A standalone web app: one search box → ranked **companies**, each with the evidence that explains it.

```
"manufacturing companies hiring for Snowflake, last quarter"
  → Acme Industrial  ·  score 0.87
      3 matching roles, most recent 6 days ago
      "Senior SAP S/4HANA Architect"
      → Manufacturer migrating ECC to S/4HANA with a Snowflake data layer.
```

That last line — a normalized paraphrase written at ingest — is the product. It's why the SI trusts
the result without reading a job ad.

---

## 2. Non-goals

- **Not** a replacement for the Java lead search. Additive, separate, no shared code.
- **Not** a general lead database. Apollo has ~60M companies. We lose any head-to-head on coverage.
  We win on one question they can't answer.
- **Not** RAG. Nothing is generated at query time. We retrieve and rank *companies*.
- **Not** production. No auth, no multi-tenancy, no data-pack gating. The lab isolates the search
  question. A real tenant would see a gated subset — say so in the UI.
- **Never writes to LeadPlus tables.** `lead_company*` belongs to the Java `search` module. We read
  them and write only our own derived tables.
- **Not a people search. `lead_contact` stays excluded** — see §3's note. CHANGES-v2 §6 proposed a
  `contact_signal` index and it was **skipped on measurement, not on principle**: the two gates
  that would have made it useful are zero on this corpus (§0). Nothing in this app answers a
  question about a named person, and the `PEOPLE` term source, `ContactFunction`,
  `ContactSeniority` and `result_mode` are deliberately unbuilt.

---

## 3. The data

Source: local Postgres (`leadplus_local`, :5433). **Never query RDS from the app.**

| Table | Use |
|---|---|
| `lead_company_job` | job postings — the signal |
| `lead_company` | firmographics + Apollo technographics |
| `lead_query` | existing controlled vocabulary (industries etc.) |
| ~~`lead_contact`~~ | **excluded** — PII, and unnecessary here. Still true; see below. |

### `lead_contact` is now indexed — a role census (`contact_signal`). See SEARCH-EXPLAINED §9.

CHANGES-v2 §6 proposed indexing a **role census** (`title`, `department`, `seniority`, normalized
title tokens — never names, emails, phones or LinkedIn) so the app could answer *"companies whose
CFO arrived from a Big-4 firm"*. It was **skipped** on two gates that measured **zero** — but that
measurement was taken on the SYNTHETIC seed and was wrong. Re-run on the real clone, both gates
pass decisively, so the census is now **built**:

| Gate | Question | Synthetic (wrong) | **Real clone** |
|---|---|---|---|
| **A** | Does `apollo_contact_data` carry `employment_history`? | 0 / 518 | **4,659 / 36,145** |
| **B** | Are there CFO / VP-Finance contacts to return? | 0 / 1,242 | **20 CFOs, 906 finance roles, 306 finance leaders (C/VP)** |

`contact_signal` (§7) holds **53,746** role rows across 13,539 canonical companies — a title, a
`ContactFunction`, a `ContactSeniority`, a department, a Big-4-alumnus flag and the current-role
start date (`landed_at`), plus a 3072-dim embedding of a role sentence. It stores **no**
identifying field: no `first_name`, `last_name`, `full_name`, `email`, `phonee164`, `linkedin_url`
or `notes` — they are never SELECTed, so they cannot leak (`\d contact_signal` is the proof). It
is the 4th document type (§6), fused through the same company-level RRF, and it is consulted only
in `PEOPLE` result mode — a `COMPANIES` query retrieves byte-identically to before it existed.

Two things stayed exactly as the exclusion note warned:
- **Honest caveat:** "the CFO of Acme" is *pseudonymous*, not anonymous — a role is a person when
  the company is small enough. The census is data-minimised, not de-identified.
- **`ContactFunction`/`ContactSeniority` are a SEPARATE vocabulary** from the job enums. The job
  `Function` was **not** widened with `FINANCE`: job enums describe requisitions, people enums
  describe people, and merging them is how a schema starts lying about what it holds.

Honest limits found on the real data: (1) `lead_contact_normalized_title` is **empty** (0 rows) on
this clone, so §9's "reuse it" cannot apply — function/seniority are derived deterministically from
the title text instead. (2) The Big-4 flag comes from a **prior** (non-current) `employment_history`
employer: 19 such alumni. `employment_history` carries `start_date`, so "recently landed" is real
(current-role start), but the clone's most-recent starts are ~2022-2024, so "recently" is relative
to the snapshot. (3) None of the 19 Big-4 alumni also carry a "transformation" current title, so
that query is satisfied by the OR of the two role signals, not their intersection.

### `lead_company_job` (relevant columns)

```
id · lead_company_id · active · title · description (text) · department · location
type · posted_date · apply_url · job_url
skills[] · requirements[] · benefits[] · technologies[] · tools[] · services[]
```

### `lead_company` (relevant columns)

```
id · name · domain · industry (free-text varchar) · hq_city/state/country · region
employee_count · employee_range · revenue_usd · sic_codes[] · naics_codes[]
keywords[] · technologies[]                          ← Apollo, curated
scraped_technologies[] · scraped_tools[] · scraped_services[]   ← the flattened job rollup
active · exclusion · tenant_id
```

**Ignore `salesperson_name`, `notes`, `account_summary`** — internal free-text, may contain personal data.

---

## 4. Design rules — non-negotiable

Each of these exists because violating it is what broke the current system.

1. **LLM at the edges, deterministic in the middle.** LLM does exactly two things: normalize
   documents at ingest, and parse the query into filters. **Never** in the ranking path. An LLM
   ranker means the same query returns different results on different runs — precisely the
   "search isn't consistently reliable" complaint we're fixing.

2. **Filter on facts, rank on fuzz.** Hard filters: `posted_date`, employee/revenue range.
   **`industry` is NOT a filter** — it's free-text (`"Industrial Machinery"` vs the user's
   `"manufacturing"`). Hard-filtering it silently deletes correct answers before ranking sees them.
   It becomes a **soft multiplier** (§8.5). **Terms are never filters.** They feed coverage (§8.4).
   3-of-3 outranks 1-of-3; nothing is dropped. This kills the AND/OR cliff permanently.

3. **Retrieve documents, return companies.** Two document types — job postings ("investing in X")
   and company profiles ("uses X"). Evidence is a document; the answer is always a company. Fusion
   happens at company level (§8.1).

4. **Symmetric normalization.** Run the *query* through the same normalizer as the documents before
   embedding. Compare signal-to-signal in one vocabulary.

5. **Enums where closed, canonicalise where long-tail.** Free-texting technologies gives you
   `SAP S/4HANA`, `S/4 HANA`, `SAP S4`, `S4/HANA` — and you're back to substring matching.

   > **This rule is about NAMED PRODUCTS. It does not say "canonicalise everything long-tail",
   > and reading it that way cost us a table.** The example above is a technology, and that is
   > load-bearing: `SAP S/4HANA` and `SAP S4` are one thing *with an official form*, so
   > canonicalising **recovers** an identity that already exists. A **descriptive phrase** has no
   > official form — `icd-10 coding accuracy`, `embedded electronics design` — so canonicalising
   > it **invents** one.
   >
   > We applied this rule to `job_intent` anyway. Measured: **5,209 distinct phrases in 8,114
   > rows** (nearly every intent unique), **3.9% resolved**, nearest-match similarities
   > **0.32–0.52**, and a **5,195-row review queue of things that were never broken**. The column
   > and both vocabulary tables were **removed**; intents are matched semantically via
   > `intent_embedding`. See §5.8. **`tech_canonical` stays** — it is the case this rule is for,
   > and its ladder catches real traps (`ROS`→`ROSS` @ 0.79).
   >
   > The test before canonicalising anything new: **does this thing have an official name?** If
   > yes, canonicalise. If it is a description, embed it.

   > ### ⚠️ THE INVARIANT — a vocabulary must not contradict itself
   >
   > **No phrase may be both a canonical term and an alias of a different term.**
   >
   > Enforced in `scripts/bootstrap_tech.py` (`_alias_owners` fails the run on a self-contradictory
   > `SEED_TECH`; `reconcile` makes the table obey it; `verify_invariant` asserts zero collisions
   > afterwards) and mirrored in `score.py`'s `TermMatcher`. It is not a style rule — violating it
   > broke this vocabulary in **both directions at once**, and neither failure raised anything.
   >
   > **How it happened.** `tech_canonical` is built from two sources that disagree: the hand-seeded
   > `SEED_TECH` (`"AWS": ["Amazon Web Services", "Amazon AWS"]`, `"SAP": ["SAP ERP", "SAP ECC",
   > "ECC", "SAP R/3"]`) **union** the ~4,529 distinct values of Apollo's
   > `lead_company.technologies[]` — which carry `Amazon AWS` and `SAP ECC` **as values of their
   > own**. So 19 phrases ended up as both a term and someone else's alias. The stage-3 ladder
   > tries **exact before alias**, so each one matched its own standalone term and *never reached
   > the alias step*. The alias was dead code that looked like configuration.
   >
   > **Measured, before the fix:**
   >
   > ```
   > fragmentation: 65 companies use AWS -> 40 stored as `Amazon AWS`, 25 as `AWS`, ZERO overlap.
   >                A live search for AWS returned 25 of 65. The other 40 were invisible.
   > conflation:    query `SAP ECC` resolved to canonical `SAP`. Top 5 showed coverage=1.00 and
   >                NOT ONE carried SAP ECC. The 20 companies that genuinely run ECC matched
   >                nothing.
   > ```
   >
   > Read those together: the same defect **split one product into two** and **merged two products
   > into one**, silently, at once. Both are rule 5's own failure mode — `SAP S/4HANA` vs `SAP S4`
   > — reappearing one layer up, in the vocabulary that exists to prevent it.
   >
   > **The seed is the classification, and it is the only place to look.** Declaring a phrase as an
   > alias means MERGE (the standalone term is deleted and its rows migrate to the owner: same
   > product). Not declaring it means SPLIT (the standalone stands alone and the stale alias is
   > pruned off whoever claimed it: different product). `bootstrap_tech.py` reads that decision and
   > carries it out; nothing is hand-listed.
   >
   > **The SAP ruling, because it is the product question.** `SAP` is the vendor/generic; `SAP ECC`,
   > `SAP ERP` and `SAP R/3` are the legacy on-prem products; `SAP S/4HANA` is the modern successor.
   > These are **not** spellings of one thing, so they are SPLIT — and Apollo agrees, since 2
   > companies carry ECC *and* ERP, which is only possible if they are different attributes. On this
   > corpus SAP=10, SAP ECC=20, SAP ERP=6, S/4HANA=1 — four different sets. §0's wedge is *"who is
   > still on ECC and therefore has a migration ahead of them?"*; folding ECC into SAP destroys the
   > only question this tool exists to ask, which is why the conflation above was worse than a wrong
   > answer — it was a confident one. (The bare acronym `ECC` belongs to `SAP ECC`, not to `SAP`.)
   >
   > **Honest footnote on the SAP ECC result:** all 20 ECC companies are the seeded `.example`
   > fictions of §0. The fix is still correct and necessary — it replaced five confidently wrong
   > answers with correct ones — but on *this* corpus a SAP ECC query can only return demo data, for
   > the same reason `since_days` can (§0). The AWS half is real: of the 65, **45 are real
   > companies**, and 40 of the 65 were unreachable before.
   >
   > **The guard must use the ladder's own key.** A string-equality collision check finds 18 and
   > misses the 19th — `Oracle E Business Suite` vs the alias `Oracle E-Business Suite`, which differ
   > only by punctuation `tech_key` strips. `repository.tech_alias_collisions` compares on
   > `tech_key`, because a guard that parses its input differently from the thing it guards is not a
   > guard (`config.assert_local_database` learned this the same way).

6. **No chunking.** A job ad is 2–5KB and already the natural unit.

7. **No ANN index.** Exact brute-force cosine over the filtered set is faster *and more accurate* at
   this size. ANN composes badly with pre-filters. Revisit at ~1M rows.

   > **Two updates from the real corpus, and the second is a warning.**
   >
   > **(a) The rule is now enforced by pgvector, not by discipline.** At `vector(3072)` an ANN
   > index is not merely inadvisable, it is impossible — verified:
   > ```
   > CREATE INDEX ... USING hnsw    -> ERROR: column cannot have more than 2000 dimensions
   > CREATE INDEX ... USING ivfflat -> ERROR: column cannot have more than 2000 dimensions
   > ```
   >
   > **(b) "Revisit at ~1M rows" is the wrong trigger. Watch BYTES, not rows.** A `vector(3072)`
   > row is ~12KB, so `job_intent` reached **20MB at 500 jobs** and the six-list `/structured`
   > path already measured **retrieve_ms ≈ 77 (total 122ms)** — over §6's <100ms budget, at 17%
   > of the corpus. The budget is broken long before 1M rows, because the 3072 switch doubled the
   > bytes per vector and `job_intent` added ~3–5 vectors per job on top.
   >
   > This is a real cost of §5.8's decision, and it has no easy exit: the usual escape (an ANN
   > index) is the thing (a) just made illegal. The honest options are to halve the dims (losing
   > the cross-source comparability that motivated 3072), pre-filter harder before the scan, or
   > accept a slower `/structured`. **Do not "solve" it by reaching for an index — measure first.**

8. **No Elasticsearch.** A second datastore that can silently drift from Postgres.

---

## 5. Ingest pipeline (offline, idempotent, resumable)

### 5.1 Two document types

The motivating query is *"companies **using** Snowflake and AWS"* — but job postings answer
*"companies **hiring for** Snowflake"*. Different questions. Indexing only jobs means the system
cannot answer the query that started this project. With both: RRF fuses them, a company matching
**both** ranks top, and a tech-only match still surfaces — just lower.

### 5.2 Job pipeline

**Stage 1 — Extract.** Batches of 100, keyset pagination, joined to company context.

```sql
SELECT j.id, j.lead_company_id, j.title, j.description, j.department, j.location,
       j.type, j.posted_date, j.skills, j.requirements, j.technologies, j.tools, j.services,
       c.name AS company_name, c.industry, c.employee_range
FROM lead_company_job j
JOIN lead_company c ON c.id = j.lead_company_id
WHERE j.active AND c.active
  AND j.id > %(cursor)s
  AND NOT EXISTS (
    SELECT 1 FROM job_signal s
    WHERE s.job_id = j.id AND s.prompt_version = %(prompt_version)s AND s.model = %(model)s
  )
ORDER BY j.id
LIMIT 100;
```

The `NOT EXISTS` makes it resumable and cheap to re-run: bump `prompt_version` and only the delta
re-processes. **Never re-run the corpus to fix 50 rows.**

**Stage 2 — Normalize (LLM).** One call per job, OpenAI structured outputs against the schema:

```python
class SignalRecord(BaseModel):
    initiative: Initiative
    function: Function
    seniority: Seniority
    engagement_type: EngagementType
    technologies: list[str]        # raw extraction; canonicalised in stage 3
    paraphrase: str                # 1–2 sentences, signal only, no boilerplate
    confidence: float              # 0..1
```

Concurrency 20, exponential backoff. Failures → dead-letter table with the raw response.
**Do not let one bad row kill the batch.**

**Stage 3 — Canonicalise technologies.**

```
raw term "SAP S/4 HANA"
   ├─ exact match (lowercased, punctuation-stripped) vs tech_canonical  → hit
   ├─ alias match vs tech_canonical.aliases                              → hit
   ├─ embedding nearest-neighbour, cosine > 0.85                         → hit + record alias
   └─ else → tech_review_queue (occurrences++)   ← human resolves, never auto-guess
```

**Bootstrap `tech_canonical`** from the distinct values in `lead_company.technologies[]` (Apollo's
list is curated) plus a hand-seeded list of SI-relevant platforms.

**Stage 4 — Embed.** `text-embedding-3-large`, **3072 dims**, on the **paraphrase only** (and on each
intent phrase, §5.8) — never the raw description. Batch 100 texts per call.

3072, not 1536/-small, so that ONE query vector compares against `job_signal`, `company_signal`
AND `job_intent` — including the other team's 3072-dim table if the two are ever UNIONed. Two
dimensionalities would mean two query embeds and no cross-source cosine. Note this also makes
rule 7 mandatory rather than merely correct: pgvector's ivfflat/hnsw cap at 2000 dims and would
refuse the index anyway.

**Stage 5 — Load.** Single upsert keyed on `job_id`, so a crash mid-batch leaves no partial rows.

### 5.3 Company pipeline

Same shape, structured columns only. The LLM *verbalises* facts into a searchable narrative in the
same vocabulary as job paraphrases (rule 4). Output: canonical `technologies[]` + a paraphrase like
*"Mid-size industrial machinery manufacturer in Ohio running SAP ECC, Salesforce and AWS."*
**Explicitly ignore** `notes`, `account_summary`, `salesperson_name`.

### 5.4 Canonical company resolution — run BEFORE anything else

LeadPlus uses copy-on-write: `lead_company` holds a shared row (`tenant_id IS NULL`) *and* a
per-tenant copy for every tenant that touched the company — same domain, different `id`. Ingest
naively and the same real company is indexed 2–3× and returns 2–3×.

Fold by `lower(domain)`, preferring the shared row, else lowest id. Companies with no domain are
their own canonical row. Then everywhere downstream: `job_signal.company_id` = **canonical id**
(do **not** filter jobs to shared companies only); `company_signal` is built once per canonical id,
unioning `technologies[]`/`keywords[]`/`scraped_*` across all `member_ids`.

**Verify:** `count(*) FROM company_canonical` should be **less** than `count(*) FROM lead_company
WHERE active`. If equal, copy-on-write isn't populated in this restore and the fold is a no-op —
**fine, but confirm rather than assume.**

**MEASURED ON THE REAL CORPUS — the fold works, but not for the reason this section gives.**

```
active lead_company rows : 22,966
canonical companies      : 22,901      -> 65 rows collapsed, across 60 domain groups
```

So it is **no longer a no-op** (it was, on synthetic). But the premise above is still mostly
wrong: **copy-on-write is barely populated** — only 28 of 22,966 active rows carry a
`tenant_id`, and of the 60 multi-member groups, **58 are duplicate SHARED rows** (two
`tenant_id IS NULL` rows, same domain) and only **2** are the shared+tenant pattern §5.4
describes. The fold is catching plain duplicate companies, not copy-on-write.

It still earns its place, and here is the whole of its effect on the working set — one company:

```
1476  Berkeley Bowl Produce Inc.   berkeleybowl.com   10 text-bearing jobs
1477  Berkeley Bowl Marketplace    berkeleybowl.com   10 text-bearing jobs
```

One real grocer, scraped twice under two names. Without the fold it is indexed twice, ranks
twice, and its 20 postings look like two companies' worth of hiring. With it: canonical 1476,
`member_ids [1476, 1477]`, 10 jobs remapped. **488 text-bearing companies -> 487.**

That is a small win, honestly reported: the fold prevents one visible duplicate here. Keep it —
it is cheap, it is correct, and the duplication it catches is real — but do not cite §5.4's
"indexed 2–3×" as a reason it matters on this data. It isn't true here.

### 5.5 Industry canonicalisation

Map free-text `industry` onto a canonical value so §8.5's multiplier has something exact to compare.
**Reuse `lead_query WHERE type = 'COMPANY_INDUSTRY'`** — the list the Java system already injects as
`{{INDUSTRY_LIST}}`. Do not invent a second taxonomy. Store `industry_raw`, `industry_canonical`,
`industry_embedding`.

### 5.6 Repost detection

`title_norm = lower(trim(title))`; mark `is_repost` where the same `(company_id, title_norm)` exists
within 90 days **and** paraphrase cosine > 0.95. MVP shortcut: `volume` counts `DISTINCT title_norm`.

### 5.7 Operational requirements

| Concern | Requirement |
|---|---|
| Idempotency | key = `(job_id, prompt_version, model)`. Re-running is a no-op. |
| Resumability | keyset cursor + checkpoint. Kill and restart at any point. |
| Failure isolation | per-row try/except → dead-letter. Never abort the batch. |
| Rate limits | concurrency 20, exponential backoff, jitter. |
| Cost control | `--limit N` and `--dry-run`. **Always sample before the full run.** |
| Prompt versioning | `prompt_version` in every row. Changing a prompt is a data migration. |

### 5.8 The intent grain — adopted from another team (`job_intent`)

Another team at Limark built `lead_company_job_intent` (451 rows, 92 jobs, 25 companies). Their
extraction grain is **better than ours in one respect and we adopted it**: ~5 short intent
phrases per job (`erp transformation program`, `sap ecc to snowflake pipelines`) instead of our
single paraphrase. Their vectors are 3072-dim (`text-embedding-3-large`), which is why §5.2
stage 4 and the whole schema moved to 3072.

> **⚠️ Their 451 rows describe ZERO real companies.** All 25 of the companies they cover are the
> seeded fictions from §0: `tenant_id = 29`, `Synthetic *`, `.example` domains. Checked directly:
>
> ```
> their companies: 25 | name LIKE 'Synthetic %': 25 | domain LIKE '%.example': 25
> ```
>
> This does **not** retract the grain — see below — but it retracts every *measurement* offered
> in support of it. "Verified description-driven, not title-templated: 25 same-title jobs produced
> 11 distinct intent-sets" was measured against **generated** descriptions from one ERP template.
> It shows the extractor varies its output when a generator varies its input. It does not show
> the grain survives a real job ad, and it cannot: their extractor has never read one.
>
> It also fully explains §5.8's resolution result below. Their vocabulary does not fail to
> transfer because our corpus is *different* from theirs; it fails because **theirs is not a
> corpus.**

**The grain is still right, and it is adopted on its own evidence, not theirs.** The 25-row gate
(§13 phase 3) ran it against real postings — healthcare coders, German apprenticeships, robotics
engineers — and it held: intent-sets tracked the descriptions, same-title jobs diverged, and
postings that named no work correctly returned an empty list. That is the evidence for the
decision. Their table was the *idea*; the real corpus was the test.

Adopting 3072 dims is likewise justified on its own merits (one query vector across
`job_signal`/`company_signal`/`job_intent`) — and NOT by "matching theirs", which is matching a
table built on fiction.

**We keep the paraphrase.** It is the UI evidence line and the product (§1). The two are
different grains, not competitors: a paraphrase *explains* a company to a salesperson; an intent
phrase *matches* a query that names an initiative. **One LLM call emits both** — they are two
readings of one document, so a second call would pay twice and let the readings disagree.

**What we add: provenance.** Their table has none (`id, job_id, intent, intent_embedding,
created_at, updated_at`), so it cannot say which rows came from which prompt or model, or whose
they are. `job_intent` carries `prompt_version`, `model` and `source` — which makes a UNION of
the two tables safe, and makes a bad prompt version findable and re-runnable (§5.7's key).

**`lead_company_job_intent` is THEIRS. Read-only, like `lead_company*`. Never write it.**

### ⛔ INTENTS ARE NOT CANONICALISED — the category error, and its removal

**This was built, measured, and removed. Do not re-add it.** `intent_canonical` (the column and
the table), `intent_review_queue`, the 80-phrase seed from the other team and the intent ladder
are **gone**. What follows is why, so nobody rebuilds them from rule 5.

**The error was reading rule 5 as "canonicalise everything long-tail".** It says *enums where
closed, **canonicalise where long-tail*** — and its worked example is a **technology**. That is
the whole distinction:

* A **technology is a named product.** `SAP S/4HANA` / `S/4 HANA` / `SAP S4` really are one thing
  with an official form. Snapping them together is *recovering* an identity that exists.
  `tech_canonical` is correct and load-bearing — keep it.
* An **intent is a descriptive phrase.** `icd-10 coding accuracy`, `drg assignment validation`,
  `embedded electronics design`. **There is no official form to snap to.** Canonicalising it is
  *inventing* an identity that does not exist.

Applying the technology ladder to intents was a **category error**, and the corpus said so:

```
job_intent                  : 8,114 rows / 5,209 DISTINCT phrases  <- nearly every intent unique
resolved by the ladder      :   317 (3.9%)          NULL: 7,797
intent_review_queue         : 5,195 rows of things that were NEVER BROKEN
nearest-match similarities  : 0.32 - 0.52  -- correctly nowhere near the 0.85 threshold
    icd-10 coding accuracy   -> record-to-report documentation      0.318
    personal care services   -> cost quality continuity partnership 0.328
    patient care delivery    -> delivery plan management            0.538  (shares one word)
```

Read the shape of that: **5,209 distinct phrases in 8,114 rows.** A vocabulary is worth having
when many observations collapse onto few terms. Here almost nothing repeats, so there is nothing
to collapse — the ladder could only ever refuse (correctly, 96.1% of the time) or force a
resemblance into an identity. The 5,195-row review queue was the tell: **a queue of things that
were never broken**, asking a human to "fix" phrases that were already exactly right.

**The other team was right.** Their `lead_company_job_intent` is `id, job_id, intent,
intent_embedding, created_at, updated_at` — **no canonical column at all.** They did not make
this mistake, and adopting their grain (§5.8) should have included adopting its shape.

**How intents are matched: semantically, via the 3072-dim `intent_embedding`.** That is what the
vector is for, and it is why the removal costs nothing — retrieval never read `intent_canonical`.
`icd-10 coding accuracy` and `icd-10 coding compliance` are near neighbours *in the embedding
space*, which is the correct place to express "these are similar" — because it says **similar**,
with a number, rather than asserting they are the same thing. A canonical column can only say
"identical" or "unknown", and for a descriptive phrase both are usually wrong.

**The nearest-miss list above is not an argument for a review queue — it is an argument against
the column.** Forcing `patient care delivery` onto `delivery plan management` would be a
resemblance promoted to an identity: defect #2 of the system this project replaces, rebuilt
inside the replacement, in the one place nobody would look. The way to never do that is to not
keep a vocabulary that a future threshold tweak could make it do.

`job_intent` is therefore **their exact shape plus our provenance**: `id, job_id, company_id,
intent, intent_embedding, prompt_version, model, source, created_at, updated_at`. The provenance
is the part worth adding (a UNION with theirs stays attributable, a bad prompt version stays
findable). The canonical column was not.

### 5.9 Provisional enums

Validate against real jobs before the full run — a starting hypothesis, not truth.

```python
Initiative     = NEW_IMPLEMENTATION | MIGRATION | MODERNIZATION | SCALE_OUT | MAINTENANCE | UNKNOWN
Function       = DATA_ENGINEERING | ERP | CLOUD_INFRA | SECURITY | APP_DEV | ANALYTICS |
                 INTEGRATION | NETWORKING | OTHER
Seniority      = INTERN | JUNIOR | MID | SENIOR | LEAD | ARCHITECT | MANAGER | DIRECTOR | EXEC
EngagementType = PERMANENT | CONTRACT | CONSULTING | UNKNOWN

# SEARCH-EXPLAINED §9 — the CONTACT vocabulary. SEPARATE from the job enums above, deliberately:
# a job enum describes a requisition, a contact enum describes a person. Do NOT add FINANCE to the
# job Function.
ContactFunction  = FINANCE | IT | OPERATIONS | PROCUREMENT | SALES | HR | LEGAL | EXECUTIVE | OTHER
ContactSeniority = C_LEVEL | VP | DIRECTOR | MANAGER | IC | OTHER
ResultMode       = COMPANIES | PEOPLE      # PEOPLE consults contact_signal; COMPANIES does not
```

---

## 6. Retrieval pipeline (online)

Target **< 100ms** excluding the LLM parse. Everything after the parse is deterministic.

```
"manufacturing companies hiring Snowflake, last quarter"
   [1] PARSE — LLM, cacheable → chips { terms[], industry, since_days, result_mode, ... }  ← editable in UI
   [2] HARD FILTERS — facts only, in SQL (posted_date, employee_range, revenue)
   [3a] LEXICAL ts_rank top 200   ‖   [3b] SEMANTIC exact cosine top 200   (same survivor set)
        over job_signal, job_intent (§5.8) AND company_signal -> SIX lists
        (+ contact_signal in PEOPLE mode -> EIGHT lists; SEARCH-EXPLAINED §9)
   [4] project jobs→companies, then RRF Σ 1/(60+rank) over the company-level lists
   [5] COVERAGE per company (terms matched, by Term.source)
   [6] AGGREGATE → companies + evidence (+ role census in PEOPLE mode)
   [0] if 0 companies AND not a refusal → ZERO-EXPLAINER (SEARCH-EXPLAINED §10):
       re-run the filter set minus one filter at a time, name the limiter + its coverage %,
       suggest the relax with its recount. An honest zero explains itself.
```

**[1] Parse.** LLM → `Chips`. Cache on the normalized query string. Returned in the response so the
UI renders them **editable** — a wrong parse must be a visible one-click fix.
`/api/search/structured` skips this stage and is what evals use.

```python
class Term(BaseModel):
    value: str
    source: Literal["USES", "HIRING", "ANY"] = "ANY"

class Chips(BaseModel):
    terms: list[Term]
    industry: str | None = None          # soft multiplier — NEVER a filter
    since_days: int | None = None        # hard filter — a fact
    function: Function | None = None
    seniority: Seniority | None = None
    intent_mode: IntentMode = "EITHER"   # selects the weight profile (§8.2)
    result_mode: ResultMode = "COMPANIES"  # PEOPLE → consult contact_signal, role as evidence (§9)
```

**[2] Hard filters.** Facts only — the only stage that *removes* candidates.
**`industry` is NOT here.**

**[3a] Lexical.** `ts_rank` over the `tsv` GIN index, top 200.
**[3b] Semantic.** pgvector `<=>` cosine, **no index** (rule 7), top 200. `qvec` = embedding of the
**normalized query paraphrase**, not the raw string (rule 4). Run both against `company_signal` too
(no date filter — companies have no `posted_date`).

**[4] Fuse.** Project the job lists to companies *first* (best job's rank wins), then RRF the six
company-level lists. RRF fuses *ranks*, so `ts_rank` and cosine never have to be made commensurable
— which is also why adding `job_intent` as a source (§5.8) needed no re-weighting and no tuning:
a new list contributes 1/(60+rank) like every other. The intent lists dedup to the best-ranked row
per job before projecting, so a job with five matching phrases is still one job.

**[5] Coverage** by `Term.source`, in Python over ~300 candidates. **Terms never filter.**

**[6] Aggregate.** Group by `company_id`, apply §8.6, attach top 3 evidence jobs, sort by score.
Dedup happened at ingest (§5.4).

---

## 7. Schema (our tables only — derived, disposable, rebuildable)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE company_canonical (
  canonical_id     bigint PRIMARY KEY,
  domain           text UNIQUE,
  member_ids       bigint[] NOT NULL
);

CREATE TABLE job_signal (
  job_id           bigint PRIMARY KEY,
  company_id       bigint NOT NULL,      -- ALWAYS company_canonical.canonical_id
  initiative       text,
  function         text,
  seniority        text,
  engagement_type  text,
  technologies     text[],               -- canonical only
  paraphrase       text NOT NULL,
  confidence       real,
  title_norm       text,
  is_repost        boolean DEFAULT false,
  tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', paraphrase)) STORED,
  embedding        vector(3072),         -- NO index. See rule 7.
  posted_date      timestamp,
  prompt_version   text NOT NULL,
  model            text NOT NULL,
  run_at           timestamptz DEFAULT now()
);
CREATE INDEX ON job_signal USING gin(tsv);
CREATE INDEX ON job_signal (company_id);
CREATE INDEX ON job_signal (posted_date);

CREATE TABLE company_signal (
  company_id         bigint PRIMARY KEY,
  paraphrase         text NOT NULL,
  technologies       text[],
  industry_raw       text,
  industry_canonical text,
  industry_embedding vector(3072),
  tsv                tsvector GENERATED ALWAYS AS (to_tsvector('english', paraphrase)) STORED,
  embedding          vector(3072),
  prompt_version     text NOT NULL,
  model              text NOT NULL,
  run_at             timestamptz DEFAULT now()
);
CREATE INDEX ON company_signal USING gin(tsv);
CREATE INDEX ON company_signal (industry_canonical);

CREATE TABLE tech_canonical (
  term      text PRIMARY KEY,
  embedding vector(3072),
  aliases   text[]
);

CREATE TABLE tech_review_queue (
  raw_term    text PRIMARY KEY,
  nearest     text,
  similarity  real,
  occurrences int DEFAULT 1,
  resolved_to text
);

-- §5.8 — the finer grain, adopted from lead_company_job_intent, PLUS the provenance it lacks.
-- Their shape is (id, job_id, intent, intent_embedding, created_at, updated_at) and cannot say
-- whose rows are whose. Ours can, so a UNION with theirs is safe.
-- NO intent_canonical column: an intent is a descriptive phrase, not a named product (§5.8).
-- 5,209 distinct phrases / 8,114 rows, 3.9% resolved, sims 0.32-0.52. Matched via the embedding.
-- This is THEIR shape (id, job_id, intent, intent_embedding, created_at, updated_at) PLUS our
-- provenance (company_id, prompt_version, model, source) — so a UNION is attributable.
CREATE TABLE job_intent (
  id               bigserial PRIMARY KEY,
  job_id           bigint NOT NULL,
  company_id       bigint NOT NULL,     -- ALWAYS company_canonical.canonical_id
  intent           text NOT NULL,       -- as extracted; the phrase IS the record
  intent_embedding vector(3072),        -- NO index. Rule 7. This is how an intent is matched.
  prompt_version   text NOT NULL,
  model            text NOT NULL,
  source           text NOT NULL DEFAULT 'leadplus-intel',
  created_at       timestamptz DEFAULT now(),
  updated_at       timestamptz DEFAULT now()
);
CREATE UNIQUE INDEX ON job_intent (job_id, intent, prompt_version, model);  -- §5.7's key
CREATE INDEX ON job_intent (job_id);
CREATE INDEX ON job_intent (company_id);
CREATE INDEX ON job_intent USING gin (to_tsvector('english', intent));

-- There is deliberately NO `intent_canonical` and NO `intent_review_queue` table. They existed,
-- were measured (§5.8) and were dropped. `tech_canonical`/`tech_review_queue` above are NOT the
-- precedent for re-adding them: a product has an official name; a description does not.

-- SEARCH-EXPLAINED §9 — the contact ROLE CENSUS. The 4th document type ("who is there").
-- A role, never a person: NO first_name/last_name/full_name/email/phonee164/linkedin_url/notes
-- column exists, so `\d contact_signal` is the PII proof. `function`/`seniority` are a SEPARATE
-- vocabulary from the job enums (ContactFunction/ContactSeniority) — job enums describe reqs,
-- people enums describe people. Deterministic classification from the title, no LLM; only the
-- embedding of `census_text` costs anything. Consulted only in PEOPLE result mode.
CREATE TABLE contact_signal (
  id               bigserial PRIMARY KEY,
  company_id       bigint NOT NULL,      -- ALWAYS company_canonical.canonical_id, as job_signal
  lead_contact_id  bigint NOT NULL,
  canonical_title  text,                 -- the ROLE (a title), never a name
  seniority        text,                 -- ContactSeniority: C_LEVEL|VP|DIRECTOR|MANAGER|IC|OTHER
  function         text,                 -- ContactFunction: FINANCE|IT|OPERATIONS|PROCUREMENT|...
  department       text,
  is_big4_alum     boolean DEFAULT false,-- a PAST employer is Deloitte/PwC/EY/KPMG
  prior_employer   text,                 -- the Big-4 firm they left, when is_big4_alum
  landed_at        date,                 -- start date of the CURRENT role, for "recently landed"
  census_text      text NOT NULL,        -- the embedded role sentence — role words only, no PII
  tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', census_text)) STORED,
  embedding        vector(3072),         -- NO index. Rule 7.
  prompt_version   text NOT NULL,
  model            text NOT NULL,
  run_at           timestamptz DEFAULT now()
);
CREATE UNIQUE INDEX ON contact_signal (lead_contact_id);  -- §5.7's key: one census row per contact
CREATE INDEX ON contact_signal (company_id);
CREATE INDEX ON contact_signal USING gin(tsv);
CREATE INDEX ON contact_signal (function);
CREATE INDEX ON contact_signal (is_big4_alum) WHERE is_big4_alum;
```

**Never `ALTER` or write to `lead_company*`.**

---

## 8. Scoring

### 8.1 Company-level fusion

```python
def to_company_ranks(job_ranks: dict[int, int], job_to_company: dict[int, int]) -> dict[int, int]:
    """Job-level ranks -> company-level ranks. A company inherits its BEST job's rank."""
    best: dict[int, int] = {}
    for job_id, rank in job_ranks.items():
        cid = job_to_company[job_id]
        best[cid] = min(best.get(cid, 10**9), rank)
    return {cid: i + 1 for i, (cid, _) in enumerate(sorted(best.items(), key=lambda kv: kv[1]))}

K_RRF = 60
def rrf(*lists: dict[int, int]) -> dict[int, float]:
    scores = defaultdict(float)
    for ranks in lists:
        for cid, rank in ranks.items():
            scores[cid] += 1.0 / (K_RRF + rank)
    return scores

fused    = rrf(L1, L2, L3, L4)   # job_lex, job_sem, company_lex, company_sem
best_doc = normalize_01(fused)
```

A company inherits only its **best** job's rank — volume is a separate axis (§8.3).

### 8.2 Intent modes

```python
WEIGHTS: dict[IntentMode, dict[str, float]] = {
    "USES":   dict(coverage=.60, recency=.00, volume=.00, best_doc=.40),
    "HIRING": dict(coverage=.30, recency=.40, volume=.10, best_doc=.20),
    "EITHER": dict(coverage=.45, recency=.20, volume=.05, best_doc=.30),
}
```

### 8.3 Axes

```python
coverage = matched_terms / max(1, asked_terms)       # 0..1 — the AND-ness, no cliff
recency  = exp(-days_since_latest_post / 60.0)       # 0..1 — 0.0 when no jobs
volume   = log1p(distinct_matching_roles) / log(11)  # 0..1, saturates ~10
best_doc = normalized rrf from §8.1
```

**`volume` counts DISTINCT roles**, not job rows.

### 8.4 Term coverage, by source

```python
def coverage(company, jobs, terms: list[Term]) -> tuple[float, list[str]]:
    uses_hay   = {t.lower() for t in company.technologies} | tokens(company.paraphrase)
    hiring_hay = set().union(*[{t.lower() for t in j.technologies} | tokens(j.paraphrase)
                               for j in jobs]) if jobs else set()
    matched = []
    for t in terms:
        hay = {"USES": uses_hay, "HIRING": hiring_hay, "ANY": uses_hay | hiring_hay}[t.source]
        if t.value.lower() in hay:
            matched.append(t.value)
    return len(matched) / max(1, len(terms)), matched
```

### 8.5 Industry — a soft multiplier, never a filter

```python
def industry_multiplier(company, asked_industry: str | None) -> float:
    if not asked_industry:                                    return 1.00
    if company.industry_canonical == asked_industry:          return 1.00
    if cosine(emb(company.industry_raw), emb(asked_industry)) > 0.82:
                                                              return 0.75
    return 0.35     # down-weighted, NOT dropped
```

### 8.6 Final score

```python
w = WEIGHTS[chips.intent_mode]
company_score = industry_multiplier(company, chips.industry) * (
      w["coverage"] * coverage + w["recency"] * recency
    + w["volume"]   * volume   + w["best_doc"] * best_doc)
```

**Every number in §8.2 and §8.5 is invented** — a starting hypothesis, tuned against the golden set
(§14), never shipped as truth. Return the full per-axis breakdown **plus the applied `intent_mode`
and `industry_multiplier`** so any ranking is explainable.

---

## 9. Prompts

Files in `prompts/`, versioned, one per concern. **Never inline prompts in code** — the current
system's core bug is an omission inside a prompt file nobody read.

`prompts/job_normalizer.md` must specify: the **closed enum** for each enum field, each value
described precisely; that `technologies[]` extracts **only named products/platforms**, not skills or
adjectives; that `paraphrase` is **1–2 sentences, signal only, no boilerplate**; that `UNKNOWN` is
always allowed and preferred over a guess; structured outputs, never free-text parsing.

**Anti-pattern to avoid:** the shipped `lead-chat-assistant.md` has extraction rules for titles and
cities but **none** for keywords/technologies — so the model guesses and the same sentence parses
differently on different runs. Every field you want populated needs an explicit rule.

`prompts/query_parser.md`: same vocabulary and enums as the normalizer (rule 4). Handles relative
dates ("last quarter" → 90). Must **not** invent terms the user didn't say.

---

## 10. API

```
POST /api/search             { "q": "...", "limit": 20 }
POST /api/search/structured  # takes chips directly — no LLM, deterministic. Used by evals.
GET  /api/health
```

Response carries `chips` (editable in the UI), ranked `companies` with `score`, per-axis
`breakdown`, `evidence[]` (job_id, title, posted_date, matched_terms, paraphrase), and `timing_ms`.

---

## 11. Stack

Python 3.12 · `uv` · FastAPI · pydantic v2 · **psycopg 3, raw SQL, no ORM** · openai
(`gpt-4.1-mini` normalize+parse, `text-embedding-3-large` 3072) · pgvector · Jinja2 + HTMX.

**`repository.py` is the seam.** Every raw query behind a narrow interface, so the source can swap
from this restore to a real one with nothing above it changing.

LangSmith: optional observability (`@traceable`, no LangChain needed). **No key here → no-op.**
LangChain: allowed in ingest only; **never** in retrieval. Prompts stay as files.

---

## 13. Build order

| # | Phase | Acceptance |
|---|---|---|
| 0 | Profile the data | §15 gates. **Done** — see the LOCAL DEVIATION table. |
| 1 | Schema + repo skeleton | Tables created; repository returns real rows; `/api/health` green. |
| 2 | Canonical companies | `company_canonical` populated; confirm the no-op. |
| 3 | Normalizer on a sample | Eyeball every paraphrase. **Fix the prompt before the full run.** |
| 4 | Full ingest | `job_signal` + `company_signal`. Resumable. Report cost + failures. |
| 5 | Canonicalisation | `tech_canonical` + `industry_canonical`; no `SAP S4`/`S/4HANA` splits. |
| 6 | Retrieval + RRF | `/api/search/structured` ranked companies. **No LLM in this path.** |
| 7 | Scoring + evidence | Intent modes, industry multiplier, breakdown + evidence. |
| 8 | UI | Box → editable chips → cards with paraphrase, date, breakdown. |
| 9 | NL parse | `/api/search` — LLM → chips → same deterministic core. |
| 10 | Eval | precision@10 vs golden set. **Tune §8.2/§8.5 — invented until this runs.** |

---

## 14. Eval

**Without a golden set, the weights are vibes and "most accurate" is unfalsifiable.**
`evals/golden.yaml`: real queries, each with hand-labelled companies that *should* rank top.
`scripts/eval.py` reports **precision@10**, **recall@50**, **MRR** against
`/api/search/structured` so the LLM parse doesn't add noise.

> **Local deviation:** the spec wants 30–50 queries from the teammate who spent a week testing.
> Unavailable here, so `golden.yaml` is **machine-authored** from the known structure of the
> synthetic pool. It measures **regressions**, not real-world relevance. Replace it with real
> labelled queries before trusting any tuning.

This is the gate the existing system never had: 560 unit tests pass while search returns garbage,
because no test asserts *result quality*.

---

## 15. Profiling queries

```sql
SELECT count(DISTINCT lead_company_id) FROM lead_company_job WHERE active;
SELECT date_trunc('month', posted_date) m, count(*) FROM lead_company_job
 WHERE active AND posted_date IS NOT NULL GROUP BY 1 ORDER BY 1 DESC;
SELECT count(*), count(*) FILTER (WHERE cardinality(technologies) > 0),
       count(*) FILTER (WHERE length(description) > 200), avg(length(description))::int
  FROM lead_company_job WHERE active;
SELECT count(*), count(*) FILTER (WHERE cardinality(technologies) > 0),
       count(*) FILTER (WHERE cardinality(scraped_technologies) > 0)
  FROM lead_company WHERE active;
```
