"""SEARCH-EXPLAINED §9 — build the contact role census (`contact_signal`). No LLM, ~$0.30.

    .venv/bin/python scripts/ingest_contacts.py --limit 200 --dry-run   # sample first
    .venv/bin/python scripts/ingest_contacts.py                         # full run

## What this builds — and what it refuses to build

A **role census, not a contact database.** For each active contact attached to a canonical
company it stores a title, a function, a seniority, a department and (from Apollo
`employment_history`) a Big-4-alumnus flag and the current-role start date. It stores **no**
`first_name`, `last_name`, `full_name`, `email`, `phonee164`, `linkedin_url` or `notes` — those
are never SELECTed, so they cannot leak. The output identifies a seat, not a person: "the CFO of
Acme" is still pseudonymous (§9's honest caveat), but it is the smallest thing that answers
"which companies have a CFO".

## Why deterministic (no LLM)

A title is already structured. `contacts.classify_function`/`classify_seniority` read it with a
regex ladder more consistently than a model would, and consistency is the product (§1). Rule 1
keeps the LLM at the edges; there is no prose to comprehend here, so it stays out. The only spend
is one embedding per contact (~$0.30 for ~54k).

## Operational contract (§5.7)

| Concern           | How                                                                      |
|-------------------|--------------------------------------------------------------------------|
| Idempotency       | scope = "no contact_signal row for this version+model"; re-run = delta    |
| Resumability      | keyset cursor over `lead_contact.id`; written rows ARE the checkpoint     |
| Failure isolation | per-batch try/except -> ingest_dead_letter; one bad batch never aborts    |
| Cost control      | --limit N and --dry-run                                                   |
| Prompt versioning | `prompt_version = contact_census/v1`, `model = deterministic-census`     |

`lead_contact_normalized_title` is EMPTY on this clone (0 rows), so §9's "reuse it" cannot apply —
the classification derives function/seniority from the title text directly. Reported honestly at
the end.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import _bootstrap  # noqa: F401

from intel import config, contacts, embed, llm, repository
from intel.models import ContactSignalRow, ContactSource

CENSUS_VERSION = "contact_census/v1"
# `deterministic-census` because the classification is a Python function, not a model. One query
# then separates these rows from anything an LLM ever wrote. Do not put a model name here.
CENSUS_MODEL = "deterministic-census"


class Stats:
    def __init__(self) -> None:
        self.seen = 0
        self.indexed = 0
        self.written = 0
        self.failed = 0
        self.skipped_no_canonical = 0
        self.big4 = 0
        self.finance = 0

    def __str__(self) -> str:
        return (
            f"seen={self.seen} indexed={self.indexed} written={self.written} failed={self.failed} "
            f"(skipped_no_canonical={self.skipped_no_canonical} big4_alumni={self.big4} "
            f"finance={self.finance})"
        )


def build_row(
    contact: ContactSource,
    *,
    company_id: int,
    big4: dict[int, contacts.Big4History],
    stats: Stats,
) -> ContactSignalRow:
    function = contacts.classify_function(contact.title, contact.department)
    seniority = contacts.classify_seniority(contact.title, contact.seniority)
    history = big4.get(contact.lead_contact_id, contacts.Big4History(False, None, None))

    if history.is_big4_alum:
        stats.big4 += 1
    if function.value == "FINANCE":
        stats.finance += 1

    census = contacts.census_text(
        canonical_title=contact.title,
        function=function,
        seniority=seniority,
        department=contact.department,
        is_big4_alum=history.is_big4_alum,
        prior_employer=history.prior_employer,
    )
    return ContactSignalRow(
        company_id=company_id,
        lead_contact_id=contact.lead_contact_id,
        canonical_title=(contact.title or "").strip() or None,
        seniority=seniority.value,
        function=function.value,
        department=(contact.department or "").strip() or None,
        is_big4_alum=history.is_big4_alum,
        prior_employer=history.prior_employer,
        landed_at=history.landed_at,
        census_text=census,
        prompt_version=CENSUS_VERSION,
        model=CENSUS_MODEL,
    )


async def ingest(conn, *, limit: int | None, dry_run: bool, show_rows: bool) -> Stats:
    stats = Stats()

    total = repository.count_indexable_contacts(conn)
    print(f"active contacts with a company : {total}")

    # §5.4 — map every member lead_company.id -> canonical_id, in Python. Doing the membership test
    # here avoids an `ANY(member_ids)` seq-scan join on every contact fetch.
    member_map = repository.canonical_member_map(conn)
    print(f"canonical member map           : {len(member_map)} member ids")

    # Big-4 evidence, parsed ONCE from the 4,659 Apollo blobs that carry employment history.
    print("parsing Apollo employment_history for Big-4 alumni …")
    big4: dict[int, contacts.Big4History] = {}
    landed_known = 0
    for row in repository.fetch_big4_apollo_data(conn):
        h = contacts.big4_history(row["data"])
        if h.is_big4_alum or h.landed_at is not None:
            big4[row["lead_contact_id"]] = h
            if h.landed_at is not None:
                landed_known += 1
    n_alum = sum(1 for h in big4.values() if h.is_big4_alum)
    print(f"  Big-4 alumni (past employer) : {n_alum}   (contacts with a landing date: {landed_known})")
    print(f"\nprompt_version={CENSUS_VERSION} model={CENSUS_MODEL}\n")

    cursor = 0
    while True:
        remaining = None if limit is None else limit - stats.seen
        if remaining is not None and remaining <= 0:
            break
        batch_size = min(config.FETCH_BATCH_SIZE, remaining or config.FETCH_BATCH_SIZE)

        source = repository.fetch_contacts_to_index(
            conn, cursor=cursor, prompt_version=CENSUS_VERSION, model=CENSUS_MODEL, limit=batch_size
        )
        if not source:
            break
        cursor = max(c.lead_contact_id for c in source)
        stats.seen += len(source)

        # Scope (§9): only contacts whose company is canonical. The rest are counted and skipped.
        rows: list[ContactSignalRow] = []
        for contact in source:
            company_id = member_map.get(contact.lead_company_id)
            if company_id is None:
                stats.skipped_no_canonical += 1
                continue
            rows.append(build_row(contact, company_id=company_id, big4=big4, stats=stats))
        stats.indexed += len(rows)
        if not rows:
            continue

        try:
            if not dry_run:
                vectors = await embed.embed_texts([r.census_text for r in rows])
                for r, v in zip(rows, vectors):
                    r.embedding = v
        except Exception as exc:  # noqa: BLE001 — §5.7: one bad batch must not abort the run.
            stats.failed += len(rows)
            print(f"  ! batch at cursor {cursor} failed: {type(exc).__name__}: {exc}")
            if not dry_run:
                for r in rows:
                    repository.record_dead_letter(
                        conn,
                        kind="contact_census",
                        source_id=r.lead_contact_id,
                        prompt_version=CENSUS_VERSION,
                        model=CENSUS_MODEL,
                        error=f"{type(exc).__name__}: {exc}",
                        raw_response=None,
                    )
            continue

        if show_rows:
            for r in rows[:15]:
                flag = f" · Big-4 alum ({r.prior_employer})" if r.is_big4_alum else ""
                print(f"  [{r.function}/{r.seniority}] {r.canonical_title or '-'}{flag}")
                print(f"       census: {r.census_text}")

        if dry_run:
            stats.written += len(rows)
        else:
            stats.written += repository.upsert_contact_signals(conn, rows)
            repository.clear_dead_letters(
                conn,
                kind="contact_census",
                source_ids=[r.lead_contact_id for r in rows],
                prompt_version=CENSUS_VERSION,
                model=CENSUS_MODEL,
            )
        if stats.seen % 5000 < config.FETCH_BATCH_SIZE or limit is not None:
            print(f"  {stats}")

    return stats


async def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    with repository.connect() as conn:
        if args.reseed and not args.dry_run:
            deleted = conn.execute(
                "DELETE FROM contact_signal WHERE prompt_version = %s AND model = %s",
                (CENSUS_VERSION, CENSUS_MODEL),
            ).rowcount
            print(f"--reseed: deleted {deleted} census rows\n")

        stats = await ingest(
            conn, limit=args.limit, dry_run=args.dry_run, show_rows=args.show or args.dry_run
        )

        print(f"\n{'=' * 100}\nSUMMARY  ({time.monotonic() - started:.1f}s)")
        print(f"  {stats}")
        print("\n" + llm.USAGE.report())

        if args.dry_run:
            print("\n--dry-run: NOTHING written. Read the census rows above before the full run.")
            return 0

        counts = repository.contact_counts(conn)
        pii = repository.contact_pii_columns(conn)
        orphans = repository.orphan_contact_signals(conn)
        print(
            f"\nstored: contact_signal={counts['rows']} "
            f"(no embedding: {counts['no_embedding']}, dims={counts['dims']}, "
            f"dim_variants={counts['dim_variants']}, companies={counts['companies']})"
        )
        print(
            f"        Big-4 alumni={counts['big4_alumni']} · finance roles={counts['finance']} · "
            f"finance leaders (C/VP)={counts['finance_leaders']} · orphan company_ids={orphans}"
        )
        print(f"        PII columns present: {pii or 'NONE — role census only, no identity stored'}")
        if counts["dead_letters"] if "dead_letters" in counts else False:  # pragma: no cover
            pass
    return 0


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="Build the contact role census (no LLM)")
    parser.add_argument("--limit", type=int, default=None, help="max contacts (§5.7 cost control)")
    parser.add_argument("--dry-run", action="store_true", help="classify + print, embed/write nothing")
    parser.add_argument("--show", action="store_true", help="print sample rows (implied by --dry-run)")
    parser.add_argument("--reseed", action="store_true", help="delete and rewrite census rows")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
