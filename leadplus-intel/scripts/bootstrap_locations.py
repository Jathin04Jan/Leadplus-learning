"""Location canonicalisation — CHANGES-v2 §3.1. **No LLM. $0.**

    .venv/bin/python scripts/bootstrap_locations.py [--dry-run]

Populates `location_alias`, the table that lets *"in California"*, *"in CA"* and *"in calif"* all
reach the same rows. Without it, §3.2's filter is a string equality against whatever spelling the
user happened to type, and a miss returns **zero results with no error** — the worst possible
failure, because an empty page looks like an answer.

## The inversion — read this before editing the seed

The original spec assumed `lead_company.hq_state` holds `CA` and made `ca` the canonical value.
**It does not.** CHANGES-v2 §0 measured this restore: `hq_state` holds **full names**
(`California`, `Texas`), `hq_country` holds `United States`. Building it as originally written
would have produced a table whose canonical values match nothing at all, and the symptom would
have been silent: every location query returning zero.

So: **canonical = the full lowercase name**, and the abbreviations are aliases pointing at it.

    ('ca', 'california', 'state')  ('calif', 'california', 'state')  ('california', 'california', 'state')

Each canonical is also its own alias, because the parser usually emits the full name already and
that path must not need a special case.

## Why the corpus sweep at the end

The static lists below are a hypothesis about what place names exist. A hypothesis that misses a
value present in `hq_city` fails silently, exactly like the inversion above. So after seeding, the
script reads the restore's own DISTINCT `hq_state`/`hq_city`/`hq_country` values (READ-ONLY — §2
is untouched) and guarantees every one is resolvable to itself. The seed gives us the *aliases*
users type; the corpus gives us the *canonicals* that actually exist. Both are needed, and the
script reports any corpus value the static seed did not already know.
"""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from intel import repository
from intel.canonicalize import location_key

# ---------------------------------------------------------------------------
# 50 US states + DC. canonical = full lowercase name; aliases = USPS code + common short forms.
# ---------------------------------------------------------------------------

STATES: dict[str, list[str]] = {
    "Alabama": ["AL", "Ala"],
    "Alaska": ["AK", "Alas"],
    "Arizona": ["AZ", "Ariz"],
    "Arkansas": ["AR", "Ark"],
    "California": ["CA", "Calif", "Cal"],
    "Colorado": ["CO", "Colo"],
    "Connecticut": ["CT", "Conn"],
    "Delaware": ["DE", "Del"],
    "District of Columbia": ["DC", "Washington DC", "Washington D.C."],
    "Florida": ["FL", "Fla"],
    "Georgia": ["GA", "Ga"],
    "Hawaii": ["HI"],
    "Idaho": ["ID"],
    "Illinois": ["IL", "Ill"],
    "Indiana": ["IN", "Ind"],
    "Iowa": ["IA"],
    "Kansas": ["KS", "Kan"],
    "Kentucky": ["KY", "Ken"],
    "Louisiana": ["LA", "La"],
    "Maine": ["ME"],
    "Maryland": ["MD", "Md"],
    "Massachusetts": ["MA", "Mass"],
    "Michigan": ["MI", "Mich"],
    "Minnesota": ["MN", "Minn"],
    "Mississippi": ["MS", "Miss"],
    "Missouri": ["MO", "Mo"],
    "Montana": ["MT", "Mont"],
    "Nebraska": ["NE", "Neb"],
    "Nevada": ["NV", "Nev"],
    "New Hampshire": ["NH"],
    "New Jersey": ["NJ"],
    "New Mexico": ["NM"],
    "New York": ["NY", "NY State", "New York State"],
    "North Carolina": ["NC"],
    "North Dakota": ["ND"],
    "Ohio": ["OH"],
    "Oklahoma": ["OK", "Okla"],
    "Oregon": ["OR", "Ore"],
    "Pennsylvania": ["PA", "Penn", "Penna"],
    "Rhode Island": ["RI"],
    "South Carolina": ["SC"],
    "South Dakota": ["SD"],
    "Tennessee": ["TN", "Tenn"],
    "Texas": ["TX", "Tex"],
    "Utah": ["UT"],
    "Vermont": ["VT"],
    "Virginia": ["VA", "Va"],
    "Washington": ["WA", "Wash", "Washington State"],
    "West Virginia": ["WV", "W Va"],
    "Wisconsin": ["WI", "Wis", "Wisc"],
    "Wyoming": ["WY", "Wyo"],
}

