"""Industry canonicalisation — the `industry_alias` table. **No LLM. $0.**

    .venv/bin/python scripts/bootstrap_industries.py [--dry-run]

Populates `industry_alias`, the table that lets *"manufacturing companies"* reach the 25 taxonomy
values that are manufacturing, and *"automotive"* reach exactly the 2 that are automotive.

Modelled on `bootstrap_locations.py`, deliberately and line for line: a hand-authored seed of the
words users type, a sweep of the corpus's own DISTINCT values so nothing present can be
unreachable, and an assertion at the end that every corpus value resolves to itself. That script
exists because a location vocabulary that misses a value fails **silently** — the query just
returns nothing, and an empty page reads as an answer. Industry has the identical failure mode and
now has the identical defence.

## Why this table exists — rule 2's premise was false

`ARCHITECTURE.md` rule 2 says `industry` must never be a filter because it is free text. Measured
on the real corpus, it is **not free text**: it is a 95-value closed taxonomy, LinkedIn-style,
declared in `lead_query WHERE type='COMPANY_INDUSTRY'` and never free-typed. Rule 2's own headline
— *filter on facts* — therefore covers it. The exception was written for a column this database
does not have.

The soft multiplier's cost, measured: *"companies in the automotive industry with revenue over
$100M"* returned Industrial-Machinery and Logistics companies at rank. There are **4** automotive
companies in the pool and **none** over $100M. The honest answer was 0; the tool gave a confident
page of wrong ones.

## Why the naive fix is worse, which is the reason for the SET

    "manufacturing"           -> a user means ~11,032 companies across 25 taxonomy values
    industry = 'Manufacturing' ->                1,067 companies

Hard-filtering the literal string deletes 90% of the correct answers — rule 2's warning coming
true, for a different reason than rule 2 gave. So an alias maps to a **set**, the chip expands
through it, and *then* it filters. Expanding-then-filtering is the whole design.

## The two knobs, and what `industry_strict` now means

    default            -> hard filter on the EXPANDED set   ("manufacturing" -> 25 values)
    industry_strict    -> hard filter on the EXACT value    ("strictly manufacturing" -> 1)

That preserves CHANGES-v2 §5's contract — the insisting word ("strictly", "only", "must be") is
the user overriding the default and accepting the deletions — while making the default useful
instead of merely soft. What §5 could not anticipate is that the *default* is now also a filter;
the override narrows it rather than turning it on.
"""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from intel import repository
from intel.canonicalize import industry_key

# ---------------------------------------------------------------------------
# The seed: user word -> the taxonomy values it covers.
#
# HOW THESE WERE CHOSEN, because "judgement" is not a method. Every `canonical` below is a value
# `lead_company.industry` actually holds — the script asserts that, and a typo fails the run
# rather than silently seeding an alias that matches nothing. The `alias` side is the word a
# salesperson types. The membership question is always the same one: **would a marketer searching
# this word consider this company a correct answer, or a wrong one they would have to skip?**
#
# The rule is COVERAGE, not resemblance. `Machinery Manufacturing` is manufacturing. `Automation`
# is not (it is a service/solutions category), and neither is `Robotics & AI Solution Providers` —
# those companies sell automation, they do not manufacture. That distinction is arguable and it is
# argued in the open here, which is the point: a seed is a classification, and this file is the
# only place to look, exactly as `SEED_TECH` is for technologies.
#
# NOT SEEDED, deliberately: no alias for a word the taxonomy cannot honour. There is no
# `enterprise`, no `tech startup`, no `mid-market`. An alias that fakes a match is worse than a
# miss, because a miss is REPORTED (`retrieve._expand_industries` says so and returns an honest
# zero) and a fake is not.
# ---------------------------------------------------------------------------

