"""The seam (ARCHITECTURE.md §11).

EVERY raw query in this project lives in this file, behind a narrow interface, so the source can
swap from this synthetic restore to a real one with nothing above it changing. psycopg 3, raw
SQL, explicit column lists, no ORM.

Read/write discipline (§2, §7):
  * `lead_company`, `lead_company_job`, `lead_query` are READ-ONLY. Nothing here writes them.
  * `lead_company_job_intent` is READ-ONLY and belongs to ANOTHER TEAM. We read it to seed the
    intent vocabulary and to compare coverage; we never write it. Our equivalent is `job_intent`,
    which is the same shape plus the provenance columns theirs lacks.
  * We write only our own derived tables: company_canonical, job_signal, company_signal,
    job_intent, tech_canonical, tech_review_queue, location_alias, ingest_dead_letter.

pgvector note: `pgvector-python` is not installed, so vectors cross the wire as their text
literal (`'[0.1,0.2,...]'`) with an explicit `::vector` cast. `_vec()` is the single place that
formatting happens.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

from . import config
from .models import (
    CanonicalCompany,
    CompanySignalRow,
    CompanySource,
    JobIntentRow,
    JobSignalRow,
    JobSource,
)

# Tables we are forbidden from writing. Asserted by `assert_read_only_respected()`.
# `lead_company_job_intent` is on this list because it is another team's table, not because it is
# LeadPlus core — the discipline is the same either way: read it, never write it.
LEADPLUS_TABLES = (
    "lead_company",
    "lead_company_job",
    "lead_query",
    "lead_contact",
    "lead_company_job_intent",
)


def _vec(values: Sequence[float] | None) -> str | None:
    """Render a float list as a pgvector text literal, for use with an explicit `::vector` cast."""
    if values is None:
        return None
    return "[" + ",".join(f"{v:.8f}" for v in values) + "]"


@contextmanager
def connect(autocommit: bool = True) -> Iterator[psycopg.Connection]:
    """Open a connection with dict rows. Short-lived by design — this is a batch app."""
    with psycopg.connect(config.DATABASE_URL, row_factory=dict_row, autocommit=autocommit) as conn:
        yield conn


# ---------------------------------------------------------------------------
# Phase 1 — schema + health
# ---------------------------------------------------------------------------


def apply_schema(conn: psycopg.Connection) -> None:
    """Apply sql/schema.sql. Idempotent — every statement is IF NOT EXISTS."""
    ddl = (config.SQL_DIR / "schema.sql").read_text(encoding="utf-8")
    conn.execute(ddl)


def health(conn: psycopg.Connection) -> dict[str, Any]:
    """Everything `/api/health` needs: db reachable, pgvector present, derived tables present."""
    row = conn.execute("SELECT version() AS pg_version").fetchone()
    pg_version = (row or {}).get("pg_version", "").split(",")[0]

    row = conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()
    vector_version = (row or {}).get("extversion")

    tables: dict[str, int | None] = {}
    for table in (
        "company_canonical",
        "job_signal",
        "job_intent",
        "company_signal",
        "tech_canonical",
        "tech_review_queue",
        "location_alias",
        "ingest_dead_letter",
    ):
        exists = conn.execute("SELECT to_regclass(%s) AS reg", (table,)).fetchone()
        tables[table] = (
            conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]  # type: ignore[index]
            if (exists or {}).get("reg")
            else None
        )

    ok = bool(vector_version) and all(v is not None for v in tables.values())
    return {
        "status": "ok" if ok else "degraded",
        "postgres": pg_version,
        "pgvector": vector_version,
        "tables": tables,
    }


def assert_read_only_respected(conn: psycopg.Connection) -> list[str]:
    """Belt-and-braces: confirm no trigger/rule of ours hangs off a LeadPlus table."""
    rows = conn.execute(
        """
        SELECT c.relname AS table_name, t.tgname AS trigger_name
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        WHERE NOT t.tgisinternal AND c.relname = ANY(%s)
        """,
        (list(LEADPLUS_TABLES),),
    ).fetchall()
    return [f"{r['table_name']}.{r['trigger_name']}" for r in rows]


# ---------------------------------------------------------------------------
# §15 — profiling
# ---------------------------------------------------------------------------


def profile_companies_with_jobs(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT count(DISTINCT lead_company_id) AS n FROM lead_company_job WHERE active"
    ).fetchone()
    return row["n"]  # type: ignore[index]


def profile_jobs_by_month(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT date_trunc('month', posted_date) AS m, count(*) AS n
        FROM lead_company_job
        WHERE active AND posted_date IS NOT NULL
        GROUP BY 1 ORDER BY 1 DESC
        """
    ).fetchall()


def profile_jobs(conn: psycopg.Connection) -> dict[str, Any]:
    return conn.execute(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE cardinality(technologies) > 0) AS with_tech,
               count(*) FILTER (WHERE length(description) > 200) AS with_description,
               avg(length(description))::int AS avg_description_len
        FROM lead_company_job WHERE active
        """
    ).fetchone()  # type: ignore[return-value]


def profile_company_rows(conn: psycopg.Connection) -> dict[str, Any]:
    return conn.execute(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE cardinality(technologies) > 0) AS with_apollo_tech,
               count(*) FILTER (WHERE cardinality(scraped_technologies) > 0) AS with_scraped_tech
        FROM lead_company WHERE active
        """
    ).fetchone()  # type: ignore[return-value]


def profile_copy_on_write(conn: psycopg.Connection) -> dict[str, Any]:
    """§5.4's verification inputs — is copy-on-write actually populated in this restore?"""
    return conn.execute(
        """
        SELECT count(*) AS active_companies,
               count(*) FILTER (WHERE tenant_id IS NULL) AS shared_rows,
               count(*) FILTER (WHERE exclusion) AS excluded,
               count(*) FILTER (WHERE domain IS NULL) AS without_domain,
               count(DISTINCT lower(domain)) AS distinct_domains
        FROM lead_company WHERE active
        """
    ).fetchone()  # type: ignore[return-value]


def profile_industries(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT industry, count(*) AS n
        FROM lead_company WHERE active
        GROUP BY 1 ORDER BY 2 DESC
        """
    ).fetchall()


def profile_lead_query_types(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT type, count(*) AS n FROM lead_query GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()


# ---------------------------------------------------------------------------
# §5.4 — canonical company resolution
# ---------------------------------------------------------------------------


# Test fixtures seeded into leadplus_dev on 2026-07-09. `.example` is reserved by RFC 2606 and
# can never resolve, so this predicate cannot exclude a real company — that is the whole reason
# the TLD exists.
#
# They are excluded because they OUTRANK the real pool, not because they are untidy. Measured
# before this filter existed: 25 rows, 0.11% of 23,063 companies, taking 9-10 of every top 10.
# "companies hiring for ERP migration" returned four `Synthetic ...` shops with an identical
# 'ERP Transformation Program Manager' posting and ZERO real companies. They win because they
# were built to: 12 carry SAP+Snowflake+AWS exactly, a combination NO real company in the pool
# has. Ranking fictions above real leads is the failure this project exists to end, so they are
# cut at the fold — the root — and nothing downstream can index them.
FIXTURE_PREDICATE = "domain NOT LIKE '%%.example'"


def fetch_companies_for_fold(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """All active, real companies with the fields the fold needs.

    Deliberately NOT filtered on `exclusion`: exclusion is a per-tenant list-hygiene flag, not a
    statement that the company is unreal. Dropping those rows here would silently shrink the
    canonical set and desync `job_signal.company_id` for their jobs.

    Test fixtures ARE filtered — see `FIXTURE_PREDICATE`. A company that never becomes canonical
    cannot be indexed, so this one predicate keeps them out of every downstream table.
    """
    return conn.execute(
        f"""
        SELECT id, lower(trim(domain)) AS domain_key, domain, tenant_id, name
        FROM lead_company
        WHERE active AND {FIXTURE_PREDICATE}
        ORDER BY id
        """
    ).fetchall()


def count_active_lead_companies(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) AS n FROM lead_company WHERE active").fetchone()
    return row["n"]  # type: ignore[index]


def replace_company_canonical(
    conn: psycopg.Connection, groups: Iterable[CanonicalCompany]
) -> int:
    """Rebuild `company_canonical` wholesale. The fold is a pure function of `lead_company`."""
    rows = [(g.canonical_id, g.domain, g.member_ids) for g in groups]
    with conn.transaction():
        conn.execute("TRUNCATE company_canonical")
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO company_canonical (canonical_id, domain, member_ids)
                VALUES (%s, %s, %s)
                """,
                rows,
            )
    return len(rows)


def count_company_canonical(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) AS n FROM company_canonical").fetchone()
    return row["n"]  # type: ignore[index]


def canonical_member_map(conn: psycopg.Connection) -> dict[int, int]:
    """member lead_company.id -> canonical_id. The §5.4 contract for every downstream write."""
    rows = conn.execute(
        "SELECT canonical_id, member_ids FROM company_canonical"
    ).fetchall()
    return {member: r["canonical_id"] for r in rows for member in r["member_ids"]}


def fetch_canonical_companies(conn: psycopg.Connection) -> list[CanonicalCompany]:
    rows = conn.execute(
        "SELECT canonical_id, domain, member_ids FROM company_canonical ORDER BY canonical_id"
    ).fetchall()
    return [CanonicalCompany(**r) for r in rows]


# ---------------------------------------------------------------------------
# §5.2 stage 1 — job extract
# ---------------------------------------------------------------------------