# ---------------------------------------------------------------------------
# ~30 countries + ISO codes. `United States` is the only one this restore holds (301/301), but a
# country the corpus lacks costs one row and turns "in Germany" into an honest zero rather than a
# silently-dropped filter.
# ---------------------------------------------------------------------------

COUNTRIES: dict[str, list[str]] = {
    "United States": ["US", "USA", "U.S.", "U.S.A.", "United States of America", "America"],
    "Canada": ["CA-country", "CAN"],
    "Mexico": ["MX", "MEX"],
    "United Kingdom": ["UK", "GB", "GBR", "Great Britain", "Britain", "England"],
    "Ireland": ["IE", "IRL"],
    "France": ["FR", "FRA"],
    "Germany": ["DE", "DEU", "GER", "Deutschland"],
    "Netherlands": ["NL", "NLD", "Holland", "The Netherlands"],
    "Belgium": ["BE", "BEL"],
    "Luxembourg": ["LU", "LUX"],
    "Switzerland": ["CH", "CHE"],
    "Austria": ["AT", "AUT"],
    "Spain": ["ES", "ESP"],
    "Portugal": ["PT", "PRT"],
    "Italy": ["IT", "ITA"],
    "Denmark": ["DK", "DNK"],
    "Sweden": ["SE", "SWE"],
    "Norway": ["NO", "NOR"],
    "Finland": ["FI", "FIN"],
    "Poland": ["PL", "POL"],
    "Czech Republic": ["CZ", "CZE", "Czechia"],
    "India": ["IN-country", "IND"],
    "China": ["CN", "CHN"],
    "Japan": ["JP", "JPN"],
    "South Korea": ["KR", "KOR", "Korea"],
    "Singapore": ["SG", "SGP"],
    "Australia": ["AU", "AUS"],
    "New Zealand": ["NZ", "NZL"],
    "Brazil": ["BR", "BRA"],
    "Israel": ["IL-country", "ISR"],
    "United Arab Emirates": ["AE", "ARE", "UAE"],
}

# `CA`, `IN` and `IL` are a US state code AND an ISO country code. The state wins: a salesperson
# on a US manufacturing corpus who types "CA" means California, and this corpus is 301/301 United
# States. The country entries above are suffixed so the seed cannot silently overwrite the state
# alias with the country one depending on dict order — a collision that would be invisible until
# "companies in CA" started returning all 301 rows.
_COUNTRY_ONLY_SUFFIX = "-country"

# ---------------------------------------------------------------------------
# ~100 US cities. Aliases carry the punctuation and nickname variants a user types.
# ---------------------------------------------------------------------------

