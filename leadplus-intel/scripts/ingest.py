"""Ingest — normalize + embed the corpus into job_signal / company_signal. Phases 3-4.

    .venv/bin/python scripts/ingest.py --limit 25 --dry-run   # ALWAYS sample first (§5.7)
    .venv/bin/python scripts/ingest.py                        # full run
    .venv/bin/python scripts/ingest.py --jobs-only --limit 50

Operational contract (§5.7):

| Concern           | How                                                                       |
|-------------------|---------------------------------------------------------------------------|
| Idempotency       | key = (job_id, prompt_version, model) via the §5.2 NOT EXISTS clause      |
| Resumability      | keyset cursor; written rows ARE the checkpoint — kill and restart anytime |
| Failure isolation | per-row try/except -> ingest_dead_letter with the raw response            |
| Rate limits       | concurrency 20, exponential backoff + jitter (llm.py)                      |
| Cost control      | --limit N and --dry-run                                                    |
| Prompt versioning | prompt_version in every row; bumping it re-processes only the delta        |

Bump a prompt's `version:` front-matter and re-run: only rows lacking that version re-process.
**Never re-run the corpus to fix 50 rows.**
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap
import time

import _bootstrap  # noqa: F401

from intel import canonicalize, config, embed, llm, normalize, repository
from intel.models import CompanySignalRow, CompanySource, JobSignalRow, JobSource


class Stats:
    def __init__(self) -> None:
        self.seen = 0
        self.written = 0
        self.failed = 0

    def __str__(self) -> str:
        return f"seen={self.seen} written={self.written} failed={self.failed}"


# ---------------------------------------------------------------------------
# Reporting for the phase-3 eyeball gate
# ---------------------------------------------------------------------------


def show_job(job: JobSource, record) -> None:
    print(f"\n{'=' * 100}")
    print(f"job {job.id} · {job.title} · {job.department or '-'} · {job.type or '-'}")
    print(f"  company industry : {job.industry or '-'}   scraper tech: {job.technologies or []}")
    print(
        f"  ENUMS  initiative={record.initiative.value}  function={record.function.value}  "
        f"seniority={record.seniority.value}  engagement={record.engagement_type.value}  "
        f"confidence={record.confidence:.2f}"
    )
    print(f"  TECH   {record.technologies}")
    print("  PARA   " + textwrap.fill(record.paraphrase, 94, subsequent_indent="         "))


def show_company(company: CompanySource, record) -> None:
    print(f"\n{'=' * 100}")
    print(f"company {company.canonical_id} · {company.domain or '-'} · {company.industry or '-'}")
    print(f"  apollo tech : {company.technologies or []}")
    print(f"  scraped     : {company.scraped_technologies or []} / {company.scraped_tools or []}")
    print(f"  TECH   {record.technologies}   confidence={record.confidence:.2f}")
    print("  PARA   " + textwrap.fill(record.paraphrase, 94, subsequent_indent="         "))


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


async def ingest_jobs(conn, *, limit: int | None, dry_run: bool, show: bool) -> Stats:
    prompt_version = normalize.job_prompt_version()
    model = config.CHAT_MODEL
    stats = Stats()

    canonical = repository.canonical_member_map(conn)
    if not canonical:
        raise RuntimeError(
            "company_canonical is empty — run scripts/bootstrap_canonical.py first (§5.4)."
        )
    canonical_ids = set(canonical.values())

    tech = _tech_canonicalizer(conn)
    cursor = 0
    print(f"\njobs: prompt_version={prompt_version} model={model}")

    while True:
        remaining = None if limit is None else limit - stats.seen
        if remaining is not None and remaining <= 0:
            break
        batch_size = min(config.FETCH_BATCH_SIZE, remaining or config.FETCH_BATCH_SIZE)

        jobs = repository.fetch_jobs_to_normalize(
            conn, cursor=cursor, prompt_version=prompt_version, model=model, limit=batch_size
        )
        if not jobs:
            break
        cursor = max(j.id for j in jobs)
        stats.seen += len(jobs)

        # Stage 2 — normalize. Concurrency 20; exceptions come back as values, never raised.
        results = await llm.gather_limited([normalize.normalize_job(j) for j in jobs])

        rows: list[JobSignalRow] = []
        good: list[tuple[JobSource, object]] = []
        for job, result in zip(jobs, results):
            if isinstance(result, BaseException):
                stats.failed += 1
                raw = getattr(result, "raw_response", None)
                print(f"  ! job {job.id} failed: {type(result).__name__}: {result}")
                if not dry_run:
                    repository.record_dead_letter(
                        conn,
                        kind="job",
                        source_id=job.id,
                        prompt_version=prompt_version,
                        model=model,
                        error=f"{type(result).__name__}: {result}",
                        raw_response=raw,
                    )
                continue
            record, _raw = result  # type: ignore[misc]
            good.append((job, record))
            if show:
                show_job(job, record)

        if not good:
            continue

        # Stage 3 — canonicalise technologies (no-op until tech_canonical is seeded).
        tech_lists = []
        for _job, record in good:
            tech_lists.append(
                await tech.canonical_list(list(record.technologies)) if tech else list(record.technologies)
            )

        # Stage 4 — embed the PARAPHRASE only, batched 100 (§5.2 stage 4).
        vectors = await embed.embed_texts([r.paraphrase for _j, r in good])

        for (job, record), techs, vector in zip(good, tech_lists, vectors):
            rows.append(
                JobSignalRow(
                    job_id=job.id,
                    company_id=canonical.get(job.lead_company_id, job.lead_company_id),
                    initiative=record.initiative.value,
                    function=record.function.value,
                    seniority=record.seniority.value,
                    engagement_type=record.engagement_type.value,
                    technologies=techs,
                    paraphrase=record.paraphrase,
                    confidence=record.confidence,
                    title_norm=job.title_norm,
                    posted_date=job.posted_date,
                    embedding=vector,
                    prompt_version=prompt_version,
                    model=model,
                )
            )

        missing = [r.job_id for r in rows if r.company_id not in canonical_ids]
        if missing:
            raise RuntimeError(f"§5.4 invariant broken: non-canonical company_id for jobs {missing}")

        # Stage 5 — single upsert keyed on job_id: a crash mid-batch leaves no partial rows.
        if dry_run:
            stats.written += len(rows)
        else:
            stats.written += repository.upsert_job_signals(conn, rows)
            repository.clear_dead_letters(
                conn,
                kind="job",
                source_ids=[r.job_id for r in rows],
                prompt_version=prompt_version,
                model=model,
            )
        print(f"  jobs {stats}")

    return stats


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


async def ingest_companies(conn, *, limit: int | None, dry_run: bool, show: bool) -> Stats:
    prompt_version = normalize.company_prompt_version()
    model = config.CHAT_MODEL
    stats = Stats()
    tech = _tech_canonicalizer(conn)
    cursor = 0
    print(f"\ncompanies: prompt_version={prompt_version} model={model}")

    while True:
        remaining = None if limit is None else limit - stats.seen
        if remaining is not None and remaining <= 0:
            break
        batch_size = min(config.FETCH_BATCH_SIZE, remaining or config.FETCH_BATCH_SIZE)

        companies = repository.fetch_companies_to_normalize(
            conn, cursor=cursor, prompt_version=prompt_version, model=model, limit=batch_size
        )
        if not companies:
            break
        cursor = max(c.canonical_id for c in companies)
        stats.seen += len(companies)

        results = await llm.gather_limited([normalize.normalize_company(c) for c in companies])

        good: list[tuple[CompanySource, object]] = []
        for company, result in zip(companies, results):
            if isinstance(result, BaseException):
                stats.failed += 1
                print(f"  ! company {company.canonical_id} failed: {type(result).__name__}: {result}")
                if not dry_run:
                    repository.record_dead_letter(
                        conn,
                        kind="company",
                        source_id=company.canonical_id,
                        prompt_version=prompt_version,
                        model=model,
                        error=f"{type(result).__name__}: {result}",
                        raw_response=getattr(result, "raw_response", None),
                    )
                continue
            record, _raw = result  # type: ignore[misc]
            good.append((company, record))
            if show:
                show_company(company, record)

        if not good:
            continue

        tech_lists = []
        for _company, record in good:
            tech_lists.append(
                await tech.canonical_list(list(record.technologies)) if tech else list(record.technologies)
            )

        vectors = await embed.embed_texts([r.paraphrase for _c, r in good])

        rows = [
            CompanySignalRow(
                company_id=company.canonical_id,
                paraphrase=record.paraphrase,
                technologies=techs,
                industry_raw=company.industry,
                embedding=vector,
                prompt_version=prompt_version,
                model=model,
            )
            for (company, record), techs, vector in zip(good, tech_lists, vectors)
        ]

        if dry_run:
            stats.written += len(rows)
        else:
            stats.written += repository.upsert_company_signals(conn, rows)
            repository.clear_dead_letters(
                conn,
                kind="company",
                source_ids=[r.company_id for r in rows],
                prompt_version=prompt_version,
                model=model,
            )
        print(f"  companies {stats}")

    return stats


def _tech_canonicalizer(conn) -> canonicalize.TechCanonicalizer | None:
    """Stage 3 needs a seeded vocabulary.

    §13's build order runs the full ingest (phase 4) BEFORE tech_canonical exists (phase 5), so on
    a first run this returns None and raw extractions are stored. `bootstrap_tech.py` then seeds
    the vocabulary and canonicalises every stored row in place. Once seeded, later ingests
    canonicalise inline per §5.2 stage 3 and no second pass is needed.
    """
    if repository.fetch_tech_canonical(conn):
        return canonicalize.TechCanonicalizer(conn)
    print("  note: tech_canonical is empty — storing raw technologies; run bootstrap_tech.py next.")
    return None


# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    with repository.connect() as conn:
        job_stats = company_stats = None

        if not args.companies_only:
            job_stats = await ingest_jobs(
                conn, limit=args.limit, dry_run=args.dry_run, show=args.show or args.dry_run
            )
        if not args.jobs_only:
            company_stats = await ingest_companies(
                conn, limit=args.limit, dry_run=args.dry_run, show=args.show or args.dry_run
            )

        if not args.dry_run:
            # §5.6 — needs every paraphrase embedded, so it runs after the batches land.
            reposts = repository.mark_reposts(conn)
            print(f"\n§5.6 repost detection: marked {reposts} rows as is_repost")

        print(f"\n{'=' * 100}\nSUMMARY  ({time.monotonic() - started:.1f}s)")
        if job_stats:
            print(f"  jobs      : {job_stats}")
        if company_stats:
            print(f"  companies : {company_stats}")
        print("\n" + llm.USAGE.report())

        if args.dry_run:
            print("\n--dry-run: NOTHING was written. Read the paraphrases above before the full run.")
        else:
            counts = repository.verify_counts(conn)
            print(
                f"\nstored: job_signal={counts['job_signal']} "
                f"company_signal={counts['company_signal']} "
                f"dead_letters={counts['dead_letters']}"
            )
            if counts["dead_letters"]:
                print("  dead-lettered rows:")
                for row in repository.dead_letters(conn)[:20]:
                    print(f"    {row['kind']} {row['source_id']}: {row['error'][:120]}")

    return 0


def main() -> int:
    # A long run is usually watched through a redirect (`> ingest.log`), where Python block-buffers
    # stdout and the log stays empty for minutes. Line buffering keeps progress visible live.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description="Normalize + embed the corpus (§5.2, §5.3)")
    parser.add_argument("--limit", type=int, default=None, help="max rows per document type (§5.7)")
    parser.add_argument("--dry-run", action="store_true", help="call the LLM, print, write nothing")
    parser.add_argument("--show", action="store_true", help="print every record (implied by --dry-run)")
    parser.add_argument("--jobs-only", action="store_true")
    parser.add_argument("--companies-only", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
