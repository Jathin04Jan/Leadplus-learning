"""Profile the corpus — the §15 gates. Phase 0.

    .venv/bin/python scripts/profile_data.py

Runs against the local replica at :5433 (§3: never query RDS from the app). Read-only.

This exists to answer one question before a cent is spent on ingest: **is there signal here?**
The §0 LOCAL DEVIATION table records the honest answer — this is a synthetic replica, so these
gates replay the seed and cannot validate the thesis. Run it anyway: the numbers below are what
every downstream count is checked against.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

from intel import config, repository


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def main() -> int:
    print(f"database: {config.DATABASE_URL.rsplit('@', 1)[-1]}")

    with repository.connect() as conn:
        health = repository.health(conn)
        print(f"postgres: {health['postgres']}   pgvector: {health['pgvector']}")

        rule("§15.1 — companies with at least one active job")
        companies_with_jobs = repository.profile_companies_with_jobs(conn)
        print(f"{companies_with_jobs} companies")

        rule("§15.2 — active jobs by posted month")
        for row in repository.profile_jobs_by_month(conn):
            month = row["m"].date().isoformat()[:7] if row["m"] else "(null)"
            print(f"  {month}  {row['n']:>5}")

        rule("§15.3 — active job enrichment")
        jobs = repository.profile_jobs(conn)
        total = jobs["total"]
        print(f"  active jobs                : {total}")
        print(f"  with technologies[]        : {jobs['with_tech']} ({_pct(jobs['with_tech'], total)})")
        print(
            f"  with description > 200 ch  : {jobs['with_description']} "
            f"({_pct(jobs['with_description'], total)})"
        )
        print(f"  avg description length     : {jobs['avg_description_len']} chars")

        rule("§15.4 — active company enrichment")
        companies = repository.profile_company_rows(conn)
        ctotal = companies["total"]
        print(f"  active companies           : {ctotal}")
        print(
            f"  with Apollo technologies[] : {companies['with_apollo_tech']} "
            f"({_pct(companies['with_apollo_tech'], ctotal)})"
        )
        print(
            f"  with scraped_technologies[]: {companies['with_scraped_tech']} "
            f"({_pct(companies['with_scraped_tech'], ctotal)})"
        )

        rule("§5.4 — copy-on-write (is the canonical fold going to do anything?)")
        cow = repository.profile_copy_on_write(conn)
        print(f"  active companies           : {cow['active_companies']}")
        print(f"  shared rows (tenant_id NULL): {cow['shared_rows']}")
        print(f"  distinct lower(domain)     : {cow['distinct_domains']}")
        print(f"  rows without a domain      : {cow['without_domain']}")
        print(f"  excluded rows              : {cow['excluded']}")
        implied = cow["distinct_domains"] + cow["without_domain"]
        if implied >= cow["active_companies"]:
            print("  => fold will be a NO-OP: one lead_company row per real company.")
            print("     Expected here (§0 LOCAL DEVIATION). Confirmed, not assumed.")
        else:
            print(f"  => fold should collapse ~{cow['active_companies'] - implied} rows.")

        rule("§5.5 — industry vocabulary (lead_query)")
        for row in repository.profile_lead_query_types(conn):
            marker = "  <- §5.5 uses this" if row["type"] == "COMPANY_INDUSTRY" else ""
            print(f"  {row['type']:<20} {row['n']:>5}{marker}")
        vocabulary = repository.fetch_industry_vocabulary(conn)
        print(f"\n  COMPANY_INDUSTRY values ({len(vocabulary)}):")
        for value in vocabulary:
            print(f"    - {value}")
        if len(vocabulary) < 100:
            print(
                f"\n  NOTE: the spec expects 16,870 COMPANY_INDUSTRY rows; this replica has "
                f"{len(vocabulary)}.\n        Using it anyway — it is the vocabulary that exists (§0)."
            )

        rule("free-text industry distribution (lead_company.industry)")
        for row in repository.profile_industries(conn):
            print(f"  {str(row['industry']):<28} {row['n']:>4}")

        rule("gates")
        _gate("jobs with a usable description", jobs["with_description"], total, 0.5)
        _gate("jobs with technologies[]", jobs["with_tech"], total, 0.3)
        _gate("companies with any technographics", companies["with_apollo_tech"], ctotal, 0.3)
        print(
            "\n§0 LOCAL DEVIATION: this is a synthetic replica. These gates replay the seed and\n"
            "CANNOT validate the job-intent thesis — that needs the real corpus. They do confirm\n"
            "the corpus is dense enough to build and prove the architecture on."
        )

    return 0


def _pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):.1f}%" if total else "n/a"


def _gate(label: str, n: int, total: int, floor: float) -> None:
    ratio = (n / total) if total else 0.0
    status = "PASS" if ratio >= floor else "FAIL"
    print(f"  [{status}] {label}: {_pct(n, total)} (floor {floor:.0%})")


if __name__ == "__main__":
    raise SystemExit(main())