CITIES: dict[str, list[str]] = {
    "New York": ["NYC", "New York City", "Manhattan"],
    "Los Angeles": ["LA-city", "L.A."],
    "Chicago": ["Chi-town"],
    "Houston": [],
    "Phoenix": [],
    "Philadelphia": ["Philly"],
    "San Antonio": [],
    "San Diego": [],
    "Dallas": [],
    "San Jose": [],
    "Austin": [],
    "Jacksonville": [],
    "Fort Worth": ["Ft Worth", "Ft. Worth"],
    "Columbus": [],
    "Charlotte": [],
    "San Francisco": ["SF", "San Fran", "Frisco"],
    "Indianapolis": ["Indy"],
    "Seattle": [],
    "Denver": [],
    "Boston": [],
    "El Paso": [],
    "Nashville": ["Nashville-Davidson"],
    "Detroit": [],
    "Oklahoma City": ["OKC"],
    "Portland": [],
    "Las Vegas": ["Vegas"],
    "Memphis": [],
    "Louisville": [],
    "Baltimore": [],
    "Milwaukee": [],
    "Albuquerque": [],
    "Tucson": [],
    "Fresno": [],
    "Sacramento": [],
    "Mesa": [],
    "Kansas City": ["KC"],
    "Atlanta": ["ATL"],
    "Omaha": [],
    "Colorado Springs": [],
    "Raleigh": [],
    "Miami": [],
    "Long Beach": [],
    "Virginia Beach": [],
    "Oakland": [],
    "Minneapolis": [],
    "Tulsa": [],
    "Tampa": [],
    "Arlington": [],
    "New Orleans": ["NOLA"],
    "Wichita": [],
    "Cleveland": [],
    "Bakersfield": [],
    "Aurora": [],
    "Anaheim": [],
    "Honolulu": [],
    "Santa Ana": [],
    "Riverside": [],
    "Corpus Christi": [],
    "Lexington": [],
    "Henderson": [],
    "Stockton": [],
    "St. Paul": ["St Paul", "Saint Paul"],
    "Cincinnati": ["Cincy"],
    "St. Louis": ["St Louis", "Saint Louis", "STL"],
    "Pittsburgh": ["Pitt"],
    "Greensboro": [],
    "Lincoln": [],
    "Anchorage": [],
    "Plano": [],
    "Orlando": [],
    "Irvine": [],
    "Newark": [],
    "Toledo": [],
    "Durham": [],
    "Chula Vista": [],
    "Fort Wayne": ["Ft Wayne", "Ft. Wayne"],
    "Jersey City": [],
    "St. Petersburg": ["St Petersburg", "Saint Petersburg"],
    "Laredo": [],
    "Madison": [],
    "Chandler": [],
    "Buffalo": [],
    "Lubbock": [],
    "Scottsdale": [],
    "Reno": [],
    "Glendale": [],
    "Gilbert": [],
    "Winston-Salem": ["Winston Salem"],
    "North Las Vegas": [],
    "Norfolk": [],
    "Chesapeake": [],
    "Garland": [],
    "Irving": [],
    "Hialeah": [],
    "Fremont": [],
    "Boise": ["Boise City"],
    "Richmond": [],
    "Baton Rouge": [],
    "Spokane": [],
    "Des Moines": [],
    "Tacoma": [],
    "San Bernardino": [],
    "Modesto": [],
    "Fontana": [],
    "Santa Clarita": [],
    "Birmingham": [],
    "Oxnard": [],
    "Rochester": [],
    "Moreno Valley": [],
    "Fayetteville": [],
    "Akron": [],
    "Huntington Beach": [],
    "Little Rock": [],
    "Augusta": [],
    "Amarillo": [],
    "Grand Rapids": [],
    "Hartford": [],
    "Salt Lake City": ["SLC"],
    "Huntsville": [],
    "Worcester": [],
    "Knoxville": [],
    "Providence": [],
    "Chattanooga": [],
    "Dayton": [],
    "Charleston": [],
    "Syracuse": [],
    "Peoria": [],
    "Erie": [],
    "Green Bay": [],
    "Allentown": [],
}


def _variants(name: str) -> set[str]:
    """The spellings of one name that must all resolve: as written, and without its periods.

    `St. Louis` is what `hq_city` holds; `st louis` is what a user types. One rule covers every
    `St.`/`Ft.`/`U.S.` in the seed, so the lists above do not have to remember to.
    """
    out = {name}
    if "." in name:
        out.add(name.replace(".", ""))
    return out


