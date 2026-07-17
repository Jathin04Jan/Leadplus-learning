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
from typing import TYPE_CHECKING, Any, Sequence

import psycopg

from . import embed, repository
from .canonicalize import industry_key, location_key, tech_key
from .models import Chips, IntentMode, TermGroup, TermSource, Value

if TYPE_CHECKING:  # pragma: no cover — import only for the type, never at runtime.
    # score.py imports Retrieval from this module, so importing score here for real would be a
    # cycle. The matcher is passed in by the caller (main.py builds it once per request anyway).
    from .score import TermMatcher

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


def _or_join(values: Sequence[str]) -> str:
    """`[AWS, Azure]` -> `AWS or Azure`. A group's alternates, as English (CHANGES-v2 §2)."""
    return " or ".join(v for v in values if v)


def bucket_terms(chips: Chips) -> tuple[list[str], list[str], list[str]]:
    """Split the POSITIVE groups into (uses, hiring, unassigned) by source, resolving ANY by intent.

    A group whose source is ANY has no side of its own, so the intent mode decides which side the
    *paraphrase* should read as. This only shapes the text we embed — §8.4 still checks an ANY
    group against the union of both haystacks, so nothing is narrowed here.

    Negated groups are absent by construction: they are filters (§2.1), and the retrieval SQL has
    already removed everything they match. Writing "not on S/4HANA" into the text we embed would
    move the query vector *towards* S/4HANA documents — embeddings have no `NOT`.
    """
    uses: list[str] = []
    hiring: list[str] = []
    unassigned: list[str] = []
    for group in chips.positive_groups():
        value = _or_join([v.strip() for v in group.any_of])
        if not value:
            continue
        if group.source == TermSource.USES:
            uses.append(value)
        elif group.source == TermSource.HIRING:
            hiring.append(value)
        elif chips.intent_mode == IntentMode.USES:
            uses.append(value)
        elif chips.intent_mode == IntentMode.HIRING:
            hiring.append(value)
        else:
            unassigned.append(value)
    return uses, hiring, unassigned


def positive_values(values: Sequence[Value]) -> list[str]:
    return [v.value.strip() for v in values if not v.negate and v.value.strip()]


