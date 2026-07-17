"""Apply the derived schema (ARCHITECTURE.md §7). Phase 1.

    .venv/bin/python scripts/init_db.py [--drop]

Idempotent. `--drop` tears our tables down first — they are derived, disposable and rebuildable
from `lead_company*` + the prompts, so dropping them costs only a re-ingest.

This NEVER touches `lead_company`, `lead_company_job`, `lead_query` (§2, §7) or
`lead_company_job_intent` (another team's). `--drop` names our tables explicitly rather than
dropping a schema, and asserts the `lead_` prefix is absent, so it cannot reach any of them —
note in particular that OUR `job_intent` and THEIR `lead_company_job_intent` are different
tables, and only the first is on this list.
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
    # `intent_canonical` / `intent_review_queue` are deliberately absent: they were a category
    # error (an intent is a phrase, not a product) and are dropped, not recreated. See §5.8.
    "job_intent",  # OURS. `lead_company_job_intent` is theirs and is NOT here.
    "location_alias",
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
