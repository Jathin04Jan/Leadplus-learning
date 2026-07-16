# LeadPlus Intent Search

A standalone search MVP: one box → ranked **companies**, each with the evidence that explains it.
Full design in [`ARCHITECTURE.md`](./ARCHITECTURE.md) — read its **§4 design rules** before changing
anything; each one exists because violating it is what broke the shipped Java search.

It exists to fix three measured defects in that system:

| Defect | Here |
|---|---|
| **"and" means OR** (`keywordMatchMode` defaults to ANY) | terms **never filter** — they feed `coverage`. 3-of-3 outranks 1-of-3; nothing is dropped. |
| **`LIKE '%sap%'` matches *Sapient*** | canonical-term equality + word-boundary FTS. Measured on this corpus: `LIKE '%sap%'` returns **37**, only **26** truly run SAP — **11 false positives (42% inflation)**. |
| **No ranking** (sorts by `updated_at DESC`) | 4-retriever RRF + explainable per-axis score (§8). |

## ⚠️ Read this before quoting any number

**The corpus is synthetic** — a generated replica in `../search-demo/`, not the real `leadplus_dev`
restore the spec was written against. This build proves the **architecture works**. It does **not**
prove job-intent is a product; that needs the real corpus (blocked: the `anjali` credential has zero
table grants). See the LOCAL DEVIATION table in `ARCHITECTURE.md` §0.

Two known data limits, both artifacts of the seed generator, not the design:
- **No job posts about SAP/Snowflake/AWS.** Jobs only hire for infra (PostgreSQL, Kafka, Airflow,
  dbt…). So `"companies hiring for Snowflake"` correctly returns **0**, and the spec's own headline
  example ("Senior SAP S/4HANA Architect") isn't reproducible. USES mode is unaffected.
- **`initiative` is `MODERNIZATION` 386/386** — every seeded description shares one line, so that
  axis carries no signal and evidence paraphrases read repetitively.

## Run it

```bash
docker compose up -d                                   # pgvector pg15 :5433 + pgAdmin :5050
cp .env.example .env                                   # add OPENAI_API_KEY  (.env is gitignored)
uv venv && uv pip install -r <(uv pip compile pyproject.toml)

# source data: seed the LeadPlus tables into leadplus_local
cd ../search-demo && DATABASE_URL=postgresql://leadplus:leadplus@localhost:5433/leadplus_local npm run reset && cd -

.venv/bin/python scripts/init_db.py            # our derived tables (§7)
.venv/bin/python scripts/profile_data.py       # §15 gates
.venv/bin/python scripts/bootstrap_canonical.py
.venv/bin/python scripts/ingest.py --limit 25 --dry-run   # ALWAYS sample & read the paraphrases first
.venv/bin/python scripts/ingest.py             # full: 386 jobs + 301 companies  (~$1, ~15 min)
.venv/bin/python scripts/bootstrap_tech.py
.venv/bin/uvicorn intel.main:app --app-dir src --port 8000
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
the per-axis `breakdown` + applied `intent_mode`/`industry_multiplier`, and `evidence[]`.

## Verified

- money query `SAP + Snowflake + AWS`, industry Manufacturing → the **13 golden companies at ranks 1–13**
- **`Sapient Consulting Group` lands at rank 22**, `cov 2/3`, missing `SAP` — excluded *on the merits*, not blacklisted
- coverage cascade `3/3=13 · 2/3=16 · 1/3=21` — the **1-of-3 companies still return**, just lower (no AND/OR cliff)
- **deterministic**: identical chips → identical sha256 over ids+scores, twice
- **intent flip** on `dbt` (a both-sides term) reorders USES vs HIRING as §8.2 predicts
- **94ms** total for `/structured` (§6 budget: <100ms); LLM parse cached to ~0
- eval: normalized precision@10 **1.000**, recall@50 **1.000**, forbidden-company gate **PASS**

`evals/golden.yaml` is **machine-authored** from the known structure of the synthetic pool — it
measures **regressions, not relevance**. Only the 5 rows marked `*` can falsify a weight; the rest are
near-tautological. Replace it with real labelled queries before trusting any tuning.

## Layout

`src/intel/repository.py` is **the seam** — all SQL lives there, raw psycopg3, no ORM. Swap it to
point at a real restore and nothing above it changes. Prompts are **files** in `prompts/`, versioned
via front-matter; never inline them (the shipped system's core bug was an omission inside a prompt
file nobody read).
