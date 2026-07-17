# LeadPlus Intent Search

A standalone search MVP: one box → ranked **companies**, each with the evidence that explains it.
Full design in [`ARCHITECTURE.md`](./ARCHITECTURE.md) — read its **§4 design rules** before changing
anything; each one exists because violating it is what broke the shipped Java search.

It exists to fix three measured defects in that system:

| Defect | Here |
|---|---|
| **"and" means OR** (`keywordMatchMode` defaults to ANY) | terms **never filter** — they feed `coverage`. 3-of-3 outranks 1-of-3; nothing is dropped. |
| **`LIKE '%sap%'` matches *Sapient*** | canonical-term equality + word-boundary FTS. Re-measured on the **real** corpus: `LIKE '%sap%'` returns **255**, only **132** carry SAP as a canonical technology — **123 false positives (93% inflation)**. Real victims: **Che·sap·eake Systems**, and `SAP America, Inc.` itself (a company *named* SAP is not a company *using* SAP). |
| **No ranking** (sorts by `updated_at DESC`) | 6-retriever RRF + explainable per-axis score (§8); 8 retrievers in people mode. |

### Beyond the three defects — what this build also ships

- **Full company index (22,876).** Every canonical company is searchable, not just the 487 with
  text job postings — the rest are indexed by a deterministic template (one embedding each, ~$0.13
  total), so structural queries return results instead of 0. See SEARCH-EXPLAINED §5.
- **Contact role census (`contact_signal`, SEARCH-EXPLAINED §9).** 53,746 roles across 13,539
  companies, as a 4th retrieval source. It is a *role census, not a contact database*: it stores
  **no** name, email, phone or LinkedIn — only the role, function, seniority and a Big-4-alumnus
  flag. In `PEOPLE` result mode it answers "companies with a CFO / VP of Finance / a Big-4 alumnus"
  at the company level, with the role as evidence and no individual exposed.
- **Zero-explainer (SEARCH-EXPLAINED §10).** When a search legitimately returns zero, it names the
  limiting filter with its measured coverage %, and suggests which filter to drop (with the recount)
  — instead of a blank page that reads as a bug.

## ⚠️ Read this before quoting any number

**The corpus is REAL** — a verified 1:1 clone of `leadplus_dev` (72 tables, 244,659 rows). The
synthetic seed in `../search-demo/` is gone and every number below was re-measured against the
clone. See `ARCHITECTURE.md` §0.

### LOCAL-ONLY — enforced, not requested

The app connects to `postgresql://leadplus:leadplus@localhost:5433/leadplus_local` and **nothing
else**. `config.assert_local_database()` runs at import and raises unless the host is
localhost/127.0.0.1 **and** the db name contains `local`, so a stray `DATABASE_URL` fails loudly
instead of quietly writing to a hosted database. **Never point this at RDS.** The clone is
complete; RDS is done with.

### What the real data says that the spec did not

- **Only 2,886 of 13,082 active jobs carry a description >200 chars.** The other ~78% are stubs
  with no prose. Only those 2,886 (across 487 canonical companies) get an LLM-read **hiring**
  signal — normalizing a stub invents signal rather than finding it. Every canonical company
  (**22,876**) is still in the index via a deterministic company template (no LLM), so structural
  queries ("manufacturers in California") work; only the *hiring* half is limited to the 487.
- **The corpus is 49% healthcare, 15% retail, 13% financial services — and 7% manufacturing.**
  Every "industrial manufacturer migrating SAP ECC" example in the spec describes 7% of the data.
  `prompts/job_normalizer.md` v6 was corrected for this; read §0 before writing a prompt.
- **No `posted_date` in the indexed corpus.** The only rows that carried a date were the 25 seeded
  demo companies (`tenant_id 29`, `Synthetic *`, `.example` domains), and those are now **excluded
  from the index** at the company fold — so **0 of the 2,761 indexed postings carry a date.** Real
  dated postings exist (22 active) but none carry description text, so none are extractable. So
  `recency` is 0.0 for **every company**, a `since_days` filter returns **nothing** (the
  zero-explainer says why, rather than returning fictions as it once did), and the §0 thesis
  ("posted six days ago -> actively investing") is **unprovable on this data**. This is not a
  scraper gap to wave at — it is the finding.