def fetch_jobs_to_normalize(
    conn: psycopg.Connection,
    *,
    cursor: int,
    prompt_version: str,
    model: str,
    limit: int = config.FETCH_BATCH_SIZE,
) -> list[JobSource]:
    """§5.2 stage 1: keyset pagination + the NOT EXISTS resumability clause.

    The NOT EXISTS is what makes this cheap to re-run and what makes a prompt bump a delta
    rather than a full re-spend. It is also the checkpoint: there is no separate checkpoint
    table because already-written rows *are* the checkpoint.

    Deviation from §5.2's literal SQL: the `length(description)` gate. The spec was written
    against an assumed corpus; on the real clone 10,196 of 13,082 active jobs are stubs with no
    prose to normalize. See `config.MIN_DESCRIPTION_CHARS` for why they are excluded at the
    source rather than normalized into invented signal.
    """
    rows = conn.execute(
        """
        SELECT j.id, j.lead_company_id, j.title, j.description, j.department, j.location,
               j.type, j.posted_date, j.skills, j.requirements, j.technologies, j.tools,
               j.services, c.name AS company_name, c.industry, c.employee_range
        FROM lead_company_job j
        JOIN lead_company c ON c.id = j.lead_company_id
        WHERE j.active AND c.active
          AND length(j.description) > %(min_chars)s
          AND j.id > %(cursor)s
          AND NOT EXISTS (
            SELECT 1 FROM job_signal s
            WHERE s.job_id = j.id AND s.prompt_version = %(prompt_version)s AND s.model = %(model)s
          )
        ORDER BY j.id
        LIMIT %(limit)s
        """,
        {
            "cursor": cursor,
            "prompt_version": prompt_version,
            "model": model,
            "limit": limit,
            "min_chars": config.MIN_DESCRIPTION_CHARS,
        },
    ).fetchall()
    return [JobSource(**{k: (v if v is not None else _default(k)) for k, v in r.items()}) for r in rows]


def _default(key: str) -> Any:
    """Array columns are nullable in LeadPlus; the model wants [] not None."""
    return [] if key in {"skills", "requirements", "technologies", "tools", "services"} else None


def count_ingestable_jobs(conn: psycopg.Connection) -> int:
    """The denominator for the ingest report: active, text-bearing jobs at active companies.

    Must apply the SAME predicate as `fetch_jobs_to_normalize`, or the progress report measures a
    corpus the ingest is not walking.
    """
    row = conn.execute(
        """
        SELECT count(*) AS n
        FROM lead_company_job j JOIN lead_company c ON c.id = j.lead_company_id
        WHERE j.active AND c.active AND length(j.description) > %(min_chars)s
        """,
        {"min_chars": config.MIN_DESCRIPTION_CHARS},
    ).fetchone()
    return row["n"]  # type: ignore[index]


def count_text_bearing_companies(conn: psycopg.Connection) -> int:
    """How many canonical companies have at least one text-bearing job — the §5.3 denominator."""
    row = conn.execute(
        """
        SELECT count(DISTINCT cc.canonical_id) AS n
        FROM lead_company_job j
        JOIN lead_company c ON c.id = j.lead_company_id
        JOIN company_canonical cc ON j.lead_company_id = ANY(cc.member_ids)
        WHERE j.active AND c.active AND length(j.description) > %(min_chars)s
        """,
        {"min_chars": config.MIN_DESCRIPTION_CHARS},
    ).fetchone()
    return row["n"]  # type: ignore[index]


# ---------------------------------------------------------------------------
# §5.3 / §5.4 — company extract, unioned across member_ids
# ---------------------------------------------------------------------------


def fetch_companies_to_normalize(
    conn: psycopg.Connection,
    *,
    cursor: int,
    prompt_version: str,
    model: str,
    limit: int = config.FETCH_BATCH_SIZE,
) -> list[CompanySource]:
    """One row per canonical company, structured fields unioned across all `member_ids` (§5.4).

    Scalar fields come from the canonical row itself; array fields (`technologies`, `keywords`,
    `scraped_*`) are unioned across members, because a per-tenant copy may carry enrichment the
    shared row lacks.

    `notes`, `account_summary` and `salesperson_name` are NEVER selected (§3, §5.3) — internal
    free-text that may contain personal data. `lead_contact` is not touched at all.

    SCOPE, and it is a spend decision rather than a design one: this walks only the canonical
    companies that have at least one text-bearing job (488), not all 22,966 active companies.
    Indexing the full firmographic corpus is a defensible product — "companies that *use* X" is
    half of §5.1 — but it is ~22.5k more LLM calls (~$25) that nobody has approved, and it would
    add 22.5k companies with no job evidence to a system whose thesis is job evidence. The
    predicate below is the only thing standing between a `--limit`-less run and that bill, so it
    lives in the SQL, not in a caller's flag.

    When that spend is approved, delete the EXISTS clause — nothing else changes.
    """
    rows = conn.execute(
        f"""
        SELECT cc.canonical_id,
               cc.member_ids,
               cc.domain,
               c.name,
               c.industry,
               c.hq_city,
               c.hq_state,
               c.hq_country,
               c.region,
               c.employee_count,
               c.employee_range,
               c.revenue_usd,
               u.keywords,
               u.technologies,
               u.scraped_technologies,
               u.scraped_tools,
               u.scraped_services
        FROM company_canonical cc
        JOIN lead_company c ON c.id = cc.canonical_id
        CROSS JOIN LATERAL ({_MEMBER_ARRAY_UNION}) u
        WHERE cc.canonical_id > %(cursor)s
          AND EXISTS (
            -- Only companies with job evidence. See the docstring: the other 22,478 are a
            -- ~$25 spend that has not been approved.
            SELECT 1 FROM lead_company_job j
            JOIN lead_company jc ON jc.id = j.lead_company_id
            WHERE j.lead_company_id = ANY(cc.member_ids)
              AND j.active AND jc.active
              AND length(j.description) > %(min_chars)s
          )
          AND NOT EXISTS (
            SELECT 1 FROM company_signal s
            WHERE s.company_id = cc.canonical_id
              AND s.prompt_version = %(prompt_version)s AND s.model = %(model)s
          )
        ORDER BY cc.canonical_id
        LIMIT %(limit)s
        """,
        {
            "cursor": cursor,
            "prompt_version": prompt_version,
            "model": model,
            "limit": limit,
            "min_chars": config.MIN_DESCRIPTION_CHARS,
        },
    ).fetchall()
    out: list[CompanySource] = []
    for r in rows:
        r = dict(r)
        if r.get("revenue_usd") is not None:
            r["revenue_usd"] = float(r["revenue_usd"])
        out.append(CompanySource(**r))
    return out


# The array-union subquery, shared verbatim by the LLM extract above and the template extract
# below. §5.4: a per-tenant copy may carry enrichment the shared row lacks, so every array column
# is unioned across `member_ids` — each in its own scalar subquery, because unnesting them in one
# pass would cartesian-product the columns against each other.
_MEMBER_ARRAY_UNION = """
          SELECT
            COALESCE((SELECT array_agg(DISTINCT x) FROM lead_company m,
                        unnest(m.keywords) AS x WHERE m.id = ANY(cc.member_ids)), '{}')             AS keywords,
            COALESCE((SELECT array_agg(DISTINCT x) FROM lead_company m,
                        unnest(m.technologies) AS x WHERE m.id = ANY(cc.member_ids)), '{}')         AS technologies,
            COALESCE((SELECT array_agg(DISTINCT x) FROM lead_company m,
                        unnest(m.scraped_technologies) AS x WHERE m.id = ANY(cc.member_ids)), '{}') AS scraped_technologies,
            COALESCE((SELECT array_agg(DISTINCT x) FROM lead_company m,
                        unnest(m.scraped_tools) AS x WHERE m.id = ANY(cc.member_ids)), '{}')        AS scraped_tools,
            COALESCE((SELECT array_agg(DISTINCT x) FROM lead_company m,
                        unnest(m.scraped_services) AS x WHERE m.id = ANY(cc.member_ids)), '{}')     AS scraped_services
"""

# The template backfill's scope predicate: a canonical company with NO `company_signal` row of any
# kind. Deliberately NOT keyed on (prompt_version, model) like `fetch_companies_to_normalize`.
#
# That difference IS the "keep the 462" rule, expressed where it cannot be forgotten. 462 companies
# already carry an LLM-written paraphrase and a real intent extraction; a template built from
# `industry` + `hq_city` + `technologies[]` is strictly poorer than that. Keying this on the
# template's own prompt_version would make every one of them "missing the template version" and the
# backfill would overwrite richer data with thinner data, on its first run, silently. Keying it on
# "has no row at all" makes that impossible rather than merely discouraged.
_NO_COMPANY_SIGNAL = """
          AND NOT EXISTS (SELECT 1 FROM company_signal s WHERE s.company_id = cc.canonical_id)
"""


def count_companies_for_template(conn: psycopg.Connection) -> dict[str, Any]:
    """The backfill's denominator: canonical companies, and how many still lack a signal row."""
    return conn.execute(
        """
        SELECT count(*) AS canonical,
               count(*) FILTER (
                 WHERE NOT EXISTS (SELECT 1 FROM company_signal s
                                   WHERE s.company_id = cc.canonical_id)
               ) AS without_signal
        FROM company_canonical cc
        """
    ).fetchone()  # type: ignore[return-value]