# The manufacturing family. 25 values, ~11,032 companies. This is the set `industry =
# 'Manufacturing'` (1,067) was silently standing in for.
_MANUFACTURING = [
    "Manufacturing",
    "AI and Smart manufacturing industries",
    "Appliances, Electrical, and Electronics Manufacturing",
    "Food and Beverage Manufacturing",
    "Machinery Manufacturing",
    "Medical Equipment Manufacturing",
    "Pharmaceutical Manufacturing",
    "Industrial Machinery Manufacturing",
    "Plastics Manufacturing",
    "Packaging and Containers Manufacturing",
    "Defense and Space Manufacturing",
    "Aviation and Aerospace Component Manufacturing",
    "Furniture and Home Furnishings Manufacturing",
    "Automation Machinery Manufacturing",
    "Computers and Electronics Manufacturing",
    "Personal Care Product Manufacturing",
    "Computer Hardware Manufacturing",
    "Beverage Manufacturing",
    "Glass, Ceramics and Concrete Manufacturing",
    "Agriculture, Construction, Mining Machinery Manufacturing",
    "Railroad Equipment Manufacturing",
    "Electrical Equipment Manufacturing",
    "HVAC and Refrigeration Equipment Manufacturing",
    "Apparel Manufacturing",
    "Dairy Product Manufacturing",
    "Measuring and Control Instrument Manufacturing",
    "Animal Feed Manufacturing",
    "Electric Lighting Equipment Manufacturing",
    "Commercial and Service Industry Machinery Manufacturing",
    "Agricultural Chemical Manufacturing",
    "Construction Hardware Manufacturing",
    "Audio and Video Equipment Manufacturing",
    "Communications Equipment Manufacturing",
    "Architectural and Structural Metal Manufacturing",
    "Climate Technology Product Manufacturing",
    "Rubber Products Manufacturing",
    "Robot Manufacturing",
    "Meat Products Manufacturing",
    "Semiconductor Manufacturing",
    "Motor Vehicle Manufacturing",
    "Fashion Accessories Manufacturing",
    "Footwear Manufacturing",
    "Baked Goods Manufacturing",
    "Household and Institutional Furniture Manufacturing",
    "Leather Product Manufacturing",
    "Engines and Power Transmission Equipment Manufacturing",
    "Mechanical or Industrial Engineering",
]