def negated_values(values: Sequence[Value]) -> list[str]:
    return [v.value.strip() for v in values if v.negate and v.value.strip()]


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

    industries = positive_values(chips.industries)
    subject = f"{_or_join(industries)} company" if industries else "Company"
    clauses: list[str] = []
    if uses:
        clauses.append(f"running {_and_join(uses)}")
    if hiring:
        clauses.append(f"hiring for {_and_join(hiring)}")
    if unassigned:
        clauses.append(f"working with {_and_join(unassigned)}")

    # §3 — the corpus's company paraphrases name the place ("Mid-size industrial machinery
    # manufacturer in Ohio running SAP ECC…"), so the query says it the same way (rule 4). This
    # cannot widen the result set: `locations` is already a hard filter and every retrieved row
    # satisfies it. It only helps the cosine order *within* the surviving set.
    locations = positive_values(chips.locations)
    if locations:
        clauses.append(f"based in {_or_join(locations)}")

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

    The positive groups' alternates and the positive industries — the content words. Deduped,
    order-stable, so the assembled tsquery is byte-identical for identical chips.

    Every alternate of a group is its own phrase. The lists are candidate *generators* and they
    already OR everything (see `_TSQUERY_CTE`'s note); it is coverage that knows `AWS or Azure` is
    one requirement rather than two, and coverage runs later, in Python.

    Negated groups are excluded: their rows are gone, so a phrase for them could only pull in
    *other* rows that happen to mention the excluded thing.

    Note the industry IS a lexical phrase while remaining a non-filter by default: contributing to
    a rank is not the same as removing a row (rule 2). A company in the wrong industry still
    retrieves; §8.5 down-weights it, and even then it is never dropped — unless the user asked for
    `industry_strict`, which is their explicit override (§5).
    """
    phrases = [v.strip() for g in chips.positive_groups() for v in g.any_of if v.strip()]
    phrases.extend(positive_values(chips.industries))
    return list(dict.fromkeys(phrases))


def canonical_industries(conn: psycopg.Connection, values: Sequence[Value]) -> list[Value]:
    """Map each asked industry onto the §5.5 vocabulary, or leave it alone.

    §8.5's first tier is `company.industry_canonical == asked_industry` — a string equality, so
    the two sides have to be spelled the same way. `company_signal.industry_canonical` holds a
    value from `lead_query WHERE type='COMPANY_INDUSTRY'`; a user (or an eval file) types
    "manufacturing". Without this step the equality never fires and every company falls to the
    0.35 tier, which would look like the multiplier is broken. With `industry_strict` it matters
    far more than that: the equality is a *filter*, so a spelling miss returns zero rows.

    Deliberately exact-match only (punctuation-and-case-insensitive), with no embedding fallback:
    this runs inside `/api/search/structured`, which must stay deterministic and network-free.
    A near-miss that this does not resolve is not lost — it is precisely what §8.5's cosine tier
    is for, and that comparison happens in SQL against the stored `industry_embedding`.
    """
    if not values:
        return []
    vocabulary = {tech_key(v): v for v in repository.fetch_industry_vocabulary(conn)}
    out: list[Value] = []
    for value in values:
        asked = value.value.strip()
        if not asked:
            continue
        out.append(value.model_copy(update={"value": vocabulary.get(tech_key(asked), asked)}))
    return out


# ---------------------------------------------------------------------------
# §6[2] — the predicate. CHANGES-v2 §2.1/§3.2/§4/§5 all land here.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Predicate:
    """The SQL parameters for one query, plus what had to be said out loud to build them.

    `shadow` is `filters` with the negation cleared. It exists for §10's `excluded_by`: the only
    way to report *which companies a negated group removed* is to know which ones would have been
    retrieved without it. Built here rather than in `retrieve()` so the two dicts cannot drift.
    """

    filters: dict[str, Any]
    shadow: dict[str, Any]
    negated: list[TermGroup]  # canonicalised — `any_of` holds `tech_canonical` terms
    notes: list[str] = field(default_factory=list)


def _canonical_negated(
    chips: Chips, matcher: "TermMatcher"
) -> tuple[list[TermGroup], list[str]]:
    """Resolve each negated alternate against `tech_canonical` — §2.1's guard rail, in Python.

    The SQL negates with `technologies && ARRAY[...]`, i.e. **exact element equality against the
    controlled vocabulary**. So "exclude S/4HANA" has to arrive spelled `SAP S/4HANA`, the way the
    array holds it, or the exclusion is a silent no-op.

    A term that resolves to nothing is passed through verbatim and **reported**. It cannot match
    the array, so it removes nothing — and that is the correct failure: the alternative is falling
    back to a substring match, which is precisely the `LIKE '%sap%'` that deletes Sapient. A
    no-op the user is told about beats a deletion they cannot see (§2.1). The note reaches the
    response and the UI.
    """
    out: list[TermGroup] = []
    notes: list[str] = []
    for group in chips.negated_groups():
        resolved: list[str] = []
        for alternate in group.any_of:
            alternate = alternate.strip()
            if not alternate:
                continue
            canonical = matcher.resolve(alternate)
            if canonical is None:
                notes.append(
                    f"cannot exclude {alternate!r}: it is not a known technology, and §2.1 forbids "
                    f"matching a negation against prose (a substring NOT would delete 'Sapient' "
                    f"for 'SAP'). Nothing was removed for this term."
                )
                log.warning("unresolvable negation term: %r", alternate)
                resolved.append(alternate)
            else:
                resolved.append(canonical)
        if resolved:
            out.append(group.model_copy(update={"any_of": resolved}))
    return out, notes


def _expand_industries(
    conn: psycopg.Connection, values: Sequence[str], *, strict: bool
) -> tuple[list[str], list[str]]:
    """Asked industry -> the SET of taxonomy values to filter on, via `industry_alias`.

    **This is where rule 2's `industry` exception is retired, so here is the argument.** Rule 2
    says industry must stay soft "because it is free text (`Industrial Machinery` vs the user's
    `manufacturing`)". That premise is false on this corpus: `lead_company.industry` is a 95-value
    closed taxonomy, declared in `lead_query`, never free-typed. Rule 2's headline — *filter on
    facts* — therefore covers it, and the exception was written for a column that does not exist
    here. What DID exist was the gap between the user's word and the taxonomy's 47 spellings of
    it, and that gap is what this table closes. The rule was right about the problem and wrong
    about the column.

    The soft multiplier's measured cost: "companies in the automotive industry with revenue over
    $100M" returned Industrial-Machinery and Logistics firms at 0.35x — still on page one. There
    are 4 automotive companies in the pool and none over $100M. Zero was the answer.

    Two modes, and `strict` is CHANGES-v2 §5's override kept intact:

      * default -> the EXPANDED set. "manufacturing" -> 47 values / 11,036 companies. Filtering on
        the literal string instead would return 1,067 and silently delete 90% of the right answers
        — rule 2's warning coming true by a different route, which is why expansion comes first.
      * strict  -> the EXACT value only. "strictly manufacturing" -> `Manufacturing`, 1,067. The
        user said an insisting word and gets the literal category. §5's contract is unchanged: the
        override is the user accepting deletions. It now narrows a filter rather than turning one on.

    An unresolvable industry is passed through verbatim and **reported**, exactly as
    `_expand_locations` does with "Wakanda" — it matches nothing, so the query returns an honest
    zero and says why. It is NOT dropped. Dropping it is the tempting move and it is the defect:
    "companies categorized under 'Tech' and 'Enterprise' with no LinkedIn" would silently become
    "companies with no LinkedIn" — 5,251 confident answers to a question nobody asked. A filter the
    user asked for and we cannot honour must fail loudly, never quietly widen.
    """
    if not values:
        return [], []
    keys = [industry_key(v) for v in values]
    rows = repository.expand_industries(conn, keys)
    by_alias: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        by_alias[row["alias"]].append(row["canonical"])

    out: set[str] = set()
    notes: list[str] = []
    for raw, key in zip(values, keys):
        covered = by_alias.get(key, [])
        if not covered:
            notes.append(
                f"unknown industry {raw!r}: it is not one of the 95 values in this corpus's "
                f"industry taxonomy and no alias maps to it, so it was matched literally and will "
                f"return nothing. It was NOT ignored — silently dropping it would answer a "
                f"different question. Re-seed with scripts/bootstrap_industries.py if it is real."
            )
            log.warning("unresolvable industry: %r", raw)
            out.add(key)
        elif strict:
            # `industry_strict`: the literal category, not its family. Prefer the value whose own
            # name IS what the user typed; if the word is only ever a family word ("automotive" is
            # not itself a taxonomy value) there is no literal to narrow to, so the expansion
            # stands and the note says so rather than returning a silent zero.
            exact = [c for c in covered if industry_key(c) == key]
            if exact:
                out.update(exact)
            else:
                out.update(covered)
                notes.append(
                    f"industry_strict was asked for {raw!r}, but {raw!r} is not itself a taxonomy "
                    f"value — it is a family word covering {len(covered)}. Filtered on all "
                    f"{len(covered)}; there is no narrower literal to insist on."
                )
        else:
            out.update(covered)
    return sorted(out), notes


def _expand_locations(
    conn: psycopg.Connection, values: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Raw location text -> canonical values, via `location_alias` (§3.1).

    Unknown text is passed through lowercased rather than dropped, and reported. Dropping it would
    turn "manufacturers in Wakanda" into "manufacturers" — 301 confident results for a question
    with no answer, which is §1's disease. Passing it through returns an honest zero.
    """
    if not values:
        return [], []
    keys = [location_key(v) for v in values]
    resolved = {r["alias"]: r["canonical"] for r in repository.expand_locations(conn, keys)}
    out: list[str] = []
    notes: list[str] = []
    for raw, key in zip(values, keys):
        canonical = resolved.get(key)
        if canonical is None:
            notes.append(
                f"unknown location {raw!r}: not in `location_alias`, so it was matched literally "
                f"and will return nothing. Re-seed with scripts/bootstrap_locations.py if it is real."
            )
            out.append(key)
        else:
            out.append(canonical)
    return sorted(set(out)), notes