def fetch_companies_for_template(
    conn: psycopg.Connection, *, cursor: int, limit: int = config.FETCH_BATCH_SIZE
) -> list[CompanySource]:
    """Every canonical company that has NO `company_signal` row, for the deterministic backfill.

    The scope difference from `fetch_companies_to_normalize` is the whole point, and it is two
    predicates:

      * that function's `EXISTS (... lead_company_job ...)` clause is **gone**. It limited the
        index to the 488 companies with a text-bearing job, on the grounds that the other ~22.5k
        were ~$25 of LLM calls nobody had approved. They are not $25 any more: a deterministic
        template costs one embedding each (~$0.13 for the lot), so the reason for the restriction
        has evaporated and with it the restriction. A lead-search tool with 2% of its leads
        indexed cannot answer "which companies use X" for 98% of the pool — structural queries
        returned 0 because the companies were not there, not because search was broken.
      * `_NO_COMPANY_SIGNAL` replaces the (prompt_version, model) key. See its comment: it is what
        stops the template overwriting the 462 richer LLM paraphrases.

    `emp_low`/`emp_high` come back parsed by `_EMP_LOW`/`_EMP_HIGH` — the SAME expressions the
    employee filter uses — so the size word a paraphrase claims and the size a filter selects on
    can never disagree. Deriving one of them in Python would let them.
    """
    rows = conn.execute(
        f"""
        SELECT cc.canonical_id,
               cc.member_ids,
               cc.domain,
               c.name,
               c.industry,
               c.hq_city,
               c.hq_state,
               c.hq_country,
               c.region,
               c.employee_count,
               c.employee_range,
               c.revenue_usd,
               {_EMP_LOW}  AS emp_low,
               {_EMP_HIGH} AS emp_high,
               u.keywords,
               u.technologies,
               u.scraped_technologies,
               u.scraped_tools,
               u.scraped_services
        FROM company_canonical cc
        JOIN lead_company c ON c.id = cc.canonical_id
        CROSS JOIN LATERAL ({_MEMBER_ARRAY_UNION}) u
        WHERE cc.canonical_id > %(cursor)s
          {_NO_COMPANY_SIGNAL}
        ORDER BY cc.canonical_id
        LIMIT %(limit)s
        """,
        {"cursor": cursor, "limit": limit},
    ).fetchall()
    out: list[CompanySource] = []
    for r in rows:
        r = dict(r)
        if r.get("revenue_usd") is not None:
            r["revenue_usd"] = float(r["revenue_usd"])
        out.append(CompanySource(**r))
    return out


# ---------------------------------------------------------------------------
# §5.2 stage 5 — load
# ---------------------------------------------------------------------------


def upsert_job_signals(conn: psycopg.Connection, rows: Sequence[JobSignalRow]) -> int:
    """Single upsert keyed on `job_id` (§5.2 stage 5) — a crash mid-batch leaves no partial rows."""
    if not rows:
        return 0
    params = [
        (
            r.job_id,
            r.company_id,
            r.initiative,
            r.function,
            r.seniority,
            r.engagement_type,
            r.technologies,
            r.paraphrase,
            r.confidence,
            r.title_norm,
            r.is_repost,
            _vec(r.embedding),
            r.posted_date,
            r.prompt_version,
            r.model,
        )
        for r in rows
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO job_signal (
              job_id, company_id, initiative, function, seniority, engagement_type,
              technologies, paraphrase, confidence, title_norm, is_repost, embedding,
              posted_date, prompt_version, model, run_at
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, now()
            )
            ON CONFLICT (job_id) DO UPDATE SET
              company_id      = EXCLUDED.company_id,
              initiative      = EXCLUDED.initiative,
              function        = EXCLUDED.function,
              seniority       = EXCLUDED.seniority,
              engagement_type = EXCLUDED.engagement_type,
              technologies    = EXCLUDED.technologies,
              paraphrase      = EXCLUDED.paraphrase,
              confidence      = EXCLUDED.confidence,
              title_norm      = EXCLUDED.title_norm,
              is_repost       = EXCLUDED.is_repost,
              embedding       = EXCLUDED.embedding,
              posted_date     = EXCLUDED.posted_date,
              prompt_version  = EXCLUDED.prompt_version,
              model           = EXCLUDED.model,
              run_at          = now()
            """,
            params,
        )
    return len(rows)


def upsert_company_signals(conn: psycopg.Connection, rows: Sequence[CompanySignalRow]) -> int:
    if not rows:
        return 0
    params = [
        (
            r.company_id,
            r.paraphrase,
            r.technologies,
            r.industry_raw,
            r.industry_canonical,
            _vec(r.industry_embedding),
            _vec(r.embedding),
            r.prompt_version,
            r.model,
        )
        for r in rows
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO company_signal (
              company_id, paraphrase, technologies, industry_raw, industry_canonical,
              industry_embedding, embedding, prompt_version, model, run_at
            ) VALUES (
              %s, %s, %s, %s, %s, %s::vector, %s::vector, %s, %s, now()
            )
            ON CONFLICT (company_id) DO UPDATE SET
              paraphrase         = EXCLUDED.paraphrase,
              technologies       = EXCLUDED.technologies,
              industry_raw       = EXCLUDED.industry_raw,
              industry_canonical = EXCLUDED.industry_canonical,
              industry_embedding = EXCLUDED.industry_embedding,
              embedding          = EXCLUDED.embedding,
              prompt_version     = EXCLUDED.prompt_version,
              model              = EXCLUDED.model,
              run_at             = now()
            """,
            params,
        )
    return len(rows)


