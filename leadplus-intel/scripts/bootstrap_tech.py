"""Technology + industry canonicalisation — §5.2 stage 3, §5.5. Phase 5.

    .venv/bin/python scripts/bootstrap_tech.py [--reseed] [--skip-industry]

Two jobs:

1. **Seed `tech_canonical`** from the distinct values of `lead_company.technologies[]` (Apollo's
   list is curated, so it is a trustworthy vocabulary) plus a hand-seeded list of SI-relevant
   platforms, then embed every term.

2. **Canonicalise** every `technologies[]` already stored on `job_signal` / `company_signal`
   through the §5.2 stage-3 ladder, and map `company_signal.industry_raw` onto the `lead_query`
   COMPANY_INDUSTRY vocabulary (§5.5).

Why this runs *after* ingest: §13's build order puts the full ingest at phase 4 and
canonicalisation at phase 5, so the first ingest stores raw extractions. This script converts
them in place. Once `tech_canonical` exists, later ingests canonicalise inline (§5.2 stage 3) and
this pass becomes a no-op for new rows.

Rule 5 is the whole point: `SAP S/4HANA`, `S/4 HANA`, `SAP S4` and `S4/HANA` must be ONE term.
Four spellings mean substring matching, and substring matching is the defect this project exists
to kill. Nothing here auto-guesses — an unresolved term goes to `tech_review_queue` for a human.
"""

from __future__ import annotations

import argparse
import asyncio

import _bootstrap  # noqa: F401

from intel import canonicalize, config, embed, llm, repository