def build_filters(
    conn: psycopg.Connection, chips: Chips, matcher: "TermMatcher"
) -> Predicate:
    """§6[2] — the only stage permitted to remove a candidate. Facts, and nothing else.

    What is here and why it is allowed to be (rule 2 / CHANGES-v2 §12):

      * `posted_date`, employees, revenue — the original three. Facts.
      * `locations` (§3.2)   — a state is a fact. Expanded through `location_alias` first.
      * `segments`/`naics`/`sic`/`has_linkedin` (§4) — facts on `lead_company`. A NULL check is
        exact; there is nothing fuzzy to get wrong.
      * negated groups (§2.1) — canonical `technologies[]` only. **Never** prose.
      * `industries` — ONLY when `industry_strict`, or when a value is negated (§5). Otherwise the
        list is absent from here entirely and lives in §8.5's multiplier, because `industry` is
        free text and hard-filtering free text silently deletes correct answers.

    `function`/`seniority` are in the dict but scope the *job evidence set*, not the company set —
    see the note in `retrieve()`.

    Every array is `sorted(set(...))`: these become SQL parameters, and an unstable parameter
    order would make identical chips produce a different query string on a re-run.
    """
    negated, notes = _canonical_negated(chips, matcher)

    neg_uses = sorted(
        {a for g in negated if g.source in (TermSource.USES, TermSource.ANY) for a in g.any_of}
    )
    neg_hiring = sorted(
        {a for g in negated if g.source in (TermSource.HIRING, TermSource.ANY) for a in g.any_of}
    )

    loc_pos, pos_notes = _expand_locations(conn, positive_values(chips.locations))
    loc_neg, neg_notes = _expand_locations(conn, negated_values(chips.locations))
    notes.extend(pos_notes)
    notes.extend(neg_notes)

    # Task 2 — industry is now a HARD filter, expanded through `industry_alias` (see
    # `_expand_industries` for why rule 2's soft-multiplier exception is retired here). Positives
    # expand to the family (or, under `industry_strict`, narrow to the literal value); negatives
    # always expand — "not manufacturing" must exclude all 47 manufacturing values, not just the
    # one literally spelled "Manufacturing". Both come back as the verbatim taxonomy strings and
    # are lowercased to meet `_FACT_FILTERS`, which compares `lower(industry_canonical)`.
    ind_pos, ind_pos_notes = _expand_industries(
        conn, positive_values(chips.industries), strict=chips.industry_strict
    )
    ind_neg, ind_neg_notes = _expand_industries(
        conn, negated_values(chips.industries), strict=False
    )
    notes.extend(ind_pos_notes)
    notes.extend(ind_neg_notes)

    filters: dict[str, Any] = {
        "since_days": chips.since_days,
        "function": chips.function.value if chips.function else None,
        "seniority": chips.seniority.value if chips.seniority else None,
        "min_employees": chips.min_employees,
        "max_employees": chips.max_employees,
        "min_revenue_usd": chips.min_revenue_usd,
        "max_revenue_usd": chips.max_revenue_usd,
        "loc_pos": loc_pos,
        "loc_neg": loc_neg,
        "segments": sorted({s.strip() for s in chips.segments if s.strip()}),
        "naics": sorted({s.strip() for s in chips.naics if s.strip()}),
        "sic": sorted({s.strip() for s in chips.sic if s.strip()}),
        "has_linkedin": chips.has_linkedin,
        # Task 2 — always a hard filter now, over the EXPANDED taxonomy set (or the exact value
        # under `industry_strict`). The old code only filtered when the user said "strictly" and
        # let §8.5's multiplier carry the rest — which is how "automotive, revenue > $100M"
        # returned Industrial-Machinery firms. `ind_pos`/`ind_neg` are already the resolved
        # taxonomy strings; lowercased here to meet `_FACT_FILTERS`'s `lower(industry_canonical)`.
        "industry_pos": sorted({v.lower() for v in ind_pos}),
        "industry_neg": sorted({v.lower() for v in ind_neg}),
        "neg_uses": neg_uses,
        "neg_hiring": neg_hiring,
    }
    shadow = {**filters, "neg_uses": [], "neg_hiring": []}
    return Predicate(filters=filters, shadow=shadow, negated=negated, notes=notes)


