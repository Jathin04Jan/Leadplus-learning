"""Phase 6 — retrieval and fusion (ARCHITECTURE.md §6, §8.1).

**There is no LLM in this file, and there must never be one.** Rule 1: the LLM parses the query
into chips at the edge, and everything from here to the ranked list is deterministic. An LLM in
this path means the same chips return a different order on different runs, which is the
"search isn't consistently reliable" complaint the whole project exists to answer.

The one call to OpenAI that *does* happen here is `embed.embed_texts` on the query paraphrase.
That is not a ranker: it is rule 4 (symmetric normalization) — the query is put through the same
vocabulary as the documents before being compared to them. It is deterministic for a given input
and it is cached.

The pipeline, per §6:

    [2] hard filters — facts only, in SQL. Terms and industry are NOT here.
    [3] four lists:  job_lex ‖ job_sem ‖ company_lex ‖ company_sem   (top 200 each)
    [4] project the two job lists onto companies (best job's rank wins), then RRF all four.

What comes out is a candidate set with a fused rank score. Nothing has been dropped for failing
to match a term — that is scoring's job (§8.4), and it down-weights, it does not delete.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

import psycopg

from . import embed, repository
from .canonicalize import tech_key
from .models import Chips, IntentMode, TermSource

log = logging.getLogger(__name__)

# §8.1. RRF's constant. 60 is the value from the original Cormack et al. paper and the spec.
K_RRF = 60

# §6[3]: "top 200" for each of the four lists.
LIST_DEPTH = 200


# ---------------------------------------------------------------------------
# Rule 4 — the normalized query paraphrase
# ---------------------------------------------------------------------------


def _humanize(value: str) -> str:
    """`DATA_ENGINEERING` -> `data engineering`. The enums are the vocabulary; prose is the form."""
    return value.replace("_", " ").lower()


def _and_join(values: Sequence[str]) -> str:
    """`[A, B, C]` -> `A, B and C`. Matches how the normalizer prompts write lists."""
    items = [v for v in values if v]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def bucket_terms(chips: Chips) -> tuple[list[str], list[str], list[str]]:
    """Split terms into (uses, hiring, unassigned) by `Term.source`, resolving ANY by intent.

    A term whose source is ANY has no side of its own, so the intent mode decides which side the
    *paraphrase* should read as. This only shapes the text we embed — §8.4 still checks an ANY
    term against the union of both haystacks, so nothing is narrowed here.
    """
    uses: list[str] = []
    hiring: list[str] = []
    unassigned: list[str] = []
    for term in chips.terms:
        value = term.value.strip()
        if not value:
            continue
        if term.source == TermSource.USES:
            uses.append(value)
        elif term.source == TermSource.HIRING:
            hiring.append(value)
        elif chips.intent_mode == IntentMode.USES:
            uses.append(value)
        elif chips.intent_mode == IntentMode.HIRING:
            hiring.append(value)
        else:
            unassigned.append(value)
    return uses, hiring, unassigned


def query_paraphrase(chips: Chips) -> str:
    """Render chips as a document-shaped paraphrase — rule 4, the query half.

    `job_signal.embedding` and `company_signal.embedding` are embeddings of *paraphrases*: third
    person, declarative, signal-only, written by the normalizer prompts. Embedding the user's raw
    string instead would compare an interrogative fragment ("companies hiring for dbt, last
    quarter") against declarative prose, and the nearest neighbours would partly reflect that
    difference in form rather than in meaning.

    So the query is rendered into the same register the documents are written in:

        chips(terms=[SAP·USES, Snowflake·USES], industry="Manufacturing", intent_mode=USES)
          -> "Manufacturing company running SAP and Snowflake."

        chips(terms=[dbt·HIRING], function=DATA_ENGINEERING, intent_mode=HIRING)
          -> "Company hiring for dbt, in data engineering roles."

    This is a deterministic template on purpose. Asking an LLM to write it would put a
    nondeterministic step back inside the ranking path.
    """
    uses, hiring, unassigned = bucket_terms(chips)

    subject = f"{chips.industry.strip()} company" if chips.industry else "Company"
    clauses: list[str] = []
    if uses:
        clauses.append(f"running {_and_join(uses)}")
    if hiring:
        clauses.append(f"hiring for {_and_join(hiring)}")
    if unassigned:
        clauses.append(f"working with {_and_join(unassigned)}")

    role = " ".join(
        part
        for part in (
            _humanize(chips.seniority.value) if chips.seniority else "",
            _humanize(chips.function.value) if chips.function else "",
        )
        if part
    )
    if role:
        clauses.append(f"in {role} roles")

    # The first clause joins with a space, the rest with commas: the corpus's paraphrases read
    # "Large manufacturing company running SAP S/4HANA and AWS, focused on cloud modernization",
    # not "Large manufacturing company, running ...". Rule 4 is about matching the documents'
    # register, and a stray comma is a token the document side does not have.
    if not clauses:
        return subject + "."
    sentence = f"{subject} {clauses[0]}"
    if len(clauses) > 1:
        sentence += ", " + ", ".join(clauses[1:])
    return sentence.strip() + "."


def chip_phrases(chips: Chips) -> list[str]:
    """The phrases the lexical lists OR together (§6[3a]).

    The terms and the industry — the content words. Deduped, order-stable, so the assembled
    tsquery is byte-identical for identical chips.

    Note the industry IS a lexical phrase while remaining a non-filter: contributing to a rank is
    not the same as removing a row (rule 2). A company in the wrong industry still retrieves; it
    is §8.5 that down-weights it, and even then it is never dropped.
    """
    phrases = [t.value.strip() for t in chips.terms if t.value.strip()]
    if chips.industry and chips.industry.strip():
        phrases.append(chips.industry.strip())
    return list(dict.fromkeys(phrases))


def hard_filters(chips: Chips) -> dict[str, Any]:
    """§6[2] — the only stage permitted to remove a candidate. Facts, and nothing else.

    `industry` is deliberately not here (rule 2). Neither are terms. `function`/`seniority` are
    here but scope the *job evidence set*, not the company set — see the note in `retrieve`.
    """
    return {
        "since_days": chips.since_days,
        "function": chips.function.value if chips.function else None,
        "seniority": chips.seniority.value if chips.seniority else None,
        "min_employees": chips.min_employees,
        "max_employees": chips.max_employees,
        "min_revenue_usd": chips.min_revenue_usd,
        "max_revenue_usd": chips.max_revenue_usd,
    }


def canonical_industry(conn: psycopg.Connection, asked: str | None) -> str | None:
    """Map the asked industry onto the §5.5 vocabulary, or leave it alone.

    §8.5's first tier is `company.industry_canonical == asked_industry` — a string equality, so
    the two sides have to be spelled the same way. `company_signal.industry_canonical` holds a
    value from `lead_query WHERE type='COMPANY_INDUSTRY'`; a user (or an eval file) types
    "manufacturing". Without this step the equality never fires and every company falls to the
    0.35 tier, which would look like the multiplier is broken.

    Deliberately exact-match only (punctuation-and-case-insensitive), with no embedding fallback:
    this runs inside `/api/search/structured`, which must stay deterministic and network-free.
    A near-miss that this does not resolve is not lost — it is precisely what §8.5's cosine tier
    is for, and that comparison happens in SQL against the stored `industry_embedding`.
    """
    if not asked or not asked.strip():
        return None
    wanted = tech_key(asked)
    for value in repository.fetch_industry_vocabulary(conn):
        if tech_key(value) == wanted:
            return value
    return asked.strip()


# ---------------------------------------------------------------------------
# Query vectors (rule 4). Cached — an embedding call is the slowest thing in the path.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryVectors:
    paraphrase: str
    qvec: list[float]
    industry_vec: list[float] | None


_VEC_CACHE: dict[str, list[float]] = {}


async def _embed_cached(text: str) -> list[float]:
    """Embed, memoised on the exact text.

    The cache is what makes a repeated query cost 0ms instead of ~200ms, and it is also what
    makes byte-identical output on a re-run a guarantee rather than an observation about
    OpenAI's floating point.
    """
    if text not in _VEC_CACHE:
        _VEC_CACHE[text] = await embed.embed_text(text)
    return _VEC_CACHE[text]


async def prepare(chips: Chips) -> QueryVectors:
    """Build the query paraphrase and its vector(s). The only await in the retrieval path."""
    paraphrase = query_paraphrase(chips)
    qvec = await _embed_cached(paraphrase)
    industry_vec = None
    if chips.industry and chips.industry.strip():
        # §8.5 compares emb(company.industry_raw) to emb(asked_industry). The company side was
        # embedded at ingest from the bare industry string, so the query side must be the bare
        # string too — embedding "Manufacturing company running SAP" here instead would be
        # comparing two different things and the 0.82 threshold would mean nothing.
        industry_vec = await _embed_cached(chips.industry.strip())
    return QueryVectors(paraphrase=paraphrase, qvec=qvec, industry_vec=industry_vec)


# ---------------------------------------------------------------------------
# §8.1 — fusion
# ---------------------------------------------------------------------------


def to_company_ranks(job_ranks: dict[int, int], job_to_company: dict[int, int]) -> dict[int, int]:
    """§8.1, verbatim: job-level ranks -> company-level ranks, best job's rank wins.

    A company inherits only its **best** job's rank. Three good jobs do not make a company three
    times more relevant — that is what `volume` is for (§8.3), as a separate axis with its own
    weight, so the two effects can be tuned apart.

    Deviation from the spec's snippet, deliberately: the final sort breaks ties on `cid` as well
    as on rank. Ties are guaranteed here (every company whose best job ranked 7th is tied), and
    the spec's `sorted(..., key=lambda kv: kv[1])` would resolve them by dict insertion order —
    stable, but stable in terms of an ordering nothing else guarantees. Ranking must be
    reproducible; this makes it so.
    """
    best: dict[int, int] = {}
    for job_id, rank in job_ranks.items():
        cid = job_to_company[job_id]
        best[cid] = min(best.get(cid, 10**9), rank)
    ordered = sorted(best.items(), key=lambda kv: (kv[1], kv[0]))
    return {cid: i + 1 for i, (cid, _) in enumerate(ordered)}


def rrf(*lists: dict[int, int]) -> dict[int, float]:
    """§8.1, verbatim: Σ 1/(60+rank) over the company-level lists.

    RRF fuses *ranks*, never scores, which is the entire reason it is here: `ts_rank` and cosine
    are not commensurable — they have different scales, different distributions, and no shared
    zero. Any attempt to add them directly needs a normalisation nobody can justify.
    """
    scores: dict[int, float] = defaultdict(float)
    for ranks in lists:
        for cid, rank in ranks.items():
            scores[cid] += 1.0 / (K_RRF + rank)
    return dict(scores)


def normalize_01(scores: dict[int, float]) -> dict[int, float]:
    """Min-max to 0..1 — §8.1's `best_doc = normalize_01(fused)`.

    When every candidate has the same fused score (one candidate, or a perfectly flat list) the
    axis carries no information, so it is 1.0 for everyone rather than 0.0: they all retrieved
    equally well, and 0.0 would silently claim the opposite.
    """
    if not scores:
        return {}
    values = list(scores.values())
    low, high = min(values), max(values)
    if high - low < 1e-12:
        return {cid: 1.0 for cid in scores}
    return {cid: (value - low) / (high - low) for cid, value in scores.items()}


def _ranks(rows: Sequence[dict[str, Any]], key: str) -> dict[int, int]:
    """A SQL result list -> {id: 1-based rank}. The rows arrive already ordered by the query."""
    return {row[key]: i + 1 for i, row in enumerate(rows)}


# ---------------------------------------------------------------------------
# The retrieval itself
# ---------------------------------------------------------------------------


@dataclass
class Retrieval:
    """What §6[4] hands to §6[5]: a candidate set, its fused rank, and its evidence pool."""

    fused: dict[int, float]
    best_doc: dict[int, float]
    company_jobs: dict[int, list[dict[str, Any]]]
    retrieved_job_ids: dict[int, set[int]]
    details: dict[int, dict[str, Any]]
    list_sizes: dict[str, int] = field(default_factory=dict)

    @property
    def candidates(self) -> list[int]:
        return sorted(self.fused)


def retrieve(conn: psycopg.Connection, chips: Chips, vectors: QueryVectors) -> Retrieval:
    """§6 steps [2]-[4]. Pure SQL and arithmetic; deterministic; no LLM.

    On `function`/`seniority`: §6[1] puts them in `Chips` but §6[2] does not list them as hard
    filters, and rule 2's reasoning tells you why the distinction matters — they are LLM-inferred
    enums, not facts like `posted_date`. So they scope the **job document set** (the lists, and
    equally the evidence, recency and volume computed from it) and never the company set. Ask for
    "manufacturers hiring data engineers" and a company whose only DATA_ENGINEERING posting is
    old still appears through the company lists, with `recency` 0.0 — down-weighted, not deleted,
    exactly as rule 2 requires.
    """
    filters = hard_filters(chips)
    phrases = chip_phrases(chips)

    # [3] Four lists, top 200 each. The two job lists share one filtered survivor set; the two
    # company lists share the other (no date filter — companies have no posted_date, §6[3b]).
    job_lex = repository.search_jobs_lexical(conn, phrases=phrases, filters=filters, limit=LIST_DEPTH) if phrases else []
    job_sem = repository.search_jobs_semantic(conn, qvec=vectors.qvec, filters=filters, limit=LIST_DEPTH)
    com_lex = repository.search_companies_lexical(conn, phrases=phrases, filters=filters, limit=LIST_DEPTH) if phrases else []
    com_sem = repository.search_companies_semantic(conn, qvec=vectors.qvec, filters=filters, limit=LIST_DEPTH)

    job_to_company: dict[int, int] = {}
    for row in (*job_lex, *job_sem):
        job_to_company[row["job_id"]] = row["company_id"]

    # [4] Project jobs -> companies FIRST, then fuse. Fusing at job level and grouping afterwards
    # would let a company with many mediocre postings out-score one with a single excellent
    # posting, because it would collect a 1/(60+rank) contribution for each of them.
    l1 = to_company_ranks(_ranks(job_lex, "job_id"), job_to_company)
    l2 = to_company_ranks(_ranks(job_sem, "job_id"), job_to_company)
    l3 = _ranks(com_lex, "company_id")
    l4 = _ranks(com_sem, "company_id")

    fused = rrf(l1, l2, l3, l4)
    best_doc = normalize_01(fused)

    candidates = sorted(fused)

    retrieved_job_ids: dict[int, set[int]] = defaultdict(set)
    for job_id, cid in job_to_company.items():
        retrieved_job_ids[cid].add(job_id)

    details = {
        row["company_id"]: row
        for row in repository.fetch_company_details(
            conn, company_ids=candidates, industry_vec=vectors.industry_vec
        )
    }

    company_jobs: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in repository.fetch_jobs_for_companies(conn, company_ids=candidates, filters=filters):
        company_jobs[row["company_id"]].append(row)

    return Retrieval(
        fused=fused,
        best_doc=best_doc,
        company_jobs=dict(company_jobs),
        retrieved_job_ids=dict(retrieved_job_ids),
        details=details,
        list_sizes={
            "job_lexical": len(job_lex),
            "job_semantic": len(job_sem),
            "company_lexical": len(com_lex),
            "company_semantic": len(com_sem),
            "candidates": len(candidates),
        },
    )