def replace_job_intents(
    conn: psycopg.Connection,
    rows: Sequence[JobIntentRow],
    *,
    job_keys: Sequence[tuple[int, str, str]],
) -> int:
    """Load `job_intent` for a batch of jobs — one row per intent phrase.

    Delete-then-insert per (job_id, prompt_version, model) rather than a bare upsert, because a
    job's intents are a SET: a re-run of the same prompt that now emits 4 phrases where it once
    emitted 5 must leave 4 rows, not 5 with one stale. `ON CONFLICT` alone would silently keep
    the orphan and the set would drift with every re-run.

    `job_keys` is EVERY job in the batch, not just the ones that produced intents — and that
    distinction is the whole correctness of the delete. Deriving the delete set from `rows` (the
    obvious shortcut) cannot express "this job now has NO intents": a job that previously emitted
    five phrases and now emits none contributes no rows, so it would contribute no delete either,
    and its five stale phrases would survive as evidence for an extraction that no longer says
    them. An empty result is a result.

    Scoped to OUR rows: the delete carries `prompt_version`/`model`/`source`, so it cannot reach
    another prompt version's rows — and it physically cannot reach the other team's table, which
    is a different table we never write.
    """
    if not rows and not job_keys:
        return 0
    # Every job in the batch is cleared, including the ones with nothing to insert.
    keys = sorted({(job_id, pv, model, "leadplus-intel") for job_id, pv, model in job_keys})
    params = [
        (
            r.job_id,
            r.company_id,
            r.intent,
            _vec(r.intent_embedding),
            r.prompt_version,
            r.model,
            r.source,
        )
        for r in rows
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            DELETE FROM job_intent
            WHERE job_id = %s AND prompt_version = %s AND model = %s AND source = %s
            """,
            keys,
        )
        cur.executemany(
            """
            INSERT INTO job_intent (
              job_id, company_id, intent, intent_embedding,
              prompt_version, model, source, created_at, updated_at
            ) VALUES (%s, %s, %s, %s::vector, %s, %s, %s, now(), now())
            ON CONFLICT (job_id, intent, prompt_version, model) DO UPDATE SET
              company_id       = EXCLUDED.company_id,
              intent_embedding = EXCLUDED.intent_embedding,
              source           = EXCLUDED.source,
              updated_at       = now()
            """,
            params,
        )
    return len(rows)


def record_dead_letter(
    conn: psycopg.Connection,
    *,
    kind: str,
    source_id: int,
    prompt_version: str,
    model: str,
    error: str,
    raw_response: str | None,
) -> None:
    """§5.7 failure isolation. Never raises — a dead-letter write must not kill the batch either."""
    try:
        conn.execute(
            """
            INSERT INTO ingest_dead_letter (kind, source_id, prompt_version, model, error, raw_response)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (kind, source_id, prompt_version, model) DO UPDATE SET
              error = EXCLUDED.error, raw_response = EXCLUDED.raw_response, failed_at = now()
            """,
            (kind, source_id, prompt_version, model, error[:4000], (raw_response or "")[:8000] or None),
        )
    except Exception:  # noqa: BLE001 — deliberate: the dead-letter is best-effort.
        pass


def clear_dead_letters(
    conn: psycopg.Connection, *, kind: str, source_ids: Sequence[int], prompt_version: str, model: str
) -> None:
    """A row that succeeds on retry should not linger in the dead-letter table."""
    if not source_ids:
        return
    conn.execute(
        """
        DELETE FROM ingest_dead_letter
        WHERE kind = %s AND source_id = ANY(%s) AND prompt_version = %s AND model = %s
        """,
        (kind, list(source_ids), prompt_version, model),
    )


def dead_letters(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT kind, source_id, prompt_version, model, error, failed_at
        FROM ingest_dead_letter ORDER BY failed_at DESC
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# §5.6 — repost detection
# ---------------------------------------------------------------------------


def mark_reposts(conn: psycopg.Connection) -> int:
    """§5.6: same (company_id, title_norm) within 90 days AND paraphrase cosine > 0.95.

    The *earlier* posting is the original; later near-duplicates are the reposts. Both stay
    retrievable — `is_repost` exists so `volume` (§8.3) can count distinct roles, not job rows.
    """
    with conn.transaction():
        conn.execute("UPDATE job_signal SET is_repost = false")
        row = conn.execute(
            """
            WITH pairs AS (
              SELECT b.job_id
              FROM job_signal a
              JOIN job_signal b
                ON b.company_id = a.company_id
               AND b.title_norm = a.title_norm
               AND b.job_id <> a.job_id
               AND a.posted_date IS NOT NULL AND b.posted_date IS NOT NULL
               AND b.posted_date > a.posted_date
               AND b.posted_date - a.posted_date <= make_interval(days => %(window)s)
              WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL
                AND (1 - (a.embedding <=> b.embedding)) > %(threshold)s
            )
            UPDATE job_signal s SET is_repost = true
            WHERE s.job_id IN (SELECT job_id FROM pairs)
            """,
            {"window": config.REPOST_WINDOW_DAYS, "threshold": config.REPOST_COSINE_THRESHOLD},
        )
        marked = row.rowcount
    return marked


# ---------------------------------------------------------------------------
# §5.2 stage 3 — tech_canonical + review queue
# ---------------------------------------------------------------------------


def distinct_apollo_technologies(conn: psycopg.Connection) -> list[str]:
    """§5.2 stage 3 bootstrap source: Apollo's curated `lead_company.technologies[]`."""
    rows = conn.execute(
        """
        SELECT DISTINCT trim(t) AS term
        FROM lead_company, unnest(technologies) AS t
        WHERE active AND trim(t) <> ''
        ORDER BY 1
        """
    ).fetchall()
    return [r["term"] for r in rows]


def upsert_tech_canonical(
    conn: psycopg.Connection, rows: Sequence[tuple[str, list[float] | None, list[str]]]
) -> int:
    if not rows:
        return 0
    params = [(term, _vec(emb), aliases) for term, emb, aliases in rows]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO tech_canonical (term, embedding, aliases)
            VALUES (%s, %s::vector, %s)
            ON CONFLICT (term) DO UPDATE SET
              embedding = COALESCE(EXCLUDED.embedding, tech_canonical.embedding),
              aliases   = (
                SELECT array_agg(DISTINCT a)
                FROM unnest(COALESCE(tech_canonical.aliases, '{}') || EXCLUDED.aliases) AS a
              )
            """,
            params,
        )
    return len(rows)


def fetch_tech_canonical(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT term, aliases FROM tech_canonical ORDER BY term"
    ).fetchall()


def delete_tech_terms(conn: psycopg.Connection, terms: Sequence[str]) -> int:
    """Drop canonical terms outright — the MERGE half of THE INVARIANT.

    A phrase declared as another term's alias must not also be a term of its own: the §5.2
    stage-3 ladder tries exact BEFORE alias, so the standalone term shadows the alias and the
    alias never fires. `Amazon AWS` sat here as its own term and hid 40 of 65 AWS users.
    """
    if not terms:
        return 0
    conn.execute("DELETE FROM tech_canonical WHERE term = ANY(%s::text[])", (list(terms),))
    return len(terms)


def set_tech_aliases(
    conn: psycopg.Connection, updates: Sequence[tuple[str, list[str]]]
) -> int:
    """Replace a term's alias list outright — the SPLIT half of THE INVARIANT.

    Deliberately NOT `upsert_tech_canonical`, which unions alias lists so that aliases learned by
    the embedding step survive a re-seed. Removing an alias needs a call that can actually remove
    one, or `SAP` keeps claiming `SAP ECC` forever no matter what the seed says.
    """
    if not updates:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "UPDATE tech_canonical SET aliases = %s WHERE term = %s",
            [(aliases, term) for term, aliases in updates],
        )
    return len(updates)


def tech_alias_collisions(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """THE INVARIANT, as a query: every term that is also a DIFFERENT term's alias.

    Must return zero rows. Compared on the ladder's own match key (lowercased,
    punctuation-stripped) rather than on the raw string, because that is what the ladder resolves
    with — a string-equality version of this check reports 18 collisions and misses the 19th,
    `Oracle E Business Suite` vs the alias `Oracle E-Business Suite`, which differ only by
    punctuation the key strips. A guard that parses its input differently from the thing it
    guards is not a guard (see `config.assert_local_database` for the same lesson).
    """
    return conn.execute(
        """
        SELECT c.term AS term, o.term AS owner
        FROM tech_canonical c
        JOIN tech_canonical o
          ON o.term <> c.term
         AND EXISTS (
           SELECT 1 FROM unnest(COALESCE(o.aliases, '{}')) AS a
           WHERE regexp_replace(lower(a), '[^a-z0-9]', '', 'g')
               = regexp_replace(lower(c.term), '[^a-z0-9]', '', 'g')
         )
        ORDER BY c.term
        """
    ).fetchall()


def find_tech_exact(conn: psycopg.Connection, key: str) -> str | None:
    """Exact match on the normalized key: lowercased, punctuation-stripped (§5.2 stage 3)."""
    row = conn.execute(
        """
        SELECT term FROM tech_canonical
        WHERE regexp_replace(lower(term), '[^a-z0-9]', '', 'g') = %s
        LIMIT 1
        """,
        (key,),
    ).fetchone()
    return (row or {}).get("term")


def find_tech_alias(conn: psycopg.Connection, key: str) -> str | None:
    """Alias match against `tech_canonical.aliases` (§5.2 stage 3)."""
    row = conn.execute(
        """
        SELECT term FROM tech_canonical
        WHERE EXISTS (
          SELECT 1 FROM unnest(COALESCE(aliases, '{}')) AS a
          WHERE regexp_replace(lower(a), '[^a-z0-9]', '', 'g') = %s
        )
        LIMIT 1
        """,
        (key,),
    ).fetchone()
    return (row or {}).get("term")


def find_tech_nearest(
    conn: psycopg.Connection, embedding: Sequence[float]
) -> tuple[str, float] | None:
    """Embedding nearest-neighbour over `tech_canonical`.

    Brute-force exact cosine, no index (rule 7) — the vocabulary is tens of rows.
    Returns (term, cosine_similarity); the >0.85 decision is the caller's (§5.2 stage 3).
    """
    row = conn.execute(
        """
        SELECT term, 1 - (embedding <=> %s::vector) AS similarity
        FROM tech_canonical
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector
        LIMIT 1
        """,
        (_vec(embedding), _vec(embedding)),
    ).fetchone()
    if not row:
        return None
    return row["term"], float(row["similarity"])


def add_tech_alias(conn: psycopg.Connection, term: str, alias: str) -> None:
    """Record a resolved alias so the next run hits the cheap path (§5.2 stage 3: 'hit + record alias')."""
    conn.execute(
        """
        UPDATE tech_canonical
        SET aliases = (
          SELECT array_agg(DISTINCT a) FROM unnest(COALESCE(aliases, '{}') || ARRAY[%s]) AS a
        )
        WHERE term = %s
        """,
        (alias, term),
    )


def enqueue_tech_review(
    conn: psycopg.Connection, *, raw_term: str, nearest: str | None, similarity: float | None
) -> None:
    """Unresolved term -> human queue, occurrences++. We NEVER auto-guess (§5.2 stage 3)."""
    conn.execute(
        """
        INSERT INTO tech_review_queue (raw_term, nearest, similarity, occurrences)
        VALUES (%s, %s, %s, 1)
        ON CONFLICT (raw_term) DO UPDATE SET
          occurrences = tech_review_queue.occurrences + 1,
          nearest     = EXCLUDED.nearest,
          similarity  = EXCLUDED.similarity
        """,
        (raw_term, nearest, similarity),
    )


def fetch_tech_review_queue(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT raw_term, nearest, similarity, occurrences, resolved_to
        FROM tech_review_queue ORDER BY occurrences DESC, raw_term
        """
    ).fetchall()