# ---------------------------------------------------------------------------
# Query vectors (rule 4). Cached — an embedding call is the slowest thing in the path.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryVectors:
    paraphrase: str
    qvec: list[float]
    industry_vecs: list[list[float]]


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
    """Build the query paraphrase and its vector(s). The only await in the retrieval path.

    One vector per *positive* asked industry, because §10 takes `max()` across the list. Negated
    industries get none: they are a hard filter (§5), and their rows are gone before §8.5 looks.

    With `industry_strict` the multiplier is skipped entirely (§5) — but the vectors are still
    built, because `industry_similarity` is reported in the `Breakdown` either way and a number
    the user can see is how they check that "strictly" did what they meant.
    """
    paraphrase = query_paraphrase(chips)
    qvec = await _embed_cached(paraphrase)
    industry_vecs: list[list[float]] = []
    for value in positive_values(chips.industries):
        # §8.5 compares emb(company.industry_raw) to emb(asked_industry). The company side was
        # embedded at ingest from the bare industry string, so the query side must be the bare
        # string too — embedding "Manufacturing company running SAP" here instead would be
        # comparing two different things and the 0.82 threshold would mean nothing.
        industry_vecs.append(await _embed_cached(value))
    return QueryVectors(paraphrase=paraphrase, qvec=qvec, industry_vecs=industry_vecs)


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
    notes: list[str] = field(default_factory=list)

    # CHANGES-v2 §10 — the *un-negated* retrieval: what would have been returned had the query
    # not excluded anything. None when nothing was negated. Scoring this whole set is what lets an
    # exclusion be reported as "would have ranked #1" rather than "was removed, trust me".
    excluded: "Retrieval | None" = None
    # The subset of `excluded`'s candidates that the negation actually removed.
    excluded_ids: list[int] = field(default_factory=list)
    excluded_by: dict[int, list[str]] = field(default_factory=dict)

    @property
    def candidates(self) -> list[int]:
        return sorted(self.fused)


