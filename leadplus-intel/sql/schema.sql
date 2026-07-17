-- LeadPlus Intent Search — derived schema (ARCHITECTURE.md §7).
--
-- These tables are ours: derived, disposable, rebuildable. Everything here is reconstructible
-- from `lead_company*` + the prompts by re-running the ingest.
--
-- NEVER `ALTER` or write to `lead_company`, `lead_company_job`, or `lead_query` (§2, §7).
--
-- Two deliberate notes on what is *absent*:
--   * There is NO index on any embedding column. Rule 7: exact brute-force cosine over the
--     filtered set is faster AND more accurate at this size, and ANN composes badly with the
--     pre-filters in §6[2]. Revisit at ~1M rows.
--     Note: at vector(3072) rule 7 stopped being merely the better choice and became the only
--     one — pgvector's ivfflat/hnsw cap out at 2000 dims and would refuse the index anyway.
--   * There is no chunk table. Rule 6: a job ad is already the natural unit.
--
-- Vectors are 3072-dim (`text-embedding-3-large`), up from 1536/-small. The driver is
-- `lead_company_job_intent`, the OTHER team's table: their intent vectors are 3072-dim, and one
-- query vector has to be comparable against job_signal, company_signal AND job_intent — theirs
-- included, if the two are ever UNIONed. Changing dims is a full re-embed; it is done once, here.

CREATE EXTENSION IF NOT EXISTS vector;

-- §5.4 — canonical company resolution. Folds copy-on-write duplicates by lower(domain).
CREATE TABLE IF NOT EXISTS company_canonical (
  canonical_id     bigint PRIMARY KEY,
  domain           text UNIQUE,
  member_ids       bigint[] NOT NULL
);