def clear_tech_review_queue(conn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE tech_review_queue")


# ---------------------------------------------------------------------------
# Intents — stored and matched, NEVER canonicalised (§5.8).
#
# There is no intent vocabulary here, and its absence is the design. A ladder (exact -> alias ->
# embedding NN >0.85 -> review queue) seeded from the other team's 80 phrases used to live in this
# section. It was a category error — rule 5 canonicalises the LONG-TAIL of NAMED PRODUCTS, and an
# intent is a descriptive phrase with no official form — and the corpus said so plainly:
#
#     8,114 rows / 5,209 distinct phrases · 317 resolved (3.9%) · review queue 5,195
#     nearest-match cosines 0.32-0.52 — correctly nowhere near the 0.85 threshold
#
# The other team's `lead_company_job_intent` has no canonical column either. An intent is matched
# semantically (`search_job_intents_semantic`, on `intent_embedding`) and lexically
# (`search_job_intents_lexical`, on the GIN index). That is what those functions are for.
# `tech_canonical` above is the contrast, not the precedent: a product does have a canonical name.
# ---------------------------------------------------------------------------


def rival_coverage(conn: psycopg.Connection) -> dict[str, Any]:
    """Their coverage, for the honest comparison: rows, jobs, companies, distinct phrases."""
    row = conn.execute(
        """
        SELECT count(*) AS rows,
               count(DISTINCT i.job_id) AS jobs,
               count(DISTINCT j.lead_company_id) AS companies,
               count(DISTINCT i.intent) AS distinct_intents
        FROM lead_company_job_intent i
        JOIN lead_company_job j ON j.id = i.job_id
        """
    ).fetchone()
    return dict(row or {})


def intent_counts(conn: psycopg.Connection) -> dict[str, Any]:
    """What `job_intent` holds — rows, grain and reach. No vocabulary numbers: there is no
    vocabulary (§5.8), and `distinct_intents` vs `rows` is the measurement that ended it.
    """
    row = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM job_intent WHERE source = 'leadplus-intel')    AS rows,
          (SELECT count(DISTINCT intent) FROM job_intent
            WHERE source = 'leadplus-intel')                                   AS distinct_intents,
          (SELECT count(DISTINCT job_id) FROM job_intent
            WHERE source = 'leadplus-intel')                                   AS jobs,
          (SELECT count(DISTINCT company_id) FROM job_intent
            WHERE source = 'leadplus-intel')                                   AS companies
        """
    ).fetchone()
    return dict(row or {})


# ---------------------------------------------------------------------------
# Applying canonical technologies back onto the signal rows
# ---------------------------------------------------------------------------


def fetch_job_signal_technologies(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT job_id, technologies FROM job_signal ORDER BY job_id"
    ).fetchall()


def fetch_company_signal_technologies(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT company_id, technologies FROM company_signal ORDER BY company_id"
    ).fetchall()


def update_job_signal_technologies(
    conn: psycopg.Connection, updates: Sequence[tuple[int, list[str]]]
) -> int:
    if not updates:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "UPDATE job_signal SET technologies = %s WHERE job_id = %s",
            [(techs, job_id) for job_id, techs in updates],
        )
    return len(updates)


def update_company_signal_technologies(
    conn: psycopg.Connection, updates: Sequence[tuple[int, list[str]]]
) -> int:
    if not updates:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "UPDATE company_signal SET technologies = %s WHERE company_id = %s",
            [(techs, cid) for cid, techs in updates],
        )
    return len(updates)


# ---------------------------------------------------------------------------
# CHANGES-v2 §3.1 — the location vocabulary
# ---------------------------------------------------------------------------


def replace_location_aliases(
    conn: psycopg.Connection, rows: Sequence[tuple[str, str, str]]
) -> int:
    """Rebuild `location_alias` wholesale from the seed. A pure function of the seed lists."""
    if not rows:
        return 0
    with conn.transaction():
        conn.execute("TRUNCATE location_alias")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO location_alias (alias, canonical, kind) VALUES (%s, %s, %s) "
                "ON CONFLICT (alias) DO UPDATE SET canonical = EXCLUDED.canonical, kind = EXCLUDED.kind",
                list(rows),
            )
    return len(rows)


def count_location_aliases(conn: psycopg.Connection) -> dict[str, Any]:
    return conn.execute(
        """
        SELECT count(*) AS aliases, count(DISTINCT canonical) AS canonicals,
               count(*) FILTER (WHERE kind = 'state')   AS states,
               count(*) FILTER (WHERE kind = 'country') AS countries,
               count(*) FILTER (WHERE kind = 'city')    AS cities
        FROM location_alias
        """
    ).fetchone()  # type: ignore[return-value]


def distinct_hq_locations(conn: psycopg.Connection) -> dict[str, list[str]]:
    """The place names this restore actually holds — READ-ONLY, and the reason §3.1 was inverted.

    The seed is a static list, but a static list that misses a value present in `hq_city` fails
    silently and invisibly (the query just returns nothing). So the bootstrap reads the corpus's
    own distinct values and guarantees every one of them is resolvable to itself. This is a read
    of `lead_company`; §2's "never writes to LeadPlus tables" is untouched.
    """
    out: dict[str, list[str]] = {}
    for column, kind in (("hq_state", "state"), ("hq_city", "city"), ("hq_country", "country")):
        rows = conn.execute(
            f"""
            SELECT DISTINCT trim({column}) AS value
            FROM lead_company
            WHERE active AND {column} IS NOT NULL AND trim({column}) <> ''
            ORDER BY 1
            """
        ).fetchall()
        out[kind] = [r["value"] for r in rows]
    return out


def expand_locations(conn: psycopg.Connection, aliases: Sequence[str]) -> list[dict[str, Any]]:
    """alias key -> canonical location. The §3.1 expansion, done in the repository.

    The parser emits raw text ("CA", "Calif", "California"); `hq_state` holds `California`. This
    is the only place that gap is closed, which is why §3 says the repository expands rather than
    the parser guessing: the parser cannot know what spelling this restore uses, and if it guesses
    `CA` the filter matches zero rows and the user is told, wrongly, that there are no Californian
    manufacturers.
    """
    if not aliases:
        return []
    return conn.execute(
        """
        SELECT alias, canonical, kind FROM location_alias
        WHERE alias = ANY(%(aliases)s::text[])
        ORDER BY alias
        """,
        {"aliases": list(aliases)},
    ).fetchall()


# ---------------------------------------------------------------------------
# The industry vocabulary — `industry_alias`, the location_alias pattern applied to a taxonomy.
# ---------------------------------------------------------------------------


def replace_industry_aliases(
    conn: psycopg.Connection, rows: Sequence[tuple[str, str, str]]
) -> int:
    """Rebuild `industry_alias` wholesale from the seed. A pure function of the seed + the corpus."""
    if not rows:
        return 0
    with conn.transaction():
        conn.execute("TRUNCATE industry_alias")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO industry_alias (alias, canonical, kind) VALUES (%s, %s, %s) "
                "ON CONFLICT (alias, canonical) DO UPDATE SET kind = EXCLUDED.kind",
                list(rows),
            )
    return len(rows)


def count_industry_aliases(conn: psycopg.Connection) -> dict[str, Any]:
    return conn.execute(
        """
        SELECT count(*) AS rows, count(DISTINCT alias) AS aliases,
               count(DISTINCT canonical) AS canonicals,
               count(*) FILTER (WHERE kind = 'family') AS family,
               count(*) FILTER (WHERE kind = 'exact')  AS exact
        FROM industry_alias
        """
    ).fetchone()  # type: ignore[return-value]


def expand_industries(conn: psycopg.Connection, aliases: Sequence[str]) -> list[dict[str, Any]]:
    """alias key -> the SET of taxonomy values it covers. The expansion, done in the repository.

    The direct counterpart of `expand_locations`, and it lives here for the same reason §3 gives:
    the parser cannot know what spellings this restore holds, and if it guesses it produces a
    filter that matches zero rows and a user who is told, wrongly, that there are no manufacturers.

    The difference from `expand_locations` is the cardinality, and it is the whole point.
    One location alias means one place; one industry alias means **many** taxonomy values —
    `manufacturing` covers 25 of them. So this returns rows, not a mapping, and the caller
    unions them.
    """
    if not aliases:
        return []
    return conn.execute(
        """
        SELECT alias, canonical, kind FROM industry_alias
        WHERE alias = ANY(%(aliases)s::text[])
        ORDER BY alias, canonical
        """,
        {"aliases": list(aliases)},
    ).fetchall()


def known_segments(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """The values `lead_company.segments[]` actually holds. READ-ONLY.

    There is no `segment_alias` table and there should not be: `segments` is already a short closed
    set of exact strings with no synonyms anyone types (nobody has ever typed "Automate26" by
    accident). What it needs is not canonicalisation but **reality** — CHANGES-v2 §4 asserted the
    set was `Enterprise | Mid-Market | SMB`, and not one of those three exists. Reading the column
    is how a filter stops being built on a guess. See CHANGES-v2 §4 for the numbers.
    """
    return conn.execute(
        """
        SELECT s AS value, count(*) AS n
        FROM lead_company, unnest(segments) AS s
        WHERE active AND domain NOT LIKE '%%.example'
        GROUP BY 1 ORDER BY 2 DESC, 1
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# §5.5 — industry canonicalisation
# ---------------------------------------------------------------------------


def fetch_industry_vocabulary(conn: psycopg.Connection) -> list[str]:
    """§5.5: reuse `lead_query WHERE type='COMPANY_INDUSTRY'`. Do NOT invent a second taxonomy."""
    rows = conn.execute(
        """
        SELECT DISTINCT trim(value) AS value
        FROM lead_query
        WHERE type = 'COMPANY_INDUSTRY' AND value IS NOT NULL AND trim(value) <> ''
        ORDER BY 1
        """
    ).fetchall()
    return [r["value"] for r in rows]