# Hand-seeded SI-relevant platforms (§5.2 stage 3: "plus a hand-seeded list of SI-relevant
# platforms"). Apollo's list covers what companies *own*; this covers what job ads *name*, which
# is the vocabulary the job normalizer emits.
#
# `aliases` are the spellings we already know are the same product. The alias for SAP S/4HANA is
# the concrete case rule 5 names: without it, "SAP S4" and "S/4HANA" become separate terms.
#
# ---------------------------------------------------------------------------------------------
# THE INVARIANT — read before adding anything here.
#
#     No phrase may be both a canonical term and an alias of a DIFFERENT term.
#
# It is enforced twice and never by hand: `_alias_owners` fails loudly if THIS dict breaks it,
# and `verify_invariant` fails the run if the DATABASE breaks it. `reconcile` is what makes the
# database obey, and this dict is the only thing that decides HOW:
#
#   * declaring a phrase as an alias  -> MERGE: any standalone term for it is DELETED and its
#     rows migrate to the owner. Say this when the standalone is the SAME PRODUCT.
#   * NOT declaring it                -> SPLIT: the standalone term stands on its own and the
#     stale alias is pruned off whichever term used to claim it. Say this when it is a
#     DIFFERENT PRODUCT.
#
# This dict is therefore the classification. There is no second place to look.
#
# Why the invariant exists: `tech_canonical` is built from this dict UNION the ~4,529 distinct
# values of Apollo's `lead_company.technologies[]`, and Apollo carries `Amazon AWS` and `SAP ECC`
# as values of its own. A phrase that is both a term and an alias breaks the §5.2 stage-3 ladder
# silently, because the ladder tries exact BEFORE alias and so never reaches the alias step. Both
# failure modes were measured on this corpus:
#
#   fragmentation: 65 companies use AWS -> 40 stored `Amazon AWS`, 25 `AWS`, ZERO overlap.
#                  A search for AWS returned 25 of 65. The other 40 were invisible.
#   conflation:    query `SAP ECC` resolved to canonical `SAP`, coverage 1.00, and not one
#                  result carried SAP ECC. The 20 companies that genuinely run ECC matched
#                  nothing.
# ---------------------------------------------------------------------------------------------
SEED_TECH: dict[str, list[str]] = {
    # ERP / business systems
    #
    # SAP is the VENDOR/GENERIC name. ECC, ERP and R/3 are the legacy on-prem products, and
    # S/4HANA is the modern successor. They are NOT four spellings of one thing, so none of them
    # is an alias of `SAP` — that is a SPLIT, and it is the whole product question (§0's wedge is
    # "who is still on ECC and therefore has a migration ahead of them?"). Folding them together
    # destroys the only thing this tool is for. Measured on this corpus: SAP 10 companies,
    # SAP ECC 20, SAP ERP 6 — three different sets, and 2 companies carry ECC *and* ERP, which is
    # Apollo telling us they are different attributes.
    #
    # `ECC` (the bare acronym) belongs to SAP ECC, not to SAP.
    "SAP": [],
    # `ECC` (the bare acronym), plus Apollo's two verbose spellings of the SAME product. Measured:
    # 12 companies carry `SAP ECC`, 9 carry `SAP ERP Central Component (ECC)`, 1 carries the `6.0`
    # variant — and "ERP Central Component" is literally what ECC abbreviates, so they are one
    # product under three spellings, the exact rule-5 case. Without these aliases a query for
    # `SAP ECC` returned 10-12 of the ~17 real ECC shops and the rest were invisible — defect #2
    # (`Amazon AWS` hiding 40 of 65 AWS users) in a new costume. They are a SPLIT away from `SAP`
    # and `SAP S/4HANA` (different products) but a MERGE among themselves.
    "SAP ECC": ["ECC", "SAP ERP Central Component (ECC)", "SAP ERP Central Component (ECC) 6.0"],
    "SAP ERP": [],
    "SAP R/3": [],
    "SAP S/4HANA": ["SAP S4", "S/4HANA", "S4 HANA", "S/4 HANA", "S4/HANA", "SAP S/4", "S4HANA",
                    "SAP S4HANA", "SAP S/4 HANA"],
    # Oracle Fusion is the cloud successor to E-Business Suite, not a spelling of it — SPLIT, for
    # the same reason as SAP ECC vs SAP S/4HANA.
    "Oracle ERP": ["Oracle E-Business Suite", "Oracle EBS"],
    "Oracle Fusion": [],
    "NetSuite": ["Oracle NetSuite"],
    "Microsoft Dynamics 365": ["Dynamics 365", "D365"],
    "Microsoft Dynamics CRM": ["Dynamics CRM"],
    # Epicor is the vendor; Kinetic is the product. Same vendor-vs-product split as SAP.
    "Epicor Kinetic": [],
    "Epicor": [],
    "Infor CloudSuite": ["Infor"],
    "Workday": [],
    "Salesforce": ["SFDC", "Salesforce CRM"],
    "HubSpot": [],
    "Zoho CRM": ["Zoho"],
    "ServiceNow": [],
    # Cloud
    "AWS": ["Amazon Web Services", "Amazon AWS", "Amazon Web Services (AWS)"],
    "Microsoft Azure": ["Azure"],
    "Google Cloud Platform": ["GCP", "Google Cloud"],
    # Apollo-vs-Apollo variants. These are NOT reachable by the ladder: Apollo's ~4,529 values
    # are seeded as terms directly, so a variant is never resolved against its twin — it just
    # becomes a second term, and the companies split silently between them.
    #
    # They are hand-curated, and they have to be. Cosine cannot make this call: the four pairs
    # below sit at 0.865-0.972, and these sit in the SAME band and are DIFFERENT products —
    #     Google Analytics / Yahoo Analytics ...... 0.904   (different vendors)
    #     Google Maps (Non Paid) / (Paid Users) ... 0.874   (a deliberate licensing split)
    #     DNSimple / DNS Made Easy ................ 0.861   (different vendors)
    #     Infoblox DHCP / Infoblox DNS ............ 0.857   (different products)
    #     Siemens SIMATIC S7 / SIMATIC SCADA ...... 0.852   (different products)
    # Of the 12 same-band pairs where both sides carry companies, 8 are distinct products, so an
    # auto-merge at 0.85 would be wrong 67% of the time — and wrong in the conflation direction,
    # which is invisible. The ladder resolves an extracted term against a KNOWN vocabulary; it
    # cannot dedupe a vocabulary against itself, because vendors name things alike on purpose.
    "Microsoft Office 365": ["Office365"],
    "Amazon Elastic Load Balancing": ["Amazon Elastic Load Balancer"],
    "Azure Synapse Analytics": ["Azure Synapse"],
    "Microsoft Azure Monitor": ["Azure Monitor"],
    # Data
    "Snowflake": [],
    "Databricks": [],
    "BigQuery": ["Google BigQuery"],
    "Redshift": ["Amazon Redshift", "AWS Redshift"],
    "Teradata": [],
    "PostgreSQL": ["Postgres"],
    "Kafka": ["Apache Kafka"],
    "Spark": ["Apache Spark"],
    "PySpark": [],
    "Airflow": ["Apache Airflow"],
    "dbt": ["dbt Labs", "data build tool"],
    # Platform / infra
    "Kubernetes": ["K8s"],
    "Docker": [],
    "Terraform": ["HashiCorp Terraform"],
    "GitHub Actions": [],
    # Analytics / collaboration
    "Power BI": ["PowerBI", "Microsoft Power BI"],
    "Tableau": [],
    "Looker": [],
    "Jira": ["Atlassian Jira"],
    "Confluence": [],
    # Languages
    "Python": [],
    "Java": [],
}