def _fuse(
    conn: psycopg.Connection,
    *,
    phrases: Sequence[str],
    qvec: Sequence[float],
    filters: dict[str, Any],
) -> tuple[dict[int, float], dict[int, float], dict[int, int], dict[str, int]]:
    """§6[3]-[4] — the retrieval lists and their fusion, for one set of filters.

    SIX lists now, not four. The two added ones retrieve over `job_intent` — the finer grain
    adopted from the other team — and they are fused by the SAME company-level RRF as the rest
    (§8.1), which is the whole reason adding a source is cheap here: RRF takes ranks, so a new
    scorer needs no re-weighting, no normalisation and no tuning to join. It contributes
    1/(60+rank) like everything else.

    They are a genuinely different signal, not a duplicate of `job_signal`: a paraphrase and its
    intents are two readings of one posting, and a query phrased as an initiative ("erp
    transformation program") hits the intent index precisely while hitting the paraphrase only
    diffusely. A company found by both rises — which is exactly what §5.1 says two document types
    are for, applied at a finer grain.
    """
    # The job-side lists share one filtered survivor set; the company lists share the other
    # (no date filter — companies have no posted_date, §6[3b]).
    job_lex = (
        repository.search_jobs_lexical(conn, phrases=phrases, filters=filters, limit=LIST_DEPTH)
        if phrases
        else []
    )
    job_sem = repository.search_jobs_semantic(conn, qvec=qvec, filters=filters, limit=LIST_DEPTH)
    int_lex = (
        repository.search_job_intents_lexical(conn, phrases=phrases, filters=filters, limit=LIST_DEPTH)
        if phrases
        else []
    )
    int_sem = repository.search_job_intents_semantic(conn, qvec=qvec, filters=filters, limit=LIST_DEPTH)
    com_lex = (
        repository.search_companies_lexical(conn, phrases=phrases, filters=filters, limit=LIST_DEPTH)
        if phrases
        else []
    )
    com_sem = repository.search_companies_semantic(conn, qvec=qvec, filters=filters, limit=LIST_DEPTH)

    job_to_company: dict[int, int] = {}
    for row in (*job_lex, *job_sem, *int_lex, *int_sem):
        job_to_company[row["job_id"]] = row["company_id"]

    # [4] Project jobs -> companies FIRST, then fuse. Fusing at job level and grouping afterwards
    # would let a company with many mediocre postings out-score one with a single excellent
    # posting, because it would collect a 1/(60+rank) contribution for each of them.
    l1 = to_company_ranks(_ranks(job_lex, "job_id"), job_to_company)
    l2 = to_company_ranks(_ranks(job_sem, "job_id"), job_to_company)
    l3 = _ranks(com_lex, "company_id")
    l4 = _ranks(com_sem, "company_id")
    l5 = to_company_ranks(_ranks(int_lex, "job_id"), job_to_company)
    l6 = to_company_ranks(_ranks(int_sem, "job_id"), job_to_company)

    fused = rrf(l1, l2, l3, l4, l5, l6)
    sizes = {
        "job_lexical": len(job_lex),
        "job_semantic": len(job_sem),
        "intent_lexical": len(int_lex),
        "intent_semantic": len(int_sem),
        "company_lexical": len(com_lex),
        "company_semantic": len(com_sem),
        "candidates": len(fused),
    }
    return fused, normalize_01(fused), job_to_company, sizes


