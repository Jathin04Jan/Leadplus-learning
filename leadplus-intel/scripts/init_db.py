"""Apply the derived schema (ARCHITECTURE.md §7). Phase 1.

    .venv/bin/python scripts/init_db.py [--drop]

Idempotent. `--drop` tears our tables down first — they are derived, disposable and rebuildable
from `lead_company*` + the prompts, so dropping them costs only a re-ingest.

This NEVER touches `lead_company`, `lead_company_job` or `lead_query` (§2, §7). `--drop` names
our six tables explicitly rather than dropping a schema, so it cannot reach a LeadPlus table.
"""

from __future__ import annotations

import argparse
import json

import _bootstrap  # noqa: F401

from intel import repository

OUR_TABLES = (
    "ingest_dead_letter",
    "tech_review_queue",
    "tech_canonical",
    "company_signal",
    "job_signal",
    "company_canonical",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply sql/schema.sql")
    parser.add_argument(
        "--drop", action="store_true", help="drop our derived tables first (never LeadPlus tables)"
    )
    args = parser.parse_args()

    with repository.connect() as conn:
        if args.drop:
            for table in OUR_TABLES:
                assert not table.startswith("lead_"), f"refusing to drop {table}"
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            print(f"dropped: {', '.join(OUR_TABLES)}")

        repository.apply_schema(conn)
        print("applied sql/schema.sql")

        triggers = repository.assert_read_only_respected(conn)
        if triggers:
            print(f"WARNING: triggers found on LeadPlus tables: {triggers}")

        print(json.dumps(repository.health(conn), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
