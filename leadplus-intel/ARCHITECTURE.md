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

### LOCAL DEVIATION — read this before trusting any number in this spec

This repo runs against a **synthetic replica**, not the real `leadplus_dev` restore the spec assumes.
See `README.md`. Concretely:

| Spec assumes | Reality here |
|---|---|
| 22,126 jobs / 23,062 companies | **386 active jobs / 301 companies** |
| `lead_query` COMPANY_INDUSTRY = 16,870 | **13 rows** |
| copy-on-write populated | **0 shared rows** — the §5.4 fold is a confirmed **no-op** |
| job ads ~70% boilerplate | descriptions are signal-dense (avg 659 chars, 100% enriched) |
| ingest $20–40 / ~2h | **cents / ~2 min** |
| `lead_company_job_intent` exists (125 rows) | **does not exist** |

The §15 gates were run and **cannot validate the thesis** — they replay the seed. This build proves
the *architecture* works and the three defects are fixed. It does **not** prove job-intent is a
product; that needs the real corpus (still blocked: the `anjali` credential has zero table grants).

Every §4 design rule holds *more* strongly at 690 documents, not less.

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

### `lead_contact` stays excluded — the decision, and the measurement behind it

CHANGES-v2 §6 proposed indexing a **role census** (`title`, `department`, `seniority`, normalized
title tokens — never names, emails, phones or LinkedIn) so the app could answer *"manufacturers
whose CFO arrived from a Big-4 firm"*. **It was skipped**, and §6 of that spec is marked SKIPPED
rather than deferred. Two gates were run against this corpus first:

| Gate | Question | Result |
|---|---|---|
| **A** | Does `apollo_contact_data` carry `employment_history`? | **0 / 518** |
| **B** | Are there CFO / VP-Finance contacts to return? | **0 / 1,242** |

Both are zero. "Big-4 alumnus recently landed" is **dead on the data** — the field it needs does
not exist — and a `PEOPLE` result mode has nothing to return, so it could be neither built
usefully nor tested honestly. Building it anyway would have meant shipping a feature whose only
evidence of working was that it compiled.

So the position is unchanged and deliberate: **this app indexes companies and job postings.** A
query about contacts is `UNPARSEABLE` (§1) rather than quietly answered with the company half of
the question.

**If the real corpus ever lands**, the original design holds and is worth revisiting: reuse
`lead_contact_normalized_title` (it already carries `canonical_title`/`seniority`/`keywords` —
that gate passed), ingest the role census and no identifying fields, and note honestly that "the
CFO of Acme" is *pseudonymous*, not anonymous — a role is a person when the company is small
enough. Do **not** widen the job `Function` enum with `FINANCE`: job enums describe requisitions,
not people, and merging the two vocabularies is how a schema starts lying about what it holds.

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

6. **No chunking.** A job ad is 2–5KB and already the natural unit.

7. **No ANN index.** Exact brute-force cosine over the filtered set is faster *and more accurate* at
   this size. ANN composes badly with pre-filters. Revisit at ~1M rows.

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

**Stage 4 — Embed.** `text-embedding-3-small`, 1536 dims, on the **paraphrase only** — never the raw
description. Batch 100 texts per call.

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
**fine, but confirm rather than assume.** (Here: it *is* a no-op. Confirmed.)

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

### 5.9 Provisional enums

Validate against real jobs before the full run — a starting hypothesis, not truth.

```python
Initiative     = NEW_IMPLEMENTATION | MIGRATION | MODERNIZATION | SCALE_OUT | MAINTENANCE | UNKNOWN
Function       = DATA_ENGINEERING | ERP | CLOUD_INFRA | SECURITY | APP_DEV | ANALYTICS |
                 INTEGRATION | NETWORKING | OTHER
Seniority      = INTERN | JUNIOR | MID | SENIOR | LEAD | ARCHITECT | MANAGER | DIRECTOR | EXEC
EngagementType = PERMANENT | CONTRACT | CONSULTING | UNKNOWN
```

---

## 6. Retrieval pipeline (online)

Target **< 100ms** excluding the LLM parse. Everything after the parse is deterministic.

```
"manufacturing companies hiring Snowflake, last quarter"
   [1] PARSE — LLM, cacheable → chips { terms[], industry, since_days, ... }  ← editable in UI
   [2] HARD FILTERS — facts only, in SQL (posted_date, employee_range, revenue)
   [3a] LEXICAL ts_rank top 200   ‖   [3b] SEMANTIC exact cosine top 200   (same survivor set)
   [4] project jobs→companies, then RRF Σ 1/(60+rank) over 4 company-level lists
   [5] COVERAGE per company (terms matched, by Term.source)
   [6] AGGREGATE → companies + evidence
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
```

**[2] Hard filters.** Facts only — the only stage that *removes* candidates.
**`industry` is NOT here.**

**[3a] Lexical.** `ts_rank` over the `tsv` GIN index, top 200.
**[3b] Semantic.** pgvector `<=>` cosine, **no index** (rule 7), top 200. `qvec` = embedding of the
**normalized query paraphrase**, not the raw string (rule 4). Run both against `company_signal` too
(no date filter — companies have no `posted_date`).

**[4] Fuse.** Project the job lists to companies *first* (best job's rank wins), then RRF the four
company-level lists. RRF fuses *ranks*, so `ts_rank` and cosine never have to be made commensurable.

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
  embedding        vector(1536),         -- NO index. See rule 7.
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
  industry_embedding vector(1536),
  tsv                tsvector GENERATED ALWAYS AS (to_tsvector('english', paraphrase)) STORED,
  embedding          vector(1536),
  prompt_version     text NOT NULL,
  model              text NOT NULL,
  run_at             timestamptz DEFAULT now()
);
CREATE INDEX ON company_signal USING gin(tsv);
CREATE INDEX ON company_signal (industry_canonical);

CREATE TABLE tech_canonical (
  term      text PRIMARY KEY,
  embedding vector(1536),
  aliases   text[]
);

CREATE TABLE tech_review_queue (
  raw_term    text PRIMARY KEY,
  nearest     text,
  similarity  real,
  occurrences int DEFAULT 1,
  resolved_to text
);
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
(`gpt-4.1-mini` normalize+parse, `text-embedding-3-small` 1536) · pgvector · Jinja2 + HTMX.

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