def _hydrate(
    conn: psycopg.Connection,
    *,
    company_ids: Sequence[int],
    fused: dict[int, float],
    best_doc: dict[int, float],
    job_to_company: dict[int, int],
    filters: dict[str, Any],
    vectors: QueryVectors,
    list_sizes: dict[str, int],
) -> Retrieval:
    """Attach the scoring inputs — details, jobs, evidence — for a candidate set."""
    wanted = set(company_ids)  # hoisted: rebuilding this per job is O(jobs x candidates)
    retrieved_job_ids: dict[int, set[int]] = defaultdict(set)
    for job_id, cid in job_to_company.items():
        if cid in wanted:
            retrieved_job_ids[cid].add(job_id)

    details = {
        row["company_id"]: row
        for row in repository.fetch_company_details(
            conn, company_ids=company_ids, industry_vecs=vectors.industry_vecs
        )
    }
    company_jobs: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in repository.fetch_jobs_for_companies(conn, company_ids=company_ids, filters=filters):
        company_jobs[row["company_id"]].append(row)

    return Retrieval(
        fused={cid: fused[cid] for cid in company_ids if cid in fused},
        best_doc={cid: best_doc[cid] for cid in company_ids if cid in best_doc},
        company_jobs=dict(company_jobs),
        retrieved_job_ids=dict(retrieved_job_ids),
        details=details,
        list_sizes=list_sizes,
    )