SEED_INDUSTRY: dict[str, list[str]] = {
    # -- the big family words -------------------------------------------------------------
    "manufacturing": _MANUFACTURING,
    "manufacturer": _MANUFACTURING,
    "manufacturers": _MANUFACTURING,
    "factories": _MANUFACTURING,
    "industrial": _MANUFACTURING,
    "industrials": _MANUFACTURING,
    "industry": _MANUFACTURING,
    "production": _MANUFACTURING,
    # `Automotive` (1 company) + `Motor Vehicle Manufacturing` (3). Four companies in total — and
    # saying so is the product. The soft multiplier used to answer this with logistics firms.
    "automotive": ["Automotive", "Motor Vehicle Manufacturing"],
    "auto": ["Automotive", "Motor Vehicle Manufacturing"],
    "car makers": ["Automotive", "Motor Vehicle Manufacturing"],
    "vehicles": ["Automotive", "Motor Vehicle Manufacturing"],
    "logistics": [
        "Transportation, Logistics, Supply Chain and Storage",
        "Logistics",
        "Warehousing and Storage",
        "Truck Transportation",
        "Maritime Transportation",
    ],
    "supply chain": [
        "Transportation, Logistics, Supply Chain and Storage",
        "Logistics",
        "Warehousing and Storage",
    ],
    "transportation": [
        "Transportation, Logistics, Supply Chain and Storage",
        "Truck Transportation",
        "Maritime Transportation",
        "Airlines and Aviation",
    ],
    "shipping": [
        "Transportation, Logistics, Supply Chain and Storage",
        "Maritime Transportation",
        "Truck Transportation",
    ],
    "warehousing": ["Warehousing and Storage", "Transportation, Logistics, Supply Chain and Storage"],
    "healthcare": [
        "Hospitals and Health Care",
        "Medical",
        "Medical Devices",
        "Medical Equipment Manufacturing",
    ],
    "health care": [
        "Hospitals and Health Care",
        "Medical",
        "Medical Devices",
        "Medical Equipment Manufacturing",
    ],
    "hospitals": ["Hospitals and Health Care"],
    "medical": ["Medical", "Medical Devices", "Medical Equipment Manufacturing", "Hospitals and Health Care"],
    "pharma": ["Pharmaceutical Manufacturing"],
    "pharmaceutical": ["Pharmaceutical Manufacturing"],
    "pharmaceuticals": ["Pharmaceutical Manufacturing"],
    "life sciences": ["Pharmaceutical Manufacturing", "Biotechnology Research", "Biotechnology"],
    "biotech": ["Biotechnology Research", "Biotechnology"],
    "biotechnology": ["Biotechnology Research", "Biotechnology"],
    # `Tech`/`Technology` is a real user word for a real taxonomy value. It is seeded because it
    # maps HONESTLY, unlike `Enterprise` — which is a size word, is not an industry, is not a
    # segment on this corpus either, and is therefore deliberately absent from this file.
    "tech": ["Technology, Information and Internet", "Technology", "Software", "IT System Design Services"],
    "technology": ["Technology, Information and Internet", "Technology", "Software", "IT System Design Services"],
    "software": ["Software", "Technology, Information and Internet"],
    "saas": ["Software", "Technology, Information and Internet"],
    "it": ["IT System Design Services", "Technology, Information and Internet"],
    "internet": ["Technology, Information and Internet", "Internet Publishing"],
    "finance": ["Financial Services", "Investment Management", "Finance Tech", "Holding Companies"],
    "financial services": ["Financial Services", "Investment Management", "Finance Tech"],
    "fintech": ["Finance Tech", "Financial Services"],
    "banking": ["Financial Services"],
    "insurance": ["Financial Services"],
    "retail": [
        "Retail",
        "Retail Office Equipment",
        "Retail Appliances, Electrical, and Electronic Equipment",
        "Wholesale",
    ],
    "wholesale": ["Wholesale", "International Trade and Development"],
    "aerospace": [
        "Aviation and Aerospace Component Manufacturing",
        "Aviation & Aerospace",
        "Defense and Space Manufacturing",
        "Defense & Space",
        "Airlines and Aviation",
    ],
    "aviation": ["Aviation and Aerospace Component Manufacturing", "Aviation & Aerospace", "Airlines and Aviation"],
    "defense": ["Defense and Space Manufacturing", "Defense & Space"],
    "defence": ["Defense and Space Manufacturing", "Defense & Space"],
    "robotics": ["Robotics & AI Solution Providers", "Robotics Engineering", "Robot Manufacturing"],
    "automation": ["Automation", "Industrial Automation", "Automation Machinery Manufacturing"],
    "food": [
        "Food and Beverage Manufacturing",
        "Food and Beverage Services",
        "Food & Beverages",
        "Beverage Manufacturing",
        "Dairy Product Manufacturing",
        "Meat Products Manufacturing",
        "Baked Goods Manufacturing",
        "Animal Feed Manufacturing",
    ],
    "food and beverage": [
        "Food and Beverage Manufacturing",
        "Food and Beverage Services",
        "Food & Beverages",
        "Beverage Manufacturing",
    ],
    "beverage": ["Beverage Manufacturing", "Food and Beverage Manufacturing", "Food & Beverages"],
    "chemicals": ["Agricultural Chemical Manufacturing"],
    "electronics": [
        "Appliances, Electrical, and Electronics Manufacturing",
        "Computers and Electronics Manufacturing",
        "Computer Hardware Manufacturing",
        "Semiconductor Manufacturing",
        "Electrical Equipment Manufacturing",
        "Audio and Video Equipment Manufacturing",
    ],
    "semiconductors": ["Semiconductor Manufacturing"],
    "machinery": [
        "Machinery Manufacturing",
        "Industrial Machinery Manufacturing",
        "Automation Machinery Manufacturing",
        "Agriculture, Construction, Mining Machinery Manufacturing",
        "Commercial and Service Industry Machinery Manufacturing",
    ],
    "education": ["Education", "Higher Education"],
    "energy": ["Oil and Gas", "Environmental Services"],
    "oil and gas": ["Oil and Gas"],
    "mining": ["Mining", "Agriculture, Construction, Mining Machinery Manufacturing"],
    "construction": ["Construction", "Construction Hardware Manufacturing"],
    "telecom": ["Telecommunications", "Wireless Services", "Communications Equipment Manufacturing"],
    "telecommunications": ["Telecommunications", "Wireless Services", "Communications Equipment Manufacturing"],
    "packaging": ["Packaging and Containers Manufacturing"],
    "plastics": ["Plastics Manufacturing", "Rubber Products Manufacturing"],
    "furniture": ["Furniture and Home Furnishings Manufacturing", "Household and Institutional Furniture Manufacturing"],
    "apparel": ["Apparel Manufacturing", "Footwear Manufacturing", "Fashion Accessories Manufacturing", "Leather Product Manufacturing"],
    "agriculture": ["Agricultural Chemical Manufacturing", "Animal Feed Manufacturing", "Agriculture, Construction, Mining Machinery Manufacturing"],
}


class SeedError(RuntimeError):
    """The seed names an industry the corpus does not hold. Never caught — see `main`."""