-- §5.2 — one row per normalized job posting.
CREATE TABLE IF NOT EXISTS job_signal (
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
CREATE INDEX IF NOT EXISTS job_signal_tsv_idx ON job_signal USING gin(tsv);
CREATE INDEX IF NOT EXISTS job_signal_company_id_idx ON job_signal (company_id);
CREATE INDEX IF NOT EXISTS job_signal_posted_date_idx ON job_signal (posted_date);

-- §5.3 — one row per canonical company, in the same vocabulary as job paraphrases (rule 4).
CREATE TABLE IF NOT EXISTS company_signal (
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
CREATE INDEX IF NOT EXISTS company_signal_tsv_idx ON company_signal USING gin(tsv);
CREATE INDEX IF NOT EXISTS company_signal_industry_canonical_idx ON company_signal (industry_canonical);

-- ---------------------------------------------------------------------------
-- job_intent — the finer extraction grain, adopted from the other team.
--
-- WHOSE IDEA THIS IS: another team at Limark built `lead_company_job_intent` (451 rows, 92 jobs,
-- 25 companies). Their grain is better than ours in one respect and we are adopting it: ~5 short
-- intent phrases per job ("erp transformation program", "sap ecc to snowflake pipelines")
-- instead of our single paraphrase. Measured, not assumed: 25 same-title jobs produced 11
-- distinct intent-sets, so the phrases are description-driven, not title-templated.
--
-- We keep the paraphrase too — it is the UI evidence line and the product (§1). One LLM call
-- emits both, so the finer grain costs no extra call.
--
-- WHAT WE ADD: provenance. Their table has none — `id, job_id, intent, intent_embedding,
-- created_at, updated_at` — so it cannot answer "which rows are whose, from which prompt, from
-- which model". That is fine for one team's table and fatal for a shared one: a UNION of theirs
-- and ours would be unattributable, and a bad prompt version could not be found and re-run.
-- Ours carries `prompt_version`, `model` and `source`, so a UNION is safe and a re-run is a
-- delta. This is the same §5.7 idempotency key the rest of the pipeline uses.
--
-- `lead_company_job_intent` IS THEIRS. We read it to seed vocabulary and to compare coverage.
-- We never write to it — same rule as `lead_company*`.
-- ---------------------------------------------------------------------------
-- NO `intent_canonical` COLUMN, AND THAT IS THE DESIGN (rule 5, read correctly).
--
-- Rule 5 says "enums where closed, canonicalise where LONG-TAIL". A technology is a named
-- product: `SAP S/4HANA` / `S/4 HANA` / `SAP S4` are one thing with an official form, so
-- `tech_canonical` is right and load-bearing. An INTENT IS A DESCRIPTIVE PHRASE — `icd-10 coding
-- accuracy`, `drg assignment validation` — and there is no official form to snap it to.
-- Canonicalising it was a category error, and it was measured as one:
--
--     job_intent            : 8,114 rows / 5,209 DISTINCT phrases  (nearly every intent unique)
--     resolved by the ladder:   317 (3.9%)          intent_review_queue: 5,195 never-broken rows
--     nearest-match cosines : 0.32-0.52  (`icd-10 coding accuracy` -> `record-to-report
--                             documentation` @ 0.318) — correctly nowhere near the 0.85 threshold
--
-- The other team's `lead_company_job_intent` has no canonical column at all. They were right.
-- Intents are matched SEMANTICALLY, via `intent_embedding` — that is what the vector is for —
-- and lexically via the GIN index below. Do not re-add the column or its vocabulary tables.
CREATE TABLE IF NOT EXISTS job_intent (
  id               bigserial PRIMARY KEY,
  job_id           bigint NOT NULL,
  company_id       bigint NOT NULL,     -- ALWAYS company_canonical.canonical_id, as job_signal
  intent           text NOT NULL,       -- as extracted: short, lowercase, their style
  intent_embedding vector(3072),        -- NO index. Rule 7. This is how an intent is matched.
  prompt_version   text NOT NULL,
  model            text NOT NULL,
  source           text NOT NULL DEFAULT 'leadplus-intel',
  created_at       timestamptz DEFAULT now(),
  updated_at       timestamptz DEFAULT now()
);
-- The §5.7 idempotency key, as a constraint rather than a convention: re-running an unchanged
-- prompt is a no-op, and bumping `prompt_version` re-processes only the delta.
CREATE UNIQUE INDEX IF NOT EXISTS job_intent_key_idx
  ON job_intent (job_id, intent, prompt_version, model);
CREATE INDEX IF NOT EXISTS job_intent_job_id_idx ON job_intent (job_id);
CREATE INDEX IF NOT EXISTS job_intent_company_id_idx ON job_intent (company_id);
-- GIN on a tsv of `intent` — the phrases are 3 words, so this is the lexical half of the intent
-- retrieval list (L5). Expression index, not a stored column: the text is short enough that
-- to_tsvector on the fly costs nothing, and it keeps the row narrow.
CREATE INDEX IF NOT EXISTS job_intent_tsv_idx
  ON job_intent USING gin (to_tsvector('english', intent));

-- There is deliberately NO `intent_canonical` and NO `intent_review_queue` table here. They
-- existed, were measured, and were removed — see the note on `job_intent` above and §5.8. The
-- seed came from the other team's 80 phrases and transferred to 0.1% of ours; the review queue
-- filled with 5,195 phrases that were never broken. An intent needs no vocabulary: it needs its
-- embedding, which it has.

-- §5.2 stage 3 — the controlled technology vocabulary (rule 5).
--
-- This one STAYS, and the contrast with intents above is the whole of rule 5: a product has a
-- canonical name and a phrase does not. The ladder here earns its place — it is catching real
-- traps (`ROS` -> `ROSS` @ 0.79; `SAP` -> `Sapient`, defect #2 in a new costume).
CREATE TABLE IF NOT EXISTS tech_canonical (
  term      text PRIMARY KEY,
  embedding vector(3072),
  aliases   text[]
);

-- CHANGES-v2 §3.1 — the location vocabulary (rule 5, applied to place names).
--
-- `canonical` is the FULL LOWERCASE NAME (`california`, `united states`, `st. louis`), because
-- that is the form `lead_company.hq_state`/`hq_city`/`hq_country` actually hold in this restore
-- (CHANGES-v2 §0 measured it: full names, not `CA`). The abbreviations are the *aliases*
-- pointing at it. The original spec had this inverted and would have matched nothing.
--
-- Ours, derived and rebuildable: `scripts/bootstrap_locations.py` recreates it, no LLM.
CREATE TABLE IF NOT EXISTS location_alias (
  alias      text PRIMARY KEY,   -- 'ca', 'calif', 'california'  (lowercased, whitespace-collapsed)
  canonical  text NOT NULL,      -- 'california'  <- matches lower(lead_company.hq_state)
  kind       text NOT NULL       -- 'state' | 'country' | 'city'
);
CREATE INDEX IF NOT EXISTS location_alias_canonical_idx ON location_alias (canonical);

-- ---------------------------------------------------------------------------
-- industry_alias — the same idea as `location_alias`, and it exists because rule 2's premise
-- about `industry` was FALSE on this corpus.
--
-- Rule 2 says: "`industry` is NOT a filter — it's free-text (`Industrial Machinery` vs the user's
-- `manufacturing`). Hard-filtering it silently deletes correct answers." That reasoning is sound
-- and its premise was never checked. Measured on the real corpus, `lead_company.industry` is a
-- **95-value closed taxonomy** (LinkedIn-style: `Manufacturing`, `Machinery Manufacturing`,
-- `Motor Vehicle Manufacturing`, `Hospitals and Health Care`, …), declared in `lead_query` and
-- never free-typed. It is a FACT, and rule 2's own sentence — "filter on facts" — therefore
-- applies to it. The exception was written for a column that does not exist here.
--
-- What the soft multiplier actually did, measured: "companies in the automotive industry with
-- revenue over $100M" returned Industrial-Machinery and Logistics companies. There are **4**
-- automotive companies in the entire pool and **none** over $100M, so the honest answer was 0 and
-- the tool answered with a page of wrong ones instead.
--
-- BUT A NAIVE HARD FILTER IS WORSE, WHICH IS WHY THIS TABLE IS NOT A COLUMN COMPARISON. A user
-- who says "manufacturing" means ~11,032 companies across 25 taxonomy values;
-- `industry = 'Manufacturing'` matches **1,067**. Hard-filtering on the literal string deletes 90%
-- of the correct answers — rule 2's warning coming true, just not for rule 2's reason.
--
-- So: alias -> the SET of taxonomy values it covers, and the chip is EXPANDED before it filters.
-- `(alias, canonical)` is the primary key, NOT `alias` alone — that is the whole difference from
-- `location_alias`, where one alias means one place. One industry word means MANY values, and a
-- unique `alias` could not say so.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS industry_alias (
  alias      text NOT NULL,      -- 'manufacturing', 'automotive', 'healthcare'  (lowercased)
  canonical  text NOT NULL,      -- a value of lead_company.industry, VERBATIM (not lowercased):
                                 -- it is compared to company_signal.industry_canonical
  kind       text NOT NULL,      -- 'family' (a user word covering many) | 'exact' (self-mapping)
  PRIMARY KEY (alias, canonical)
);
CREATE INDEX IF NOT EXISTS industry_alias_canonical_idx ON industry_alias (canonical);

-- §5.2 stage 3 — terms we could not resolve. A human resolves these; we NEVER auto-guess.
CREATE TABLE IF NOT EXISTS tech_review_queue (
  raw_term    text PRIMARY KEY,
  nearest     text,
  similarity  real,
  occurrences int DEFAULT 1,
  resolved_to text
);

-- ---------------------------------------------------------------------------
-- Addition beyond §7's listing, required by §5.2 / §5.7 "Failure isolation":
-- per-row try/except lands here with the raw response so one bad row never aborts the batch.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_dead_letter (
  id             bigserial PRIMARY KEY,
  kind           text NOT NULL,          -- 'job' | 'company'
  source_id      bigint NOT NULL,
  prompt_version text NOT NULL,
  model          text NOT NULL,
  error          text,
  raw_response   text,
  failed_at      timestamptz DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ingest_dead_letter_key_idx
  ON ingest_dead_letter (kind, source_id, prompt_version, model);