def distinct_company_industries(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """The industry values this restore actually holds, with their weights. READ-ONLY.

    `lead_query WHERE type='COMPANY_INDUSTRY'` (§5.5) is the *declared* taxonomy — 91 values — and
    `lead_company.industry` is what the rows *say*: 95 values, overlapping but not equal (the
    column carries `Software`, `Cloud`, `Analytics`, `Logistics`, `Technology`, `Finance Tech`,
    which the declared list does not). `bootstrap_industries.py` needs both, for the same reason
    `bootstrap_locations.py` sweeps `hq_city`: a vocabulary that misses a value the corpus holds
    fails **silently** — the query just returns nothing, and an empty page reads as an answer.

    The counts come back because they are the argument. "manufacturing" covering 11,032 companies
    across 25 taxonomy values, versus `industry = 'Manufacturing'` covering 1,067, is not a fact
    anyone should have to take on trust from a comment.
    """
    return conn.execute(
        """
        SELECT trim(industry) AS value, count(*) AS n
        FROM lead_company
        WHERE active AND industry IS NOT NULL AND trim(industry) <> ''
        GROUP BY 1
        ORDER BY 2 DESC, 1
        """
    ).fetchall()


def fetch_company_industries(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT company_id, industry_raw FROM company_signal ORDER BY company_id"
    ).fetchall()


def update_company_industry(
    conn: psycopg.Connection,
    updates: Sequence[tuple[int, str | None, list[float] | None]],
) -> int:
    if not updates:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE company_signal
            SET industry_canonical = %s, industry_embedding = %s::vector
            WHERE company_id = %s
            """,
            [(canonical, _vec(emb), cid) for cid, canonical, emb in updates],
        )
    return len(updates)


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------


def verify_counts(conn: psycopg.Connection) -> dict[str, Any]:
    """The acceptance proof: row counts, embedding presence, and actual vector dimensionality."""
    return conn.execute(
        """
        SELECT
          (SELECT count(*) FROM job_signal)                                          AS job_signal,
          (SELECT count(*) FROM job_signal WHERE embedding IS NULL)                  AS job_signal_no_embedding,
          (SELECT count(DISTINCT vector_dims(embedding)) FROM job_signal
             WHERE embedding IS NOT NULL)                                            AS job_dim_variants,
          (SELECT max(vector_dims(embedding)) FROM job_signal)                       AS job_dims,
          (SELECT count(*) FROM job_signal WHERE is_repost)                          AS reposts,
          (SELECT count(*) FROM company_signal)                                      AS company_signal,
          (SELECT count(*) FROM company_signal WHERE embedding IS NULL)              AS company_signal_no_embedding,
          (SELECT max(vector_dims(embedding)) FROM company_signal)                   AS company_dims,
          (SELECT count(*) FROM company_signal WHERE industry_canonical IS NULL)     AS company_no_industry,
          (SELECT count(*) FROM company_signal WHERE industry_embedding IS NULL)     AS company_no_industry_embedding,
          (SELECT count(*) FROM company_canonical)                                   AS company_canonical,
          (SELECT count(*) FROM tech_canonical)                                      AS tech_canonical,
          (SELECT count(*) FROM tech_review_queue)                                   AS tech_review_queue,
          (SELECT count(*) FROM job_intent WHERE source = 'leadplus-intel')          AS job_intent,
          (SELECT count(*) FROM job_intent
             WHERE source = 'leadplus-intel' AND intent_embedding IS NULL)           AS job_intent_no_embedding,
          (SELECT count(DISTINCT vector_dims(intent_embedding)) FROM job_intent
             WHERE source = 'leadplus-intel' AND intent_embedding IS NOT NULL)       AS intent_dim_variants,
          (SELECT max(vector_dims(intent_embedding)) FROM job_intent
             WHERE source = 'leadplus-intel')                                        AS intent_dims,
          (SELECT count(DISTINCT job_id) FROM job_intent WHERE source = 'leadplus-intel')     AS intent_jobs,
          (SELECT count(DISTINCT company_id) FROM job_intent WHERE source = 'leadplus-intel') AS intent_companies,
          (SELECT count(*) FROM ingest_dead_letter)                                  AS dead_letters
        """
    ).fetchone()  # type: ignore[return-value]


def orphan_job_signals(conn: psycopg.Connection) -> int:
    """§5.4 invariant: every `job_signal.company_id` AND `job_intent.company_id` is canonical.

    `job_intent` is included because it carries the same denormalised `company_id` and is fused
    into the same company-level RRF (§8.1). A non-canonical id there would surface a company that
    the fold was supposed to have merged away — the exact duplicate §5.4 exists to prevent, just
    arriving through the newer table.
    """
    row = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM job_signal s
            WHERE NOT EXISTS (SELECT 1 FROM company_canonical cc
                              WHERE cc.canonical_id = s.company_id))
          +
          (SELECT count(*) FROM job_intent i
            WHERE i.source = 'leadplus-intel'
              AND NOT EXISTS (SELECT 1 FROM company_canonical cc
                              WHERE cc.canonical_id = i.company_id))
          AS n
        """
    ).fetchone()
    return row["n"]  # type: ignore[index]


def sample_signals(conn: psycopg.Connection, limit: int = 10) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT job_id, company_id, initiative, function, seniority, engagement_type,
               technologies, paraphrase, confidence, title_norm, is_repost, posted_date
        FROM job_signal ORDER BY job_id LIMIT %s
        """,
        (limit,),
    ).fetchall()


def technology_histogram(conn: psycopg.Connection, table: str) -> list[dict[str, Any]]:
    """Post-canonicalisation vocabulary check — used to prove no SAP S4 / S/4HANA splits (§5.5)."""
    if table not in {"job_signal", "company_signal"}:
        raise ValueError(f"refusing to interpolate unknown table: {table}")
    return conn.execute(
        f"""
        SELECT t AS term, count(*) AS n
        FROM {table}, unnest(technologies) AS t
        GROUP BY 1 ORDER BY 2 DESC, 1
        """
    ).fetchall()


def industry_histogram(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT industry_raw, industry_canonical, count(*) AS n
        FROM company_signal GROUP BY 1, 2 ORDER BY 3 DESC
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# §6 — retrieval. NO LLM below this line; these four lists are pure SQL.
#
# Three things are load-bearing in every query here:
#
#   * **The hard filters are facts only** (§6[2], rule 2): `posted_date`, employee count and
#     revenue. `industry` is deliberately absent — it is free text, and hard-filtering it would
#     silently delete correct answers before ranking ever saw them (§8.5 down-weights instead).
#     Terms are absent too: they feed coverage (§8.4), they never remove a row.
#
#   * **Every optional filter is expressed as `%(p)s::t IS NULL OR ...`** so the SQL stays static
#     and one plan serves every shape of query. Each parameter is cast explicitly, because
#     Postgres cannot infer a type for a bare NULL parameter.
#
#   * **Every ORDER BY ends in a unique-column tiebreak.** Ties in `ts_rank` and in cosine are
#     common, and without a tiebreak the row order — and therefore the rank passed to RRF — is
#     whatever the executor felt like. That would make identical chips return a different
#     ranking on a re-run, which is the exact defect this project exists to fix.
#
# The embedding columns have no index, by rule 7: exact brute-force cosine over 386+301 rows is
# both faster and more accurate than ANN, and ANN composes badly with the pre-filters above.
# ---------------------------------------------------------------------------

# The job-side hard filters, shared verbatim by the lexical list, the semantic list, and the
# evidence fetch. They must be identical in all three: a job that is filtered out of the lists
# but back in the evidence would be scored as evidence for a match it never made.
_JOB_FILTERS = """
          AND (%(since_days)s::int IS NULL
               OR j.posted_date >= now() - make_interval(days => %(since_days)s::int))
          AND (%(function)s::text IS NULL OR j.function = %(function)s::text)
          AND (%(seniority)s::text IS NULL OR j.seniority = %(seniority)s::text)
"""

# Firmographic facts live on `lead_company`, not on our derived tables, so both the job and the
# company lists join to it to filter.
#
# ---- `employee_count` IS NOT A NUMBER, AND READING IT AS ONE DELETED 269 OF 273 ANSWERS -------
#
# This filter used to be `regexp_replace(c.employee_count, '[^0-9]', '', 'g')::bigint`, i.e.
# "strip everything that isn't a digit and call the rest the headcount". That is only correct for
# the 5,672 rows that hold a bare integer. Measured across the 22,941 active real companies:
#
#     bare integer   ('1600')                     :  5,672
#     bucket string  ('201-500 employees')        : 16,829   <- the MAJORITY
#     other          ('10K', '1.5K', '10,001+')   :    186
#     NULL                                        :    254
#
# On a bucket string the digit-strip concatenates the bounds:
#
#     '201-500 employees'    -> 201500     (two hundred thousand employees)
#     '501-1,000 employees'  -> 5011000    (five million)
#     '10K'                  -> 10         (ten, not ten thousand)
#     '1.5K'                 -> 15         (fifteen, not fifteen hundred)
#
# So "manufacturing companies in California with 500-1000 employees" returned **4** — the four
# whose bare integer happened to land in range — while the **269** Californian manufacturers whose
# row literally reads `501-1,000 employees` were deleted before ranking ever saw them. That is
# this project's founding disease (a silent false negative nobody can see) reproduced inside the
# replacement, in the one filter that looked too boring to check.
#
# The fix parses the column into a [low, high] interval instead, and the parse lives HERE, in one
# SQL fragment, because `fetch_companies_for_template` needs the same numbers to write a size word
# into a paraphrase. Two parsers would eventually disagree about what a row means — the lesson
# `config.assert_local_database` and `repository.tech_alias_collisions` both learned the hard way.
#
# `high` is NULL for an open-ended bucket ('10,001+ employees'): there is no upper bound to
# compare, and inventing one would be a guess.
_EMP_LOW = """NULLIF(CASE
          WHEN c.employee_count ~ '^\\s*[0-9]+\\s*$'          THEN trim(c.employee_count)
          WHEN c.employee_count ~ '^\\s*[0-9,]+\\s*-'         THEN replace(split_part(c.employee_count, '-', 1), ',', '')
          WHEN c.employee_count ~ '^\\s*[0-9,]+\\s*\\+'        THEN replace(regexp_replace(c.employee_count, '[^0-9,].*$', '', 'g'), ',', '')
          WHEN c.employee_count ~ '^\\s*[0-9.]+\\s*[Kk]\\s*$'  THEN (replace(lower(trim(c.employee_count)), 'k', '')::numeric * 1000)::bigint::text
          ELSE NULL END, '')::bigint"""

_EMP_HIGH = """NULLIF(CASE
          WHEN c.employee_count ~ '^\\s*[0-9]+\\s*$'          THEN trim(c.employee_count)
          WHEN c.employee_count ~ '^\\s*[0-9,]+\\s*-'         THEN replace(regexp_replace(split_part(c.employee_count, '-', 2), '[^0-9,].*$', '', 'g'), ',', '')
          WHEN c.employee_count ~ '^\\s*[0-9,]+\\s*\\+'        THEN NULL
          WHEN c.employee_count ~ '^\\s*[0-9.]+\\s*[Kk]\\s*$'  THEN (replace(lower(trim(c.employee_count)), 'k', '')::numeric * 1000)::bigint::text
          ELSE NULL END, '')::bigint"""

# CONTAINMENT, not overlap — and it is a deliberate choice between two honest readings.
#
# The data is buckets, so "500-1000 employees" cannot always be answered exactly. Two options:
#   * overlap    — return a company whose bucket *touches* [500,1000]. '201-500 employees' would
#                  qualify on its single top value, and the company may really have 250 people.
#   * containment— return a company whose bucket *fits inside* [500,1000]. '501-1,000 employees'
#                  qualifies; '201-500' does not.
# Containment is chosen because every returned company then provably satisfies what was asked,
# which is the claim a filter makes. Overlap would quietly re-introduce false positives on a HARD
# filter, and a hard filter that returns rows it cannot justify is a filter the user cannot use.
# An unparseable or NULL `employee_count` fails both bounds: we cannot prove it satisfies them.
_FIRMO_FILTERS = f"""
          AND (%(min_employees)s::bigint IS NULL OR {_EMP_LOW} >= %(min_employees)s::bigint)
          AND (%(max_employees)s::bigint IS NULL OR {_EMP_HIGH} <= %(max_employees)s::bigint)
          AND (%(min_revenue_usd)s::numeric IS NULL OR c.revenue_usd >= %(min_revenue_usd)s::numeric)
          AND (%(max_revenue_usd)s::numeric IS NULL OR c.revenue_usd <= %(max_revenue_usd)s::numeric)
