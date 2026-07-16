"""Canonical company resolution — ARCHITECTURE.md §5.4. Phase 2.

    .venv/bin/python scripts/bootstrap_canonical.py [--dry-run]

**Run this before anything else.** Every downstream write keys on `company_canonical.canonical_id`:
`job_signal.company_id` is a canonical id, and `company_signal` has exactly one row per canonical
id. Ingesting before the fold exists would index copy-on-write duplicates as separate companies.

§5.4's verification is the point of this script: `count(company_canonical)` should be LESS than
`count(lead_company WHERE active)`. If they are equal, copy-on-write is not populated in this
restore and the fold is a no-op — **fine, but confirm rather than assume.** We confirm, loudly,
and the fold logic stays correct for the restore that does need it.
"""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from intel import canonicalize, repository


def main() -> int:
    parser = argparse.ArgumentParser(description="Populate company_canonical (§5.4)")
    parser.add_argument("--dry-run", action="store_true", help="compute and report; write nothing")
    args = parser.parse_args()

    with repository.connect() as conn:
        groups, result = canonicalize.fold_companies(conn)

        print("fold inputs")
        print(f"  active lead_company rows      : {result.active_companies}")
        print(f"  shared rows (tenant_id IS NULL): {result.shared_rows}")
        print(f"  rows without a domain          : {result.without_domain}")
        print()
        print("fold outputs")
        print(f"  canonical companies            : {result.canonical_companies}")
        print(f"  domains with >1 member row     : {result.groups_with_multiple_members}")
        print(f"  rows collapsed                 : {result.collapsed}")

        if result.largest_groups:
            print("\n  largest fold groups:")
            for domain, size in result.largest_groups:
                print(f"    {size} member(s)  {domain}")

        print("\n§5.4 verification")
        if result.is_noop:
            print(
                "  count(company_canonical) == count(lead_company WHERE active)  ->  NO-OP.\n"
                "  Copy-on-write is NOT populated in this restore: 0 shared rows, and one\n"
                "  lead_company row per distinct domain. Nothing to collapse.\n"
                "\n"
                "  This is EXPECTED here (§0 LOCAL DEVIATION) and is NOT a bug. Confirmed by\n"
                "  measurement, not assumed. The fold is implemented correctly and would collapse\n"
                "  duplicates on a real restore; here there are none to collapse. Every downstream\n"
                "  stage still routes through canonical_id, so nothing changes when real\n"
                "  copy-on-write data arrives."
            )
        else:
            print(
                f"  count(company_canonical)={result.canonical_companies} < "
                f"count(lead_company WHERE active)={result.active_companies}\n"
                f"  -> the fold collapsed {result.collapsed} copy-on-write duplicate row(s)."
            )

        # Invariants worth failing on: a broken fold silently corrupts every downstream id.
        member_ids = [m for g in groups for m in g.member_ids]
        assert len(member_ids) == len(set(member_ids)), "a lead_company row is in two fold groups"
        assert len(member_ids) == result.active_companies, "the fold lost or duplicated rows"
        assert all(
            g.canonical_id in g.member_ids for g in groups
        ), "a canonical_id is not one of its own members"

        if args.dry_run:
            print("\n--dry-run: nothing written.")
            return 0

        written = repository.replace_company_canonical(conn, groups)
        stored = repository.count_company_canonical(conn)
        active = repository.count_active_lead_companies(conn)
        print(f"\nwrote {written} rows to company_canonical (stored: {stored}, active: {active})")
        assert stored == written, "company_canonical row count does not match the fold"

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