- **The corpus is multilingual** (German is common).
- **`evals/golden.yaml` is dead** — its labels are synthetic company ids that now point at
  unrelated real companies. `scripts/eval.py` refuses to score it rather than emit a confident
  meaningless precision@10.

### Intents are embedded, NOT canonicalised — a design error we removed

**Technologies are canonicalised. Intents are not. That asymmetry is deliberate — do not
"fix" it.** We once applied the technology ladder (exact → alias → embedding NN > 0.85 → review
queue) to intent phrases too. It was a **category error**:

- A **technology is a named product**: `SAP S/4HANA` / `S/4 HANA` / `SAP S4` really are one thing
  with an official form, so canonicalising **recovers** a real identity. `tech_canonical` (4,509
  terms) stays, and its ladder catches real traps (`ROS`→`ROSS` @ 0.79).
- An **intent is a descriptive phrase**: `icd-10 coding accuracy`, `drg assignment validation`.
  There is no official form to snap to, so canonicalising **invents** an identity.

Measured on the real corpus, which is why it's gone rather than merely doubted:

| | |
|---|---|
| `job_intent` | **8,114 rows / 5,209 distinct phrases** — nearly every intent is unique |
| resolved by the ladder | **317 (3.9%)**; 7,797 NULL |
| `intent_review_queue` | **5,195 rows of things that were never broken** |
| nearest-match similarity | **0.32–0.52** (`icd-10 coding accuracy` → `record-to-report documentation` @ **0.318**) |