"""

# CHANGES-v2 §2.1/§3.2/§4/§5 — the company-level fact filters.
#
# Shared verbatim by all four §6[3] lists. Every one of them joins `lead_company` as `c` on the
# company id (`c.id = j.company_id` / `c.id = cs.company_id`), so `c.id` is the canonical company
# id in every query here and one fragment serves them all.
#
# Rule 2 is not broken by any of this, it is sharpened (§12): a state, a segment, a NAICS code and
# a NULL check are **facts**, and rule 2 says filter on facts. `industry` remains free text and
# therefore remains a soft multiplier — unless the user explicitly said "strictly", which is what
# `industry_pos` carries.
#
# ---- THE NEGATION GUARD RAIL (§2.1) — NON-NEGOTIABLE, DO NOT "IMPROVE" THIS ----
#
# Negation matches the canonical `technologies[]` array with `&&` (array overlap = exact element
# equality) and **NEVER** `paraphrase`, `tsv`, or any `LIKE`. A `NOT LIKE '%sap%'` here would
# delete **Sapient Consulting Group** by substring — this project's founding bug, inverted. And
# inverted is strictly worse: a false *positive* ranks lower and a human can see it and laugh; a
# false *negative* is a company that silently never appears, which nobody can see at all.
#
# The empty-array cases need no `cardinality` guard: `technologies && '{}'::text[]` is always
# false, so `NOT EXISTS(...)` is trivially true when nothing is negated.
_FACT_FILTERS = """
          AND (cardinality(%(loc_pos)s::text[]) = 0
               OR lower(coalesce(c.hq_state, ''))   = ANY(%(loc_pos)s::text[])
               OR lower(coalesce(c.hq_city, ''))    = ANY(%(loc_pos)s::text[])
               OR lower(coalesce(c.hq_country, '')) = ANY(%(loc_pos)s::text[]))
          AND NOT (lower(coalesce(c.hq_state, ''))   = ANY(%(loc_neg)s::text[])
                OR lower(coalesce(c.hq_city, ''))    = ANY(%(loc_neg)s::text[])
                OR lower(coalesce(c.hq_country, '')) = ANY(%(loc_neg)s::text[]))
          AND (cardinality(%(segments)s::text[]) = 0
               OR coalesce(c.segments::text[], '{}'::text[]) && %(segments)s::text[])
          AND (cardinality(%(naics)s::text[]) = 0
               OR coalesce(c.naics_codes::text[], '{}'::text[]) && %(naics)s::text[])
          AND (cardinality(%(sic)s::text[]) = 0
               OR coalesce(c.sic_codes::text[], '{}'::text[]) && %(sic)s::text[])
          AND (%(has_linkedin)s::boolean IS NULL
               OR (%(has_linkedin)s::boolean IS TRUE  AND c.linkedin_url IS NOT NULL)
               OR (%(has_linkedin)s::boolean IS FALSE AND c.linkedin_url IS NULL))
          AND (cardinality(%(industry_pos)s::text[]) = 0
               OR EXISTS (SELECT 1 FROM company_signal ics
                          WHERE ics.company_id = c.id
                            AND lower(ics.industry_canonical) = ANY(%(industry_pos)s::text[])))
          AND NOT EXISTS (SELECT 1 FROM company_signal ics
                          WHERE ics.company_id = c.id
                            AND lower(ics.industry_canonical) = ANY(%(industry_neg)s::text[]))
          AND NOT EXISTS (SELECT 1 FROM company_signal ncs
                          WHERE ncs.company_id = c.id
                            AND ncs.technologies && %(neg_uses)s::text[])
          AND NOT EXISTS (SELECT 1 FROM job_signal njs
                          WHERE njs.company_id = c.id
                            AND njs.technologies && %(neg_hiring)s::text[])
"""

# §6[3a]'s tsquery, built from the chip phrases.
#
# The phrases are OR'd, never AND'd. An AND here would re-create defect #1 from the other side:
# the lexical list is a *candidate generator*, and a company matching two of three terms must
# still enter the pool and be out-ranked by coverage — not be deleted by the retrieval SQL.
#
# `plainto_tsquery` per phrase handles lexing, stemming and escaping (so a phrase like
# "SAP S/4HANA" cannot break the query or inject); rendering each back `::text` and re-parsing
# the OR-joined result with `to_tsquery` is what lets the whole thing stay one static statement
# with a single array parameter. The inner ORDER BY makes the assembled query string itself
# byte-stable.
_TSQUERY_CTE = """
        WITH parts AS (
          SELECT plainto_tsquery('english', p) AS t
          FROM unnest(%(phrases)s::text[]) AS p
        ), q AS (
          SELECT to_tsquery('english', string_agg('(' || t::text || ')', ' | ' ORDER BY t::text))
                 AS query
          FROM parts
          WHERE t::text <> ''
        )