class SeedContradiction(RuntimeError):
    """The seed declares a phrase as both a term and a different term's alias. Never caught.

    Loud on purpose. A contradictory seed does not produce a broken row you can spot — it
    produces a vocabulary that silently resolves half a product's companies to the wrong term,
    which is exactly how `Amazon AWS` hid 40 of 65 AWS users in plain sight for an entire corpus.
    """


def _alias_owners(seed: dict[str, list[str]]) -> dict[str, str]:
    """`alias key -> owning term`, with THE INVARIANT enforced on the seed itself.

    Three ways a seed can contradict itself, all fatal:
      1. two terms that normalise to the same key (`Power BI` and `PowerBI` as *terms*),
      2. a term that is also another term's alias (`SAP ECC` as a term and as SAP's alias),
      3. one alias claimed by two different terms (`PySpark` under both Spark and Databricks) —
         resolution would then depend on `find_tech_alias`'s arbitrary `LIMIT 1`.
    """
    term_by_key: dict[str, str] = {}
    for term in seed:
        key = canonicalize.tech_key(term)
        if not key:
            raise SeedContradiction(f"SEED_TECH term {term!r} normalises to an empty key")
        if key in term_by_key:
            raise SeedContradiction(
                f"SEED_TECH declares two terms with the same key {key!r}: "
                f"{term_by_key[key]!r} and {term!r}. One of them must go."
            )
        term_by_key[key] = term

    owners: dict[str, str] = {}
    for term, aliases in seed.items():
        for alias in aliases:
            key = canonicalize.tech_key(alias)
            if not key:
                raise SeedContradiction(
                    f"SEED_TECH alias {alias!r} of {term!r} normalises to an empty key"
                )
            if key == canonicalize.tech_key(term):
                continue  # a term restating itself is harmless
            clash = term_by_key.get(key)
            if clash is not None:
                raise SeedContradiction(
                    f"SEED_TECH breaks THE INVARIANT: {clash!r} is a canonical term AND is "
                    f"declared as an alias of {term!r}.\n"
                    f"  No phrase may be both. Decide which it is:\n"
                    f"    MERGE — {clash!r} is the same product as {term!r}: delete the "
                    f"{clash!r} entry, keep the alias.\n"
                    f"    SPLIT — they are different products: delete the alias from "
                    f"{term!r}, keep the {clash!r} entry."
                )
            owner = owners.get(key)
            if owner is not None and owner != term:
                raise SeedContradiction(
                    f"SEED_TECH declares the alias {alias!r} under two terms: {owner!r} and "
                    f"{term!r}. An alias has exactly one owner."
                )
            owners[key] = term
    return owners