def attribute_exclusions(
    conn: psycopg.Connection, *, company_ids: Sequence[int], negated: Sequence[TermGroup]
) -> dict[int, list[str]]:
    """Which negated group removed each company — §10's `excluded_by`.

    Mirrors `_FACT_FILTERS`'s two `NOT EXISTS` clauses exactly: the same canonical arrays, the
    same source routing, the same exact equality. It has to, or the explanation would be able to
    disagree with the filter that produced it — and an explanation that can lie about a removal is
    worse than none.
    """
    if not company_ids or not negated:
        return {}
    rows = repository.fetch_company_technology_sets(conn, company_ids=company_ids)
    out: dict[int, list[str]] = {}
    for row in rows:
        uses = {t.lower() for t in row["uses_technologies"] or []}
        hiring = {t.lower() for t in row["hiring_technologies"] or []}
        fired: list[str] = []
        for group in negated:
            hay = set()
            if group.source in (TermSource.USES, TermSource.ANY):
                hay |= uses
            if group.source in (TermSource.HIRING, TermSource.ANY):
                hay |= hiring
            hit = [a for a in group.any_of if a.lower() in hay]
            if hit:
                fired.append(f"NOT {_or_join(hit)}" if len(hit) < len(group.any_of) else f"NOT {group.label}")
        if fired:
            out[row["company_id"]] = fired
    return out


def retrieve(
    conn: psycopg.Connection, chips: Chips, vectors: QueryVectors, matcher: "TermMatcher"
) -> Retrieval:
    """§6 steps [2]-[4]. Pure SQL and arithmetic; deterministic; no LLM.

    On `function`/`seniority`: §6[1] puts them in `Chips` but §6[2] does not list them as hard
    filters, and rule 2's reasoning tells you why the distinction matters — they are LLM-inferred
    enums, not facts like `posted_date`. So they scope the **job document set** (the lists, and
    equally the evidence, recency and volume computed from it) and never the company set. Ask for
    "manufacturers hiring data engineers" and a company whose only DATA_ENGINEERING posting is
    old still appears through the company lists, with `recency` 0.0 — down-weighted, not deleted,
    exactly as rule 2 requires.

    On the shadow run (CHANGES-v2 §10): when the query negates something, the four lists run a
    second time with the negation cleared. The difference between the two candidate sets is
    exactly "the companies the exclusion removed", and each is scored and returned in
    `excluded` with the rank it would have held. It costs four more SQL queries over 687 rows,
    only when a negation exists — and it is what turns "trust me, I removed something" into
    "here is what I removed, here is what it would have scored, and here is the group that did it".
    """
    predicate = build_filters(conn, chips, matcher)
    phrases = chip_phrases(chips)

    fused, best_doc, job_to_company, sizes = _fuse(
        conn, phrases=phrases, qvec=vectors.qvec, filters=predicate.filters
    )
    retrieval = _hydrate(
        conn,
        company_ids=sorted(fused),
        fused=fused,
        best_doc=best_doc,
        job_to_company=job_to_company,
        filters=predicate.filters,
        vectors=vectors,
        list_sizes=sizes,
    )
    retrieval.notes = list(predicate.notes)

    if predicate.negated:
        s_fused, s_best, s_j2c, s_sizes = _fuse(
            conn, phrases=phrases, qvec=vectors.qvec, filters=predicate.shadow
        )
        removed = sorted(set(s_fused) - set(fused))
        if removed:
            # Hydrate the WHOLE shadow set, not just the removed rows. `best_doc` is a min-max
            # normalisation over the candidate set it was computed from, and a rank is a position
            # within a list — both are meaningless in a subset. Scoring the full un-negated set is
            # the only way "would have ranked #1" is a measurement rather than a guess.
            retrieval.excluded = _hydrate(
                conn,
                company_ids=sorted(s_fused),
                fused=s_fused,
                best_doc=s_best,
                job_to_company=s_j2c,
                filters=predicate.shadow,
                vectors=vectors,
                list_sizes=s_sizes,
            )
            retrieval.excluded_ids = removed
            retrieval.excluded_by = attribute_exclusions(
                conn, company_ids=removed, negated=predicate.negated
            )

    return retrieval
