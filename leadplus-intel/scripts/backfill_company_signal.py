"""Index the FULL company pool with a deterministic template — no LLM, ~$0.13.

    .venv/bin/python scripts/backfill_company_signal.py --limit 25 --dry-run   # sample first
    .venv/bin/python scripts/backfill_company_signal.py                        # full run

## The bug this closes

`company_signal` held **462 of 22,876** canonical companies — 2%. Not because the other 22,414
were unindexable, but because `fetch_companies_to_normalize` walks only companies with a
text-bearing job, on the (then correct) grounds that LLM-normalizing the rest was ~$25 nobody had
approved. The consequence was invisible and total: every structural query — "manufacturers in
California with 500-1000 employees", "companies running SAP ECC" — returned **0 companies**, and
returned it in the voice of a working search. The leads were not missing from the answer; they
were missing from the index.

## Why a template is not a downgrade here

Read what the LLM was being paid for in `company_normalizer.md`: it reads `industry`, `hq_city`,
`employee_range` and `technologies[]` — already-structured columns — and writes them into a
sentence. There is no prose to comprehend and nothing to extract. It is a $25 string formatter
that returns a different string each time you run it. `normalize.template_paraphrase` does the
same job deterministically for the price of one embedding each.

The LLM is *not* being removed from the pipeline: it still reads every job description, where
comprehension is the whole task. This is rule 1 applied honestly — the LLM at the edges, where it
earns it.

## What is NOT touched

The **462 companies that already have an LLM paraphrase keep it.** A template built from
structured columns is thinner than a written one, and overwriting richer data with poorer data is
not a backfill. The scope predicate is "has no `company_signal` row **at all**"
(`repository._NO_COMPANY_SIGNAL`), not the usual `(prompt_version, model)` key — see its comment:
the usual key would have made all 462 look stale and eaten them on the first run.

`job_signal` and `job_intent` are untouched. The 2,761 jobs and 7,381 intents cost real money and
are correct; nothing here re-reads them.

## Operational contract (§5.7)

| Concern           | How                                                                    |
|-------------------|------------------------------------------------------------------------|
| Idempotency       | scope = "no company_signal row"; a written row leaves the set. Re-run = no-op |
| Resumability      | keyset cursor over `canonical_id`; written rows ARE the checkpoint     |
| Failure isolation | per-batch try/except -> ingest_dead_letter; one bad batch never aborts |
| Cost control      | --limit N and --dry-run                                                 |
| Prompt versioning | `prompt_version = company_template/v1`, `model = deterministic-template` |

`prompt_version` is bumped by hand in `TEMPLATE_VERSION` below when the template text changes —
§5.7 says changing a prompt is a data migration, and a template is a prompt with the model taken
out. Bumping it does **not** re-process anything on its own (scope is "no row at all"); re-writing
templated rows is `--reseed`, which is deliberately explicit and still cannot touch the 462.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap
import time

import _bootstrap  # noqa: F401

from intel import canonicalize, config, embed, llm, normalize, repository
from intel.models import CompanySignalRow, CompanySource

# §5.7's prompt_version, for a "prompt" that is a Python function. Bump when the template text
# changes; `--reseed` is what re-writes the rows.
TEMPLATE_VERSION = "company_template/v1"

# The `model` column is NOT NULL and it is provenance, not decoration: a row saying
# `deterministic-template` is a row nobody has to wonder about, and one query separates the
# templated 22,414 from the 462 that were written by gpt-4.1-mini. Do not put a model name here.
TEMPLATE_MODEL = "deterministic-template"


class Stats:
    def __init__(self) -> None:
        self.seen = 0
        self.written = 0
        self.failed = 0
        self.with_tech = 0
        self.with_industry = 0
        self.with_place = 0

    def __str__(self) -> str:
        return (
            f"seen={self.seen} written={self.written} failed={self.failed} "
            f"(industry={self.with_industry} place={self.with_place} tech={self.with_tech})"
        )


def show(company: CompanySource, row: CompanySignalRow) -> None:
    print(f"\n{'=' * 100}")
    print(f"company {company.canonical_id} · {company.domain or '-'}")
    print(
        f"  industry={company.industry or '-'} · place={company.hq_city or '-'}/"
        f"{company.hq_state or '-'} · employee_count={company.employee_count or '-'} "
        f"-> [{company.emp_low}, {company.emp_high}] · range_enum={company.employee_range or '-'}"
    )
    print(f"  raw tech    : {(company.technologies or [])[:8]}{' …' if len(company.technologies or []) > 8 else ''}")
    print(f"  canonical   : {row.technologies[:8]}{' …' if len(row.technologies) > 8 else ''} ({len(row.technologies)} total)")
    print(f"  industry_canonical : {row.industry_canonical or '(unresolved)'}")
    print("  PARA   " + textwrap.fill(row.paraphrase, 94, subsequent_indent="         "))


async def backfill(conn, *, limit: int | None, dry_run: bool, show_rows: bool, reseed: bool) -> Stats:
    stats = Stats()

    counts = repository.count_companies_for_template(conn)
    print(f"canonical companies      : {counts['canonical']}")
    print(f"  already have a signal  : {counts['canonical'] - counts['without_signal']}  <- kept, never overwritten")
    print(f"  to template            : {counts['without_signal']}")
    print(f"\nprompt_version={TEMPLATE_VERSION} model={TEMPLATE_MODEL}")

    # The §5.2 stage-3 ladder, reused rather than reinvented (it is the only thing allowed to say
    # what a technology is called). Apollo's ~4,511 values are already seeded as terms, so almost
    # every company technology resolves on the cheap `exact` rung with no embedding call.
    tech = canonicalize.TechCanonicalizer(conn)

    # §5.5, computed inline instead of in a second pass. `bootstrap_tech.canonicalise_industries`
    # does this for the LLM path afterwards; doing it here means a templated row is complete when
    # it lands, and `industry_canonical` is what the (now hard) industry filter selects on — a row
    # written without it would be invisible to every industry query until a second script ran.
    industries = await canonicalize.IndustryCanonicalizer.build(conn)
    print(f"§5.5 industry vocabulary : {len(industries.vocabulary)} values")

    # emb(industry_raw), embedded ONCE for the whole run rather than once per batch.
    #
    # §8.5 compares emb(company.industry_raw) to emb(asked_industry), so every row needs this
    # vector — but `industry` has 95 distinct values across 22,941 companies. Embedding per row
    # would be a 240x overspend; embedding per batch would still repeat the same ~40 values across
    # 224 batches. It is one call, up front, and the cost is a rounding error.
    industry_values = [row["value"] for row in repository.distinct_company_industries(conn)]
    industry_vectors: dict[str, list[float]] = {}
    if industry_values and not dry_run:
        industry_vectors = dict(zip(industry_values, await embed.embed_texts(industry_values)))
    print(f"industry_raw vectors     : {len(industry_vectors)} distinct values embedded once")

    cursor = 0
    while True:
        remaining = None if limit is None else limit - stats.seen
        if remaining is not None and remaining <= 0:
            break
        batch_size = min(config.FETCH_BATCH_SIZE, remaining or config.FETCH_BATCH_SIZE)

        companies = repository.fetch_companies_for_template(conn, cursor=cursor, limit=batch_size)
        if not companies:
            break
        cursor = max(c.canonical_id for c in companies)
        stats.seen += len(companies)

        try:
            rows = await _build_batch(
                companies,
                tech=tech,
                industries=industries,
                industry_vectors=industry_vectors,
                stats=stats,
                embed_vectors=not dry_run,
            )
        except Exception as exc:  # noqa: BLE001 — §5.7: one bad batch must not abort the run.
            stats.failed += len(companies)
            print(f"  ! batch at cursor {cursor} failed: {type(exc).__name__}: {exc}")
            if not dry_run:
                for company in companies:
                    repository.record_dead_letter(
                        conn,
                        kind="company_template",
                        source_id=company.canonical_id,
                        prompt_version=TEMPLATE_VERSION,
                        model=TEMPLATE_MODEL,
                        error=f"{type(exc).__name__}: {exc}",
                        raw_response=None,
                    )
            continue

        if show_rows:
            for company, row in zip(companies, rows):
                show(company, row)

        if dry_run:
            stats.written += len(rows)
        else:
            stats.written += repository.upsert_company_signals(conn, rows)
            repository.clear_dead_letters(
                conn,
                kind="company_template",
                source_ids=[r.company_id for r in rows],
                prompt_version=TEMPLATE_VERSION,
                model=TEMPLATE_MODEL,
            )
        if stats.seen % 1000 < config.FETCH_BATCH_SIZE or limit is not None:
            print(f"  {stats}")

    return stats


async def _build_batch(
    companies: list[CompanySource],
    *,
    tech: canonicalize.TechCanonicalizer,
    industries: canonicalize.IndustryCanonicalizer,
    industry_vectors: dict[str, list[float]],
    stats: Stats,
    embed_vectors: bool,
) -> list[CompanySignalRow]:
    """One batch: canonicalise -> template -> embed. The only network calls are embeddings.

    `embed_vectors=False` is `--dry-run`: build and print every paraphrase, spend nothing. §5.7
    says "always sample before the full run", and a sample that bills for the full run's
    embeddings is not a sample. The technology ladder may still embed an unseen raw term — that is
    the vocabulary's own cost and it is what makes the printed canonical list real rather than a
    guess about what the ladder would have said.
    """
    tech_lists: list[list[str]] = []
    for company in companies:
        # The same three sources `company_normalizer.md` trusts, in its order: curated
        # technographics first, then what the company's own job ads named. `scraped_services` is
        # excluded exactly as the prompt excludes it — "Systems Integration", "Predictive
        # Maintenance" are service categories, not products, and putting them in a controlled
        # technology vocabulary is how it stops being one.
        raw = [
            *(company.technologies or []),
            *(company.scraped_technologies or []),
            *(company.scraped_tools or []),
        ]
        tech_lists.append(await tech.canonical_list(raw))

    paraphrases = [
        normalize.template_paraphrase(company, techs)
        for company, techs in zip(companies, tech_lists)
    ]

    # §5.2 stage 4 — batch 100 per call; `embed_texts` already chunks at EMBED_BATCH_SIZE.
    vectors: list[list[float] | None] = (
        list(await embed.embed_texts(paraphrases)) if embed_vectors else [None] * len(paraphrases)
    )

    rows: list[CompanySignalRow] = []
    for company, techs, paraphrase, vector in zip(companies, tech_lists, paraphrases, vectors):
        raw_industry = (company.industry or "").strip() or None
        industry_vector = industry_vectors.get(raw_industry) if raw_industry else None
        resolution = industries.resolve(raw_industry, industry_vector)

        stats.with_tech += 1 if techs else 0
        stats.with_industry += 1 if raw_industry else 0
        stats.with_place += 1 if (company.hq_city or company.hq_state) else 0

        rows.append(
            CompanySignalRow(
                company_id=company.canonical_id,
                paraphrase=paraphrase,
                # NOT capped: `TEMPLATE_TECH_CAP` governs the prose only. This array is what
                # coverage (§8.4) and the negation guard rail (§2.1) match against by exact
                # element equality, and a truncated one would silently fail to exclude a company
                # that really does run the excluded product.
                technologies=techs,
                industry_raw=raw_industry,
                industry_canonical=resolution.canonical,
                industry_embedding=industry_vector,
                embedding=vector,
                prompt_version=TEMPLATE_VERSION,
                model=TEMPLATE_MODEL,
            )
        )
    return rows


async def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    with repository.connect() as conn:
        if args.reseed:
            # Scoped to OUR rows by (prompt_version, model). It physically cannot reach the 462:
            # they carry `company_normalizer/v1` + `gpt-4.1-mini`, so they are not in this DELETE's
            # range, and after it they still have rows — which is what keeps them out of scope.
            deleted = conn.execute(
                "DELETE FROM company_signal WHERE prompt_version = %s AND model = %s",
                (TEMPLATE_VERSION, TEMPLATE_MODEL),
            ).rowcount
            print(f"--reseed: deleted {deleted} templated rows (LLM-written rows untouched)\n")

        stats = await backfill(
            conn,
            limit=args.limit,
            dry_run=args.dry_run,
            show_rows=args.show or args.dry_run,
            reseed=args.reseed,
        )

        print(f"\n{'=' * 100}\nSUMMARY  ({time.monotonic() - started:.1f}s)")
        print(f"  {stats}")
        print("\n" + llm.USAGE.report())

        if args.dry_run:
            print("\n--dry-run: NOTHING was written. Read the paraphrases above before the full run.")
            return 0

        counts = repository.verify_counts(conn)
        remaining = repository.count_companies_for_template(conn)
        print(
            f"\nstored: company_signal={counts['company_signal']} "
            f"(no embedding: {counts['company_signal_no_embedding']}, "
            f"no industry_canonical: {counts['company_no_industry']}, "
            f"dims={counts['company_dims']})"
        )
        print(
            f"        canonical companies={remaining['canonical']} · "
            f"still unindexed={remaining['without_signal']}"
        )
        if counts["dead_letters"]:
            print("  dead-lettered rows:")
            for row in repository.dead_letters(conn)[:20]:
                print(f"    {row['kind']} {row['source_id']}: {row['error'][:120]}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(
        description="Template + embed every un-indexed canonical company (no LLM)"
    )
    parser.add_argument("--limit", type=int, default=None, help="max companies (§5.7 cost control)")
    parser.add_argument("--dry-run", action="store_true", help="build, print, embed nothing, write nothing")
    parser.add_argument("--show", action="store_true", help="print every row (implied by --dry-run)")
    parser.add_argument(
        "--reseed",
        action="store_true",
        help="delete and rewrite TEMPLATED rows (the LLM-written ones are never touched)",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