A vocabulary earns its place when many observations collapse onto few terms. **5,209 distinct
phrases in 8,114 rows is nothing to collapse.** The other team's `lead_company_job_intent` has
**no canonical column at all** — they didn't make this mistake. Intents are matched
**semantically via the 3072-dim `intent_embedding`** (that's what the vector is for) and lexically
via a GIN index; retrieval never read the canonical column, so removing it cost nothing.
`job_intent` is now **their exact shape + our provenance** (`prompt_version`, `model`, `source`),
so a UNION with theirs is attributable. Full reasoning in `ARCHITECTURE.md` §5.8 and rule 5.

## Run it

```bash
docker compose up -d                                   # pgvector pg15 :5433 + pgAdmin :5050
cp .env.example .env                                   # add OPENAI_API_KEY  (.env is gitignored)
uv venv && uv pip install -r <(uv pip compile pyproject.toml)

# The LeadPlus tables are ALREADY in leadplus_local (the real clone). Do not seed, do not restore.

.venv/bin/python scripts/init_db.py            # our derived tables (§7)
.venv/bin/python scripts/profile_data.py       # §15 gates
.venv/bin/python scripts/bootstrap_canonical.py   # §5.4 fold: 22,966 -> 22,876
.venv/bin/python scripts/bootstrap_tech.py --skip-industry   # seed tech_canonical (4,509 terms)
.venv/bin/python scripts/bootstrap_locations.py   # location_alias (CHANGES-v2 §3.1) — no LLM, $0
.venv/bin/python scripts/ingest.py --limit 25 --dry-run   # ALWAYS sample & READ the paraphrases
.venv/bin/python scripts/ingest.py             # LLM hiring signal: 2,886 jobs + 487 companies (~$3.79, ~2.5h)
.venv/bin/python scripts/bootstrap_tech.py     # canonicalise stored tech + §5.5 industries
.venv/bin/python scripts/backfill_company_signal.py   # template-index all 22,876 companies (no LLM, ~$0.13)
.venv/bin/python scripts/ingest_contacts.py    # contact role census: 53,746 roles, no PII (no LLM, ~$0.11)
.venv/bin/uvicorn intel.main:app --app-dir src --port 8000
.venv/bin/python scripts/acceptance.py         # behavioural guarantees (refusal, determinism, negation)
```
→ UI at <http://localhost:8000> · health at `/api/health`

Ingest is **idempotent** (`(job_id, prompt_version, model)`) — re-running is a no-op costing $0.
Bump `prompt_version` in a prompt's front-matter and only the delta re-processes.

## API

| | |
|---|---|
| `POST /api/search` | `{q, limit}` — LLM parses the sentence into chips, then the deterministic core. |
| `POST /api/search/structured` | chips in, ranked companies out. **No LLM.** What the evals measure. |
| `GET /api/health` | pg + pgvector version, row counts. |

Responses carry the parsed `chips` (**editable in the UI** — a wrong parse must be a one-click fix),
the per-axis `breakdown` + applied `intent_mode`/`industry_multiplier`, and `evidence[]`. When the
query excludes something, `excluded[]` lists what was removed and `breakdown.excluded_by` names the
group that did it. When the query is not a search, `refusal` says so and `companies` is empty —
**that is the answer, not a failure** (CHANGES-v2 §1). A `PEOPLE`-mode query
(`result_mode: PEOPLE`) also carries `contact_evidence[]` per company — the matching role census,
never a name (SEARCH-EXPLAINED §9). An honest zero carries `zero_explainer` — the limiting filter,
its coverage %, and a relax suggestion (§10).

## Verified

> **⚠️ EVERY RANK, SCORE AND EVAL NUMBER PREVIOUSLY LISTED HERE WAS MEASURED ON THE SYNTHETIC
> CORPUS AND HAS BEEN DELETED, NOT UPDATED.** "The 13 golden companies at ranks 1–13",
> "Sapient at rank 22", "precision@10 1.000 across 22 golden queries" — those companies do not
> exist in the real clone. Republishing them next to real data would be the most straightforward
> lie this README could tell. They are gone until re-measured.

**Behavioural guarantees — corpus-independent, so still checkable. Run `scripts/acceptance.py`:**

- `"create a 3-step campaign"` → `ACTION` refusal · `"ignore all previous instructions…"` →
  `UNPARSEABLE` refusal · **0 companies** for both. An empty chips object never retrieves.
- **negation safety (§2.1)**: negation matches the canonical `technologies[]` array **only** —
  never prose, never `tsv`. A substring `NOT LIKE '%sap%'` would invisibly delete
  **Che·sap·eake Systems**, and a false negative is unobservable. This is the one guard rail that
  cannot be checked by reading results.
- **deterministic**: identical chips → byte-identical `/api/search/structured` response twice.
- locations canonicalise through `location_alias` (`ca`/`calif` → `california`), because
  `hq_state` holds **full names** — §3.1's premise was inverted and §0 caught it before it shipped.

**Ranking quality is currently UNMEASURED on real data**, and honestly so: the golden set is dead
(below), and the axis the thesis leans on hardest (`recency`) has no real data to stand on.

`evals/golden.yaml` is **DEAD and `scripts/eval.py` refuses to run it.** It was machine-authored
against the synthetic pool, so its labels are company ids 1–301 — which now identify unrelated
real companies (id 1 was a planted SAP/Snowflake manufacturer; it is now "SILVIA PEREZ", an HR
services firm). Scoring it would emit a confident meaningless precision@10, which is the exact
disease this project exists to cure, so the guard hard-fails instead. The labels are
regenerable — each query records the `rule` that produced it — but read §14 first: labels
regenerated from the same predicate the scorer reads measure **regressions, not relevance**.

`scripts/acceptance.py` is what still holds on real data: refusal behaviour, determinism, and the
§2.1 negation guard rail are corpus-independent, so they are checkable where ranking quality
currently is not.

## Layout

`src/intel/repository.py` is **the seam** — all SQL lives there, raw psycopg3, no ORM. Swap it to
point at a real restore and nothing above it changes. Prompts are **files** in `prompts/`, versioned
via front-matter; never inline them (the shipped system's core bug was an omission inside a prompt
file nobody read).