"""


def search_jobs_lexical(
    conn: psycopg.Connection, *, phrases: Sequence[str], filters: dict[str, Any], limit: int = 200
) -> list[dict[str, Any]]:
    """§6[3a] list L1 — `ts_rank` over the `job_signal` GIN index, top 200."""
    return conn.execute(
        _TSQUERY_CTE
        + f"""
        SELECT j.job_id, j.company_id, ts_rank(j.tsv, q.query) AS score
        FROM job_signal j
        JOIN lead_company c ON c.id = j.company_id
        CROSS JOIN q
        WHERE q.query IS NOT NULL
          AND j.tsv @@ q.query
          {_JOB_FILTERS}
          {_FIRMO_FILTERS}
          {_FACT_FILTERS}
        ORDER BY score DESC, j.job_id
        LIMIT %(limit)s
        """,
        {"phrases": list(phrases), "limit": limit, **filters},
    ).fetchall()


def search_jobs_semantic(
    conn: psycopg.Connection, *, qvec: Sequence[float], filters: dict[str, Any], limit: int = 200
) -> list[dict[str, Any]]:
    """§6[3b] list L2 — exact cosine over `job_signal.embedding`. No ANN index (rule 7)."""
    return conn.execute(
        f"""
        SELECT j.job_id, j.company_id, 1 - (j.embedding <=> %(qvec)s::vector) AS score
        FROM job_signal j
        JOIN lead_company c ON c.id = j.company_id
        WHERE j.embedding IS NOT NULL
          {_JOB_FILTERS}
          {_FIRMO_FILTERS}
          {_FACT_FILTERS}
        ORDER BY j.embedding <=> %(qvec)s::vector, j.job_id
        LIMIT %(limit)s
        """,
        {"qvec": _vec(qvec), "limit": limit, **filters},
    ).fetchall()


def search_job_intents_lexical(
    conn: psycopg.Connection, *, phrases: Sequence[str], filters: dict[str, Any], limit: int = 200
) -> list[dict[str, Any]]:
    """List L5 — `ts_rank` over `job_intent.intent`, the finer grain.

    Why a fifth and sixth list rather than folding intents into `job_signal`'s tsv: an intent
    phrase is 3 words and a paraphrase is 30, so a shared `ts_rank` would be dominated by the
    longer text and the precise phrase match would be diluted exactly when it is most useful.
    Ranking them separately and letting RRF fuse the *ranks* is the same argument that put
    `ts_rank` and cosine in separate lists to begin with (§8.1) — RRF exists so incommensurable
    scorers never have to be made commensurable.

    Rows are deduped to the best-ranked row per JOB before they leave: a job has ~5 intents and
    several may match, and without this a job would contribute 5 entries whose ranks then collapse
    to one company anyway — quietly weighting a job by how many of its phrases matched, which is
    what `coverage` and `volume` are for.

    `source = 'leadplus-intel'` scopes this to OUR rows. The other team's table is not touched
    here; a UNION with theirs would be safe (that is what provenance is for) but it would also be
    a claim about their data's freshness that we cannot make.

    Applies the SAME `_JOB_FILTERS`/`_FIRMO_FILTERS`/`_FACT_FILTERS` as every other list — an
    intent that survived a filter the paraphrase did not would be evidence for a match that
    never happened.
    """
    return conn.execute(
        _TSQUERY_CTE
        + f"""
        SELECT job_id, company_id, intent, score FROM (
          -- DISTINCT ON must lead its ORDER BY with the distinct key, which would leave the rows
          -- ordered by job_id. The outer query restores score order: `_ranks()` reads position
          -- as rank, so emitting these in job_id order would silently rank by primary key.
          SELECT DISTINCT ON (i.job_id)
                 i.job_id, i.company_id, i.intent,
                 ts_rank(to_tsvector('english', i.intent), q.query) AS score
          FROM job_intent i
          JOIN job_signal j ON j.job_id = i.job_id
          JOIN lead_company c ON c.id = i.company_id
          CROSS JOIN q
          WHERE q.query IS NOT NULL
            AND i.source = 'leadplus-intel'
            AND to_tsvector('english', i.intent) @@ q.query
            {_JOB_FILTERS}
            {_FIRMO_FILTERS}
            {_FACT_FILTERS}
          ORDER BY i.job_id, score DESC, i.id
        ) t
        ORDER BY score DESC, job_id
        LIMIT %(limit)s
        """,
        {"phrases": list(phrases), "limit": limit, **filters},
    ).fetchall()


def search_job_intents_semantic(
    conn: psycopg.Connection, *, qvec: Sequence[float], filters: dict[str, Any], limit: int = 200
) -> list[dict[str, Any]]:
    """List L6 — exact cosine over `job_intent.intent_embedding`. No ANN index (rule 7).

    3072 dims, the same space as `job_signal.embedding` and `company_signal.embedding`, so the
    single query vector built in `retrieve.prepare` compares against all three.
    """
    return conn.execute(
        f"""
        SELECT job_id, company_id, intent, score FROM (
          -- See the lexical twin: DISTINCT ON dictates the inner order, the outer query restores
          -- score order so that row position means rank.
          SELECT DISTINCT ON (i.job_id)
                 i.job_id, i.company_id, i.intent,
                 1 - (i.intent_embedding <=> %(qvec)s::vector) AS score
          FROM job_intent i
          JOIN job_signal j ON j.job_id = i.job_id
          JOIN lead_company c ON c.id = i.company_id
          WHERE i.intent_embedding IS NOT NULL
            AND i.source = 'leadplus-intel'
            {_JOB_FILTERS}
            {_FIRMO_FILTERS}
            {_FACT_FILTERS}
          ORDER BY i.job_id, i.intent_embedding <=> %(qvec)s::vector, i.id
        ) t
        ORDER BY score DESC, job_id
        LIMIT %(limit)s
        """,
        {"qvec": _vec(qvec), "limit": limit, **filters},
    ).fetchall()


def search_companies_lexical(
    conn: psycopg.Connection, *, phrases: Sequence[str], filters: dict[str, Any], limit: int = 200
) -> list[dict[str, Any]]:
    """§6[3a] list L3 — `ts_rank` over `company_signal`. No date filter: companies have no `posted_date`."""
    return conn.execute(
        _TSQUERY_CTE
        + f"""
        SELECT cs.company_id, ts_rank(cs.tsv, q.query) AS score
        FROM company_signal cs
        JOIN lead_company c ON c.id = cs.company_id
        CROSS JOIN q
        WHERE q.query IS NOT NULL
          AND cs.tsv @@ q.query
          {_FIRMO_FILTERS}
          {_FACT_FILTERS}
        ORDER BY score DESC, cs.company_id
        LIMIT %(limit)s
        """,
        {"phrases": list(phrases), "limit": limit, **filters},
    ).fetchall()


def search_companies_semantic(
    conn: psycopg.Connection, *, qvec: Sequence[float], filters: dict[str, Any], limit: int = 200
) -> list[dict[str, Any]]:
    """§6[3b] list L4 — exact cosine over `company_signal.embedding`."""
    return conn.execute(
        f"""
        SELECT cs.company_id, 1 - (cs.embedding <=> %(qvec)s::vector) AS score
        FROM company_signal cs
        JOIN lead_company c ON c.id = cs.company_id
        WHERE cs.embedding IS NOT NULL
          {_FIRMO_FILTERS}
          {_FACT_FILTERS}
        ORDER BY cs.embedding <=> %(qvec)s::vector, cs.company_id
        LIMIT %(limit)s
        """,
        {"qvec": _vec(qvec), "limit": limit, **filters},
    ).fetchall()


def fetch_company_details(
    conn: psycopg.Connection,
    *,
    company_ids: Sequence[int],
    industry_vecs: Sequence[Sequence[float]] = (),
) -> list[dict[str, Any]]:
    """The candidates' scoring inputs, plus §8.5's cosine.

    `industry_similarity` is computed here, in SQL, rather than by shipping 1536 floats per
    candidate into Python to dot-product them there. `industry_embedding` is `emb(industry_raw)`
    written at ingest, so this is exactly §8.5's
    `cosine(emb(company.industry_raw), emb(asked_industry))`. It is NULL when no industry was
    asked for — in which case §8.5 returns 1.0 and never looks.

    CHANGES-v2 §10 takes `max()` across the asked industries, so this takes a *list* of vectors
    and returns the best cosine among them. "manufacturing or automotive companies" must not
    penalise a manufacturer for being a poor automotive match — the user asked for either.

    The vectors cross the wire as a `text[]` of pgvector literals and are cast per element, which
    keeps the statement static and single-parameter for any number of asked industries.
    """
    if not company_ids:
        return []
    return conn.execute(
        """
        SELECT cs.company_id,
               c.name,
               c.domain,
               c.hq_city,
               c.hq_state,
               c.hq_country,
               c.segments,
               c.linkedin_url,
               cs.paraphrase,
               cs.technologies,
               cs.industry_raw,
               cs.industry_canonical,
               CASE
                 WHEN cardinality(%(ivecs)s::text[]) = 0 OR cs.industry_embedding IS NULL THEN NULL
                 ELSE (SELECT max(1 - (cs.industry_embedding <=> v::vector))
                       FROM unnest(%(ivecs)s::text[]) AS v)
               END AS industry_similarity
        FROM company_signal cs
        JOIN lead_company c ON c.id = cs.company_id
        WHERE cs.company_id = ANY(%(ids)s::bigint[])
        ORDER BY cs.company_id
        """,
        {"ids": list(company_ids), "ivecs": [_vec(v) for v in industry_vecs]},
    ).fetchall()


def fetch_company_technology_sets(
    conn: psycopg.Connection, *, company_ids: Sequence[int]
) -> list[dict[str, Any]]:
    """Each company's canonical technologies, split by the side of the corpus that asserts them.

    Exists to attribute an exclusion (§10's `excluded_by`): `_FACT_FILTERS` removes a company in
    SQL, but "removed" with no reason attached is exactly the opaque behaviour §2.1 is fighting.
    This returns the same two arrays the `&&` tested, so Python can name the group that fired.

    `uses_technologies` is the company profile's; `hiring_technologies` is the union across its
    postings — mirroring the two `NOT EXISTS` clauses exactly, so the report cannot disagree with
    the filter.
    """
    if not company_ids:
        return []
    return conn.execute(
        """
        SELECT cs.company_id,
               coalesce(cs.technologies, '{}') AS uses_technologies,
               coalesce((SELECT array_agg(DISTINCT t)
                         FROM job_signal js, unnest(js.technologies) AS t
                         WHERE js.company_id = cs.company_id), '{}') AS hiring_technologies
        FROM company_signal cs
        WHERE cs.company_id = ANY(%(ids)s::bigint[])
        ORDER BY cs.company_id
        """,
        {"ids": list(company_ids)},
    ).fetchall()


def fetch_jobs_for_companies(
    conn: psycopg.Connection, *, company_ids: Sequence[int], filters: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every surviving job for the candidate companies — coverage, recency, volume and evidence.

    Filtered identically to the job lists (`_JOB_FILTERS`), so `since_days`/`function` narrow the
    evidence and the axes computed from it, not just the retrieval.

    `title` is joined back from `lead_company_job` because `job_signal` stores only `title_norm`
    (lowercased, for §5.6's repost key), and evidence must show the posting as it was written.
    That join is a read; §2's "never writes to LeadPlus tables" is untouched.
    """
    if not company_ids:
        return []
    return conn.execute(
        f"""
        SELECT j.job_id, j.company_id, jj.title, j.title_norm, j.paraphrase, j.technologies,
               j.posted_date, j.function, j.seniority, j.initiative, j.is_repost, j.confidence,
               -- The finer grain, carried onto the evidence line so a card can show WHY beyond
               -- the paraphrase. Aggregated in a subquery rather than a join: joining ~5 intent
               -- rows per job would multiply the job rows and silently inflate `volume`.
               COALESCE((
                 SELECT array_agg(i.intent ORDER BY i.intent)
                 FROM job_intent i
                 WHERE i.job_id = j.job_id AND i.source = 'leadplus-intel'
               ), '{{}}') AS intents
        FROM job_signal j
        JOIN lead_company c ON c.id = j.company_id
        LEFT JOIN lead_company_job jj ON jj.id = j.job_id
        WHERE j.company_id = ANY(%(ids)s::bigint[])
          {_JOB_FILTERS}
          {_FIRMO_FILTERS}
        ORDER BY j.company_id, j.posted_date DESC NULLS LAST, j.job_id
        """,
        {"ids": list(company_ids), **filters},
    ).fetchall()


def fetch_known_technologies(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """The controlled technology vocabulary, for resolving a query term (rule 5).

    Coverage (§8.4) needs to know whether "SAP" is a *canonical technology* or just a word, so it
    can decide whether `technologies[]` or the prose is the authority for it. See score.py.
    """
    return conn.execute(
        "SELECT term, COALESCE(aliases, '{}') AS aliases FROM tech_canonical ORDER BY term"
    ).fetchall()
