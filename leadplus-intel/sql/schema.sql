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
--   * There is no chunk table. Rule 6: a job ad is already the natural unit.

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
  embedding        vector(1536),         -- NO index. See rule 7.
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
  industry_embedding vector(1536),
  tsv                tsvector GENERATED ALWAYS AS (to_tsvector('english', paraphrase)) STORED,
  embedding          vector(1536),
  prompt_version     text NOT NULL,
  model              text NOT NULL,
  run_at             timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS company_signal_tsv_idx ON company_signal USING gin(tsv);
CREATE INDEX IF NOT EXISTS company_signal_industry_canonical_idx ON company_signal (industry_canonical);

-- §5.2 stage 3 — the controlled technology vocabulary (rule 5).
CREATE TABLE IF NOT EXISTS tech_canonical (
  term      text PRIMARY KEY,
  embedding vector(1536),
  aliases   text[]
);

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
