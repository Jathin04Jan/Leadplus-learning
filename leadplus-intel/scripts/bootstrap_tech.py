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
SEED_TECH: dict[str, list[str]] = {
    # ERP / business systems
    "SAP": ["SAP ERP", "SAP ECC", "ECC", "SAP R/3"],
    "SAP S/4HANA": ["SAP S4", "S/4HANA", "S4 HANA", "S/4 HANA", "S4/HANA", "SAP S/4", "S4HANA",
                    "SAP S4HANA", "SAP S/4 HANA"],
    "Oracle ERP": ["Oracle E-Business Suite", "Oracle EBS", "Oracle Fusion"],
    "NetSuite": ["Oracle NetSuite"],
    "Microsoft Dynamics 365": ["Dynamics 365", "D365"],
    "Microsoft Dynamics CRM": ["Dynamics CRM"],
    "Epicor Kinetic": ["Epicor"],
    "Infor CloudSuite": ["Infor"],
    "Workday": [],
    "Salesforce": ["SFDC", "Salesforce CRM"],
    "HubSpot": [],
    "Zoho CRM": ["Zoho"],
    "ServiceNow": [],
    # Cloud
    "AWS": ["Amazon Web Services", "Amazon AWS"],
    "Microsoft Azure": ["Azure"],
    "Google Cloud Platform": ["GCP", "Google Cloud"],
    # Data
    "Snowflake": [],
    "Databricks": [],
    "BigQuery": ["Google BigQuery"],
    "Redshift": ["Amazon Redshift", "AWS Redshift"],
    "Teradata": [],
    "PostgreSQL": ["Postgres"],
    "Kafka": ["Apache Kafka"],
    "Spark": ["Apache Spark", "PySpark"],
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


async def seed(conn, *, reseed: bool) -> None:
    apollo = repository.distinct_apollo_technologies(conn)
    print(f"Apollo distinct lead_company.technologies[] : {len(apollo)} terms")
    print(f"hand-seeded SI-relevant platforms           : {len(SEED_TECH)} terms")

    # Apollo's spelling wins where the two overlap: it is the curated source.
    terms: dict[str, list[str]] = {term: list(aliases) for term, aliases in SEED_TECH.items()}
    by_key = {canonicalize.tech_key(t): t for t in terms}
    added = 0
    for term in apollo:
        key = canonicalize.tech_key(term)
        if key not in by_key:
            terms[term] = []
            by_key[key] = term
            added += 1
    print(f"union                                       : {len(terms)} terms ({added} new from Apollo)")

    existing = {row["term"] for row in repository.fetch_tech_canonical(conn)}
    todo = list(terms) if reseed else [t for t in terms if t not in existing]
    if not todo:
        print("tech_canonical already seeded; nothing to embed.")
        return

    # Embed the term itself: stage 3's NN step compares a raw term against these vectors.
    vectors = await embed.embed_texts(todo)
    repository.upsert_tech_canonical(
        conn, [(term, vector, terms[term]) for term, vector in zip(todo, vectors)]
    )
    print(f"seeded + embedded {len(todo)} terms into tech_canonical")


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
        await seed(conn, reseed=args.reseed)
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