def build_rows(corpus: list[str]) -> tuple[list[tuple[str, str, str]], list[str], list[str]]:
    """The seed + the corpus sweep, as `industry_alias` rows.

    Returns (rows, unknown_seed_values, uncovered_corpus_values).

    Two guarantees, and they pull in opposite directions on purpose:

      * **every corpus value resolves to itself** (`kind='exact'`). This is `bootstrap_locations`'s
        rule: the corpus is the authority on what exists, and a value present in the column but
        absent from the vocabulary is a query that silently returns nothing. So a user typing an
        exact taxonomy value always works, seed or no seed.
      * **every seeded value must exist in the corpus.** A `canonical` that no company carries is a
        dead alias — it would widen an expansion by a value that can never match, which reads as
        working and is not. That is `SeedError`, and it is fatal.
    """
    by_key = {industry_key(value): value for value in corpus}

    rows: set[tuple[str, str, str]] = set()
    unknown: list[str] = []

    # -- the exact self-mappings, from the corpus itself ------------------------------------
    for value in corpus:
        rows.add((industry_key(value), value, "exact"))

    # -- the seeded family words -------------------------------------------------------------
    for alias, values in SEED_INDUSTRY.items():
        for value in values:
            actual = by_key.get(industry_key(value))
            if actual is None:
                unknown.append(f"{alias!r} -> {value!r}")
                continue
            # `actual`, not `value`: the corpus's own spelling wins, because that is what
            # `company_signal.industry_canonical` holds and what the filter compares against.
            rows.add((industry_key(alias), actual, "family"))

    covered = {canonical for _alias, canonical, kind in rows if kind == "family"}
    uncovered = sorted(v for v in corpus if v not in covered)
    return sorted(rows), unknown, uncovered


def main() -> int:
    parser = argparse.ArgumentParser(description="Populate industry_alias (no LLM)")
    parser.add_argument("--dry-run", action="store_true", help="compute and report; write nothing")
    args = parser.parse_args()

    with repository.connect() as conn:
        weights = {row["value"]: row["n"] for row in repository.distinct_company_industries(conn)}
        corpus = list(weights)
        declared = repository.fetch_industry_vocabulary(conn)

        print("corpus (READ-ONLY from lead_company)")
        print(f"  distinct lead_company.industry values      : {len(corpus)}")
        print(f"  declared lead_query COMPANY_INDUSTRY values: {len(declared)}")
        only_in_corpus = sorted(set(corpus) - set(declared))
        print(
            f"  in the column but NOT declared              : {len(only_in_corpus)} — "
            f"{', '.join(only_in_corpus[:8])}{' …' if len(only_in_corpus) > 8 else ''}"
        )
        print("  -> this is why the sweep reads the COLUMN, not just the declared list: a value")
        print("     the vocabulary misses is a query that silently returns nothing.")

        rows, unknown, uncovered = build_rows(corpus)

        if unknown:
            raise SeedError(
                "SEED_INDUSTRY names industry values this corpus does not hold:\n  "
                + "\n  ".join(unknown)
                + "\n  A dead alias widens an expansion by a value nothing can match — it reads "
                "as working and is not. Fix the seed."
            )

        print("\nseed")
        print(f"  family words : {len(SEED_INDUSTRY)}")
        print(f"  alias rows   : {len(rows)}  ({len({a for a, _, _ in rows})} distinct aliases)")

        print("\nthe measurement this table exists for")
        for alias in ("manufacturing", "automotive", "logistics", "healthcare"):
            values = sorted({c for a, c, _ in rows if a == alias})
            total = sum(weights.get(v, 0) for v in values)
            print(f"  {alias:<14}-> {len(values):>2} taxonomy values, {total:>6} companies")
        print(
            f"  {'(literal)':<14}-> industry = 'Manufacturing' alone: "
            f"{weights.get('Manufacturing', 0)} companies — the 90% the old hard filter would have deleted"
        )

        if uncovered:
            print(
                f"\n{len(uncovered)} taxonomy value(s) reachable ONLY by their exact name (no family "
                f"word maps to them):"
            )
            print(f"  {', '.join(uncovered[:12])}{' …' if len(uncovered) > 12 else ''}")
            print("  That is fine and honest — they are reachable, just not by a synonym.")

        # A corpus value that cannot resolve to itself is a silent zero-result query.
        for value in corpus:
            assert any(a == industry_key(value) for a, _, _ in rows), (
                f"corpus industry {value!r} does not resolve — a query for it would silently "
                f"return nothing"
            )

        if args.dry_run:
            print("\n--dry-run: nothing written.")
            return 0

        written = repository.replace_industry_aliases(conn, rows)
        stored = repository.count_industry_aliases(conn)
        print(f"\nwrote {written} rows to industry_alias")
        print(
            f"  stored: {stored['rows']} rows · {stored['aliases']} aliases -> "
            f"{stored['canonicals']} taxonomy values (family {stored['family']}, exact {stored['exact']})"
        )
        assert stored["rows"] == written, "industry_alias row count does not match the seed"

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