def build_rows(corpus: dict[str, list[str]]) -> tuple[list[tuple[str, str, str]], dict[str, list[str]]]:
    """The seed + the corpus sweep, as `location_alias` rows.

    Returns the rows and, separately, the corpus values the static seed did **not** know — that
    list is the honest report of how good the hypothesis was, and it is printed rather than
    hidden.
    """
    by_alias: dict[str, tuple[str, str]] = {}

    def add(alias: str, canonical: str, kind: str) -> None:
        key = location_key(alias)
        if not key:
            return
        # First writer wins: the seed's states are added before the countries, so the `CA`/`IL`
        # collisions resolve to the state deterministically rather than by dict iteration luck.
        by_alias.setdefault(key, (location_key(canonical), kind))

    for group, kind in ((STATES, "state"), (CITIES, "city"), (COUNTRIES, "country")):
        for canonical, aliases in group.items():
            for variant in _variants(canonical):
                add(variant, canonical, kind)
            for alias in aliases:
                # A `-city`/`-country` suffix marks an alias that would collide with a
                # higher-priority kind. It is a seed-authoring marker, never a lookup key.
                if alias.endswith(_COUNTRY_ONLY_SUFFIX) or alias.endswith("-city"):
                    continue
                for variant in _variants(alias):
                    add(variant, canonical, kind)

    # ---- the corpus sweep: every value this restore actually holds must resolve to itself.
    unseeded: dict[str, list[str]] = {}
    for kind, values in corpus.items():
        for value in values:
            key = location_key(value)
            if key not in by_alias:
                unseeded.setdefault(kind, []).append(value)
            # Force the identity mapping even if the seed had this alias pointing elsewhere: the
            # corpus is the authority on what exists, and §3.1's whole lesson is that the
            # canonical must match the column.
            by_alias[key] = (key, kind)

    rows = sorted((alias, canonical, kind) for alias, (canonical, kind) in by_alias.items())
    return rows, unseeded


def main() -> int:
    parser = argparse.ArgumentParser(description="Populate location_alias (CHANGES-v2 §3.1)")
    parser.add_argument("--dry-run", action="store_true", help="compute and report; write nothing")
    args = parser.parse_args()

    with repository.connect() as conn:
        corpus = repository.distinct_hq_locations(conn)
        rows, unseeded = build_rows(corpus)

        print("corpus (READ-ONLY from lead_company)")
        for kind in ("state", "city", "country"):
            values = corpus.get(kind, [])
            print(f"  distinct hq_{kind:<8}: {len(values):>3}  e.g. {', '.join(values[:4])}")

        print("\nseed")
        print(f"  states     : {len(STATES)}")
        print(f"  countries  : {len(COUNTRIES)}")
        print(f"  cities     : {len(CITIES)}")
        print(f"  alias rows : {len(rows)} (canonical: {len({c for _, c, _ in rows})})")

        print("\n§0/§3.1 verification — canonical must match the column, not the abbreviation")
        sample = [r for r in rows if r[1] == "california"]
        print(f"  california: {[a for a, _, _ in sample]} -> 'california'")
        hits = [v for v in corpus.get("state", []) if v.lower() == "california"]
        print(
            f"  lead_company.hq_state holds {hits!r} -> "
            f"{'MATCH — full name is canonical, as §0 measured' if hits else 'NO CALIFORNIA ROWS'}"
        )

        if unseeded:
            print("\ncorpus values the static seed did not know (added as their own canonical):")
            for kind, values in sorted(unseeded.items()):
                print(f"  {kind}: {len(values)} — {', '.join(values[:8])}{' …' if len(values) > 8 else ''}")
        else:
            print("\nevery corpus location was already covered by the static seed.")

        # A corpus value that cannot resolve to itself is a silent zero-result query.
        for kind, values in corpus.items():
            for value in values:
                assert any(a == location_key(value) for a, _, _ in rows), (
                    f"corpus {kind} {value!r} does not resolve — a query for it would silently "
                    f"return nothing"
                )

        if args.dry_run:
            print("\n--dry-run: nothing written.")
            return 0

        written = repository.replace_location_aliases(conn, rows)
        stored = repository.count_location_aliases(conn)
        print(f"\nwrote {written} rows to location_alias")
        print(
            f"  stored: {stored['aliases']} aliases -> {stored['canonicals']} canonicals "
            f"(states {stored['states']}, cities {stored['cities']}, countries {stored['countries']})"
        )
        assert stored["aliases"] == written, "location_alias row count does not match the seed"

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