async def seed(conn, *, reseed: bool) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Seed `tech_canonical` = SEED_TECH ∪ (Apollo's distinct values MINUS declared aliases).

    That subtraction is THE INVARIANT's other half, and it is the entire fix. Apollo's curated
    list carries `Amazon AWS` and `SAP ECC` as values in their own right. Add them as standalone
    terms and the stage-3 ladder — which tries **exact before alias** — matches `Amazon AWS`
    against its own term and never reaches the alias step that would have sent it to `AWS`. The
    alias becomes dead code, and the corpus splits in two without a single error.
    """
    alias_owner = _alias_owners(SEED_TECH)  # loud, and before anything touches the database

    apollo = repository.distinct_apollo_technologies(conn)
    print(f"Apollo distinct lead_company.technologies[] : {len(apollo)} terms")
    print(f"hand-seeded SI-relevant platforms           : {len(SEED_TECH)} terms")
    print(f"aliases declared by the seed                : {len(alias_owner)}")

    terms: dict[str, list[str]] = {term: list(aliases) for term, aliases in SEED_TECH.items()}
    by_key = {canonicalize.tech_key(t): t for t in terms}
    added = 0
    shadowed: list[tuple[str, str]] = []
    for term in apollo:
        key = canonicalize.tech_key(term)
        if key in alias_owner:
            # THE INVARIANT: this phrase is already spoken for as an alias, so it must NOT become
            # a term of its own. Skipping it here is what makes the alias reachable at all.
            shadowed.append((term, alias_owner[key]))
            continue
        if key not in by_key:
            terms[term] = []
            by_key[key] = term
            added += 1
    print(f"union                                       : {len(terms)} terms ({added} new from Apollo)")
    print(f"Apollo values skipped as declared aliases   : {len(shadowed)}")
    for term, owner in shadowed:
        print(f"    {term!r} is an alias of {owner!r} — no standalone term")

    existing = {row["term"] for row in repository.fetch_tech_canonical(conn)}
    todo = list(terms) if reseed else [t for t in terms if t not in existing]

    # Embed the term itself: stage 3's NN step compares a raw term against these vectors.
    # Only new terms need a vector; `upsert_tech_canonical` COALESCEs, so an existing term
    # re-upserted with NULL keeps the embedding it already has and costs nothing.
    vectors = await embed.embed_texts(todo) if todo else []
    embedded = dict(zip(todo, vectors))

    # Write the alias list for EVERY seeded term, not just the ones being embedded.
    # This used to ride along with the embed call, so a seed alias added to a term that already
    # existed was silently dropped on the floor — the alias list only ever landed on a term's
    # first sighting. That is how `ECC` would have ended up owned by nobody the moment it moved
    # from `SAP` to `SAP ECC`: SAP ECC already existed as an Apollo term, so it was not in `todo`,
    # so its new alias was never written. Seeding is a declaration, not a side effect of embedding.
    rows = [(term, embedded.get(term), terms[term]) for term in dict.fromkeys([*todo, *SEED_TECH])]
    repository.upsert_tech_canonical(conn, rows)
    print(f"seeded {len(rows)} term(s) ({len(todo)} newly embedded, {len(SEED_TECH)} hand-seeded "
          f"alias lists re-asserted)")
    return alias_owner, terms


def reconcile(conn, alias_owner: dict[str, str]) -> None:
    """Make the DATABASE obey THE INVARIANT. Idempotent; a no-op once it holds.

    `seed` alone cannot do this. `upsert_tech_canonical` merges alias lists *additively* (by
    design — the ladder's `add_tech_alias` learns aliases at ingest and they must survive a
    re-seed), so deleting `"SAP ECC"` from SEED_TECH's `SAP` entry does not delete it from the
    row. Nothing here is hand-listed: the seed says which side of each collision yields, and
    these two passes carry it out.
    """
    rows = repository.fetch_tech_canonical(conn)

    # --- MERGE: a term the seed declares to be another term's alias must not exist -----------
    doomed = [
        row["term"] for row in rows
        if alias_owner.get(canonicalize.tech_key(row["term"]), row["term"]) != row["term"]
    ]
    if doomed:
        for term in doomed:
            print(f"    MERGE: dropping term {term!r} -> alias of "
                  f"{alias_owner[canonicalize.tech_key(term)]!r}")
        repository.delete_tech_terms(conn, doomed)
    print(f"reconcile: {len(doomed)} merged term(s) deleted from tech_canonical")

    # --- SPLIT: an alias that collides with a SURVIVING term is pruned off the claimant -------
    # After the pass above, no surviving term is a declared alias — so any alias still colliding
    # with a term is one the seed has stopped claiming (a SPLIT), or one the seed has repointed
    # somewhere else (`ECC`: SAP -> SAP ECC). Either way the term wins and the stale alias goes.
    rows = repository.fetch_tech_canonical(conn)
    survivors = {canonicalize.tech_key(row["term"]): row["term"] for row in rows}
    updates: list[tuple[str, list[str]]] = []
    for row in rows:
        term = row["term"]
        aliases = list(row["aliases"] or [])
        keep: list[str] = []
        for alias in aliases:
            key = canonicalize.tech_key(alias)
            clash = survivors.get(key)
            if clash is not None and clash != term:
                print(f"    SPLIT: pruning alias {alias!r} off {term!r} — {clash!r} is its own term")
                continue
            owner = alias_owner.get(key)
            if owner is not None and owner != term:
                print(f"    REPOINT: pruning alias {alias!r} off {term!r} — seed gives it to {owner!r}")
                continue
            keep.append(alias)
        if keep != aliases:
            updates.append((term, keep))
    if updates:
        repository.set_tech_aliases(conn, updates)
    print(f"reconcile: {len(updates)} term(s) had stale aliases pruned")


def verify_invariant(conn) -> None:
    """THE INVARIANT, asserted against the database. The bug cannot come back past this line."""
    collisions = repository.tech_alias_collisions(conn)
    if collisions:
        listing = "\n".join(
            f"    {row['term']!r} is a canonical term AND an alias of {row['owner']!r}"
            for row in collisions
        )
        raise SeedContradiction(
            f"THE INVARIANT IS BROKEN in tech_canonical — {len(collisions)} collision(s):\n"
            f"{listing}\n"
            f"  A phrase that is both a term and another term's alias is unreachable by alias: "
            f"the ladder tries exact first. Fix SEED_TECH, do not patch the table."
        )
    print("INVARIANT: 0 collisions — no term is an alias of another term. PASS")


async def canonicalise_signals(conn) -> None:
    """Run every stored `technologies[]` through the stage-3 ladder."""
    tech = canonicalize.TechCanonicalizer(conn)

    for label, fetch, update, id_key in (
        ("job_signal", repository.fetch_job_signal_technologies,
         repository.update_job_signal_technologies, "job_id"),
        ("company_signal", repository.fetch_company_signal_technologies,
         repository.update_company_signal_technologies, "company_id"),
    ):
        rows = fetch(conn)
        updates = []
        changed = 0
        for row in rows:
            raw = list(row["technologies"] or [])
            canonical = await tech.canonical_list(raw)
            if canonical != raw:
                changed += 1
            updates.append((row[id_key], canonical))
        update(conn, updates)
        print(f"{label}: {len(rows)} rows canonicalised ({changed} changed)")

    methods: dict[str, int] = {}
    for resolution in tech._cache.values():  # noqa: SLF001 — reporting the run's own work
        methods[resolution.method] = methods.get(resolution.method, 0) + 1
    print(f"\nresolution methods across distinct raw terms: {methods}")


async def canonicalise_industries(conn) -> None:
    """§5.5 — map free-text industry onto the lead_query COMPANY_INDUSTRY vocabulary."""
    canonicaliser = await canonicalize.IndustryCanonicalizer.build(conn)
    print(f"\n§5.5 industry vocabulary (lead_query COMPANY_INDUSTRY): {len(canonicaliser.vocabulary)} values")

    rows = repository.fetch_company_industries(conn)
    raws = [row["industry_raw"] for row in rows]
    non_empty = [r for r in raws if r and r.strip()]
    vectors: dict[str, list[float]] = {}
    if non_empty:
        unique = list(dict.fromkeys(non_empty))
        # §8.5 compares emb(company.industry_raw) at query time, so industry_embedding is the
        # embedding of the RAW value, not of the canonical bucket.
        embedded = await embed.embed_texts(unique)
        vectors = dict(zip(unique, embedded))

    updates = []
    methods: dict[str, int] = {}
    unresolved: list[tuple[str, float | None]] = []
    for row in rows:
        raw = row["industry_raw"]
        resolution = canonicaliser.resolve(raw, vectors.get(raw) if raw else None)
        methods[resolution.method] = methods.get(resolution.method, 0) + 1
        if resolution.method == "unresolved":
            unresolved.append((raw, resolution.similarity))
        updates.append((row["company_id"], resolution.canonical, vectors.get(raw) if raw else None))

    repository.update_company_industry(conn, updates)
    print(f"company_signal: {len(updates)} industries mapped {methods}")
    if unresolved:
        print(f"  unresolved (left NULL rather than forced into a wrong bucket):")
        for raw, similarity in unresolved[:10]:
            print(f"    {raw!r} (best cosine {similarity:.3f})" if similarity else f"    {raw!r}")


def report(conn) -> None:
    print(f"\n{'=' * 100}\nVOCABULARY AFTER CANONICALISATION")

    for table in ("job_signal", "company_signal"):
        histogram = repository.technology_histogram(conn, table)
        print(f"\n{table}.technologies ({len(histogram)} distinct canonical terms):")
        for row in histogram:
            print(f"  {row['term']:<26} {row['n']:>4}")

    print(f"\n{'=' * 100}\nRULE 5 CHECK — no SAP S4 / S/4HANA-style splits")
    terms = {
        row["term"] for table in ("job_signal", "company_signal")
        for row in repository.technology_histogram(conn, table)
    }
    families: dict[str, list[str]] = {}
    for term in terms:
        key = canonicalize.tech_key(term)
        # Group spelling variants of the same product: s4hana / saps4hana / saps4 all collapse.
        family = key.replace("sap", "").replace("hana", "") if "s4" in key or "hana" in key else key
        families.setdefault(family or key, []).append(term)

    splits = {f: t for f, t in families.items() if len(t) > 1}
    sap_like = sorted(t for t in terms if "sap" in canonicalize.tech_key(t) or "hana" in canonicalize.tech_key(t)
                      or "s4" in canonicalize.tech_key(t))
    print(f"  SAP/S4/HANA-family terms in use: {sap_like}")
    if splits:
        print(f"  POSSIBLE SPLITS (same product, >1 spelling): {splits}")
    else:
        print("  PASS: no term family has more than one spelling in the stored vocabulary.")
    print(
        "  Note: 'SAP' and 'SAP S/4HANA' are DIFFERENT products (ECC vs S/4HANA), not a split.\n"
        "        'Sapient Cloud Suite' is a different product again and must never fold into SAP."
    )

    queue = repository.fetch_tech_review_queue(conn)
    print(f"\n{'=' * 100}\nTECH REVIEW QUEUE — {len(queue)} unresolved term(s)")
    if not queue:
        print("  empty: every extracted term resolved via exact match, alias, or embedding NN >0.85.")
    for row in queue:
        similarity = f"{row['similarity']:.3f}" if row["similarity"] is not None else "n/a"
        print(
            f"  {row['raw_term']!r:<34} occurrences={row['occurrences']:<4} "
            f"nearest={row['nearest']!r} cosine={similarity}"
        )
    if queue:
        print(
            "\n  These are NOT auto-resolved — that is the design (§5.2 stage 3). A human decides,\n"
            "  then sets resolved_to, or adds the term/alias to tech_canonical and re-runs."
        )

    histogram = repository.industry_histogram(conn)
    print(f"\n{'=' * 100}\nINDUSTRY MAPPING (§5.5)")
    for row in histogram:
        mark = "" if row["industry_canonical"] else "   <- UNMAPPED"
        print(f"  {str(row['industry_raw']):<28} -> {str(row['industry_canonical']):<28} {row['n']:>4}{mark}")


async def run(args: argparse.Namespace) -> int:
    with repository.connect() as conn:
        alias_owner, _ = await seed(conn, reseed=args.reseed)
        print()
        # Order matters: reconcile BEFORE canonicalise_signals. Deleting the merged standalone
        # terms is what lets the ladder below fall through to the alias step, and that fall-
        # through IS the data migration — `Amazon AWS` on a stored row stops matching a term of
        # its own and resolves to `AWS`. Every stored technology is already a catalogued term, so
        # the whole pass is exact/alias hits: no embeddings, no LLM, no cost.
        reconcile(conn, alias_owner)
        verify_invariant(conn)
        print()
        await canonicalise_signals(conn)
        if not args.skip_industry:
            await canonicalise_industries(conn)
        report(conn)
        print("\n" + llm.USAGE.report())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed + apply canonical technologies (§5.2, §5.5)")
    parser.add_argument("--reseed", action="store_true", help="re-embed every seed term")
    parser.add_argument("--skip-industry", action="store_true", help="skip the §5.5 industry pass")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
