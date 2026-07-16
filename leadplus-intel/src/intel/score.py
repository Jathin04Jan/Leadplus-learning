"""Phase 7 — scoring (ARCHITECTURE.md §8.2-§8.6).

Deterministic arithmetic over the candidate set §6[4] produced. No LLM, no I/O, no clock beyond
the one `now` the caller pins for the whole request.

The shape of the thing: four axes in 0..1 (§8.3), weighted by a profile the intent mode selects
(§8.2), multiplied by a soft industry factor (§8.5). Nothing in here can remove a company — the
worst that happens to a bad candidate is a low score. That is rule 2, and it is what kills the
AND/OR cliff: a company matching one of three terms scores lower than one matching three, and is
still returned.

Every number in `WEIGHTS` and in the industry thresholds is **invented** — §8.6 says so
explicitly. They are a starting hypothesis, tuned against the golden set by `scripts/eval.py`,
and they are overridable per-call so the tuner can sweep them without editing this file.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .canonicalize import tech_key
from .models import (
    Breakdown,
    Chips,
    CompanyResult,
    Evidence,
    IntentMode,
    Term,
    TermSource,
)
from .retrieve import Retrieval

# ---------------------------------------------------------------------------
# §8.2 — intent modes
# ---------------------------------------------------------------------------

# The weight profiles. Read them as claims about what answers each question:
#
#   USES   — "who runs SAP?" Company technographics answer it outright, so coverage dominates and
#            recency/volume are zeroed: a company does not stop running SAP because it stopped
#            advertising. Zeroing them is also what lets the 6 of our 13 golden companies that
#            have no job postings at all rank on their merits.
#   HIRING — "who is investing in dbt?" A posting from 2019 is not a buying signal, so recency
#            carries real weight — but not more than coverage. See the note below.
#   EITHER — the user did not say. Balanced, coverage-led.
#
# STATUS OF THESE NUMBERS, per §8.6 ("every number in §8.2 and §8.5 is invented"), after running
# `scripts/eval.py --tune` against `evals/golden.yaml`:
#
#   USES    — UNCHANGED, and confirmed to sit among the optimal profiles. 118 of 565 candidate
#             profiles tie with it at the top, so the golden set agrees with the spec here
#             without being able to prove it is uniquely right.
#   HIRING  — **CHANGED, on evidence.** The spec's coverage=.30/recency=.40 lost to coverage on
#             three queries: it let a company with a fresh posting for ONE of two asked terms
#             outrank a company posting for BOTH, and on `compound-sap-uses-dbt-hiring` it pushed
#             the single correct answer to rank 5 (MRR 0.20). Recency is a tie-breaker between
#             companies that match the question, not a reason to answer a different question.
#             Composite 0.923 -> 1.000. 60 of 565 profiles tie at the top; this is the one
#             closest to the spec's hypothesis, because the eval gives no basis to move further.
#   EITHER  — UNCHANGED, and **untested**: no golden query runs in this mode. It remains a pure
#             guess. Do not cite it as tuned.
#
# All of this was measured against a MACHINE-AUTHORED golden set (see evals/golden.yaml's
# header). It demonstrates the tuning loop works; it is not evidence about real-world relevance.
WEIGHTS: dict[IntentMode, dict[str, float]] = {
    IntentMode.USES: dict(coverage=0.60, recency=0.00, volume=0.00, best_doc=0.40),
    IntentMode.HIRING: dict(coverage=0.45, recency=0.35, volume=0.10, best_doc=0.10),
    IntentMode.EITHER: dict(coverage=0.45, recency=0.20, volume=0.05, best_doc=0.30),
}

# §8.3 — `recency = exp(-days/60)`: a 60-day half-life-ish decay. 0.5 at ~6 weeks, 0.13 at a
# quarter. §8.3 — `volume = log1p(n)/log(11)`: saturates at ~10 distinct roles.
RECENCY_TAU_DAYS = 60.0
VOLUME_SATURATION = 11.0

# §8.5 — the soft industry multiplier's tiers.
#
# UNCHANGED, and **unfalsifiable on the current golden set**: all 140 swept combinations tie at
# the top. The reason is structural rather than lucky — on every industry query the correct
# companies are an exact canonical hit (multiplier 1.00) and the incorrect ones are not, so *any*
# tier values below 1.00 preserve the ordering and score identically. This set can prove the
# multiplier must exist and must be < 1.0; it cannot say whether 0.35 should be 0.2 or 0.5.
# Those two numbers need a query whose right answer is in a *neighbouring* industry — i.e. real
# labels. Left at the spec's hypothesis rather than moved on a coin flip.
INDUSTRY_NEAR_THRESHOLD = 0.82
INDUSTRY_NEAR_MULTIPLIER = 0.75
INDUSTRY_FAR_MULTIPLIER = 0.35


@dataclass(frozen=True)
class Tuning:
    """The §8.2/§8.5 knobs, bundled so `scripts/eval.py` can sweep them without editing source."""

    weights: dict[IntentMode, dict[str, float]]
    near_threshold: float = INDUSTRY_NEAR_THRESHOLD
    near_multiplier: float = INDUSTRY_NEAR_MULTIPLIER
    far_multiplier: float = INDUSTRY_FAR_MULTIPLIER

    @staticmethod
    def default() -> "Tuning":
        return Tuning(weights={mode: dict(w) for mode, w in WEIGHTS.items()})


# ---------------------------------------------------------------------------
# §8.4 — term matching
# ---------------------------------------------------------------------------

# Word-boundary-ish delimiters. `\b` is wrong here: it treats `/` and `.` as boundaries, so
# `\bsap\b` would happily match the "SAP" inside "SAP/4" — and more to the point `\bbi\b` would
# match inside "Power BI" in ways that depend on where the token sits. Explicit lookarounds over
# the alphanumeric class say exactly what is meant: a term must not be glued to another word.
_LEFT = r"(?<![a-z0-9])"
_RIGHT = r"(?![a-z0-9])"

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#./-]*")


def tokens(text: str) -> set[str]:
    """§8.4's `tokens(paraphrase)` — word-boundary tokens, lowercased.

    This is defect #2's fix in one function. The shipped system matches terms with
    `LIKE '%sap%'`, which is why it returns **Sapient** and **sapphire** for "SAP". Tokenising on
    word boundaries means "sap" is a token of "running SAP, Snowflake" and is *not* a token of
    "Sapient Consulting Group" — resemblance is not identity.
    """
    return set(_TOKEN.findall((text or "").lower()))


class TermMatcher:
    """Decides whether a company/job satisfies a query term.

    **Deviation from §8.4's snippet, deliberate — read this before changing it.**

    §8.4 checks `t.value.lower() in hay`, where `hay` is `technologies | tokens(paraphrase)`. For
    a term that names a technology, the `tokens(paraphrase)` half is a liability rather than a
    help, because our paraphrases *spell out* the technologies:

        company 46 · technologies {"SAP S/4HANA", ...}
                   · paraphrase   "... running SAP S/4HANA, Microsoft Azure, ..."

    `tokens(...)` yields `{"sap", "s/4hana", ...}`, so the literal §8.4 check reports that this
    company matches the term **SAP**. It does not: `SAP` and `SAP S/4HANA` are two *distinct*
    entries in `tech_canonical`, and in this corpus they are disjoint (26 companies carry `SAP`,
    10 carry `SAP S/4HANA`, none carry both). Letting prose overrule the controlled vocabulary
    re-introduces exactly the substring matching that rule 5 exists to prevent — it just does it
    one tokenizer downstream.

    So the rule here is: **the controlled vocabulary wins where it applies.**

      * A term that resolves against `tech_canonical` (exactly, or by alias — so "S/4 HANA",
        "SAP ECC" and "S4HANA" all land on the right entry) is matched against the canonical
        `technologies[]` array *only*, by exact equality. Stronger than a word boundary, and it
        agrees with the corpus ground truth of 26 true SAP users.
      * Any other term — "manufacturing", "predictive maintenance", a product we have never
        catalogued — has nowhere else to live, so it is matched against the prose with the
        word-boundary rule above.

    Either path defeats the planted trap: "Sapient Consulting Group" carries `Sapient Cloud
    Suite`, which is neither equal to `SAP` nor word-boundary-present in its paraphrase.
    """

    def __init__(self, vocabulary: Iterable[dict[str, Any]]) -> None:
        # key -> canonical term, over both the terms and their aliases.
        self._by_key: dict[str, str] = {}
        for row in vocabulary:
            term = row["term"]
            self._by_key.setdefault(tech_key(term), term)
            for alias in row.get("aliases") or []:
                self._by_key.setdefault(tech_key(alias), term)

    def resolve(self, value: str) -> str | None:
        """A query term -> its canonical technology, or None if it is not one."""
        return self._by_key.get(tech_key(value))

    def matches(self, value: str, *, technologies: Sequence[str], prose: str) -> bool:
        canonical = self.resolve(value)
        if canonical is not None:
            return any(canonical.lower() == t.lower() for t in technologies)
        needle = value.strip().lower()
        if not needle:
            return False
        return re.search(_LEFT + re.escape(needle) + _RIGHT, (prose or "").lower()) is not None


def _job_prose(job: dict[str, Any]) -> str:
    return job.get("paraphrase") or ""


def job_matched_terms(job: dict[str, Any], terms: Sequence[Term], matcher: TermMatcher) -> list[str]:
    """Which of the query's job-side terms this posting itself satisfies.

    Only `HIRING` and `ANY` terms are considered: a `USES` term is a claim about the company, and
    a posting is not the evidence for it. Drives both `evidence[].matched_terms` and which
    postings count towards `recency`/`volume`.
    """
    out: list[str] = []
    for term in terms:
        if term.source == TermSource.USES:
            continue
        if matcher.matches(
            term.value, technologies=job.get("technologies") or [], prose=_job_prose(job)
        ):
            out.append(term.value)
    return out


def coverage(
    *,
    company: dict[str, Any],
    jobs: Sequence[dict[str, Any]],
    terms: Sequence[Term],
    matcher: TermMatcher,
) -> tuple[float, list[str], list[str]]:
    """§8.4 — the AND-ness, as a fraction, with no cliff.

    This is defect #1's fix. The shipped system's `keywordMatchMode` defaults to `ANY` at three
    layers, so "Snowflake **and** AWS" silently means "Snowflake **or** AWS"; flipping it to
    `ALL` would only trade one cliff for the other, returning nothing when a company matches two
    of three. Coverage replaces the switch with a *ratio*: 3-of-3 scores 1.0, 1-of-3 scores 0.33,
    and both are returned, ordered. There is no mode to get wrong.

    The haystack is chosen per term by `Term.source` (§8.4) — this is what makes "companies
    **using** Snowflake" and "companies **hiring for** Snowflake" different questions rather than
    two phrasings of one.
    """
    company_tech = company.get("technologies") or []
    company_prose = company.get("paraphrase") or ""

    job_tech: list[str] = []
    job_prose_parts: list[str] = []
    for job in jobs:
        job_tech.extend(job.get("technologies") or [])
        job_prose_parts.append(_job_prose(job))
    job_prose = " ".join(job_prose_parts)

    matched: list[str] = []
    unmatched: list[str] = []
    for term in terms:
        if term.source == TermSource.USES:
            hit = matcher.matches(term.value, technologies=company_tech, prose=company_prose)
        elif term.source == TermSource.HIRING:
            hit = matcher.matches(term.value, technologies=job_tech, prose=job_prose)
        else:
            hit = matcher.matches(
                term.value,
                technologies=[*company_tech, *job_tech],
                prose=f"{company_prose} {job_prose}",
            )
        (matched if hit else unmatched).append(term.value)

    return len(matched) / max(1, len(terms)), matched, unmatched


# ---------------------------------------------------------------------------
# §8.3 — the axes
# ---------------------------------------------------------------------------


def recency(latest: dt.datetime | None, *, now: dt.datetime) -> tuple[float, int | None]:
    """§8.3 — `exp(-days_since_latest_post / 60)`. 0.0 when there are no matching postings.

    Returns the axis and the day count, because the day count is what a human reads on the card.
    """
    if latest is None:
        return 0.0, None
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=now.tzinfo)
    days = max(0, (now - latest).days)
    return math.exp(-days / RECENCY_TAU_DAYS), days


def volume(distinct_roles: int) -> float:
    """§8.3 — `log1p(distinct_roles) / log(11)`, saturating at ~10.

    **DISTINCT `title_norm`, not job rows** (§8.3, §5.6). A company that reposts one requisition
    eight times is advertising one role, and counting rows would rank it as though it were
    building a department. Distinctness is also why §5.6's `is_repost` needs no special case
    here: two rows with the same `title_norm` collapse whether or not they were flagged.
    """
    return math.log1p(distinct_roles) / math.log(VOLUME_SATURATION)


def industry_multiplier(
    *,
    industry_canonical: str | None,
    similarity: float | None,
    asked: str | None,
    tuning: Tuning,
) -> tuple[float, str]:
    """§8.5 — a soft multiplier, **never a filter**. Returns (multiplier, which rule fired).

    Rule 2's worked example. `industry` is free text: the company says "Industrial Machinery",
    the user says "manufacturing". Hard-filtering that string deletes the correct answer before
    ranking can rescue it, and the user sees an empty page with no way to tell that the data was
    there. So the wrong industry costs a company 65% of its score and it stays on the list, where
    a human can see it and judge.

    The tiers: an exact canonical hit is 1.00; a close-but-not-equal industry (cosine > 0.82
    between the two industry strings' embeddings) keeps 0.75; anything else is down-weighted to
    0.35 — **and still returned**.
    """
    if not asked:
        return 1.00, "no industry asked"
    if industry_canonical and industry_canonical.strip().lower() == asked.strip().lower():
        return 1.00, f"canonical hit: {industry_canonical}"
    if similarity is not None and similarity > tuning.near_threshold:
        return tuning.near_multiplier, f"near match: cosine {similarity:.3f} > {tuning.near_threshold}"
    detail = f"cosine {similarity:.3f}" if similarity is not None else "no industry embedding"
    return tuning.far_multiplier, f"down-weighted, not dropped ({detail})"


# ---------------------------------------------------------------------------
# §8.6 — the final score
# ---------------------------------------------------------------------------


def rank(
    *,
    retrieval: Retrieval,
    chips: Chips,
    matcher: TermMatcher,
    asked_industry: str | None,
    now: dt.datetime,
    limit: int = 20,
    tuning: Tuning | None = None,
) -> list[CompanyResult]:
    """§6[5]-[6] and §8.6: score every candidate, attach evidence, sort, cut to `limit`.

    The cut is the *only* place a company is removed for not being good enough, and it happens
    after ranking, at the caller's requested depth — never before it (rule 2).
    """
    tuning = tuning or Tuning.default()
    weights = tuning.weights[chips.intent_mode]

    results: list[CompanyResult] = []
    for cid in retrieval.candidates:
        detail = retrieval.details.get(cid)
        if detail is None:
            # A company_signal row vanished between the list query and the detail query. It has
            # no paraphrase and therefore nothing to show or explain; skipping is honest.
            continue

        jobs = retrieval.company_jobs.get(cid, [])

        cov, matched, unmatched = coverage(
            company=detail, jobs=jobs, terms=chips.terms, matcher=matcher
        )

        # Which postings count as "matching" for recency/volume/evidence. §8.3 says
        # "distinct_matching_roles", and a posting matches when it satisfies a job-side term.
        # With no job-side terms to test (e.g. a pure USES query) every surviving posting counts
        # — there is nothing for it to fail.
        job_side_terms = [t for t in chips.terms if t.source != TermSource.USES]
        per_job_matches: dict[int, list[str]] = {}
        matching_jobs: list[dict[str, Any]] = []
        for job in jobs:
            hits = job_matched_terms(job, job_side_terms, matcher) if job_side_terms else []
            per_job_matches[job["job_id"]] = hits
            if not job_side_terms or hits:
                matching_jobs.append(job)

        dates = [j["posted_date"] for j in matching_jobs if j.get("posted_date")]
        rec, days_ago = recency(max(dates) if dates else None, now=now)
        distinct_roles = len({(j.get("title_norm") or "").strip() for j in matching_jobs if j.get("title_norm")})
        vol = volume(distinct_roles)
        best = retrieval.best_doc.get(cid, 0.0)

        subtotal = (
            weights["coverage"] * cov
            + weights["recency"] * rec
            + weights["volume"] * vol
            + weights["best_doc"] * best
        )
        multiplier, rule = industry_multiplier(
            industry_canonical=detail.get("industry_canonical"),
            similarity=detail.get("industry_similarity"),
            asked=asked_industry,
            tuning=tuning,
        )
        score = multiplier * subtotal

        # §6[6]: top 3 evidence jobs. Freshest first — the recent posting is the reason to call.
        evidence_pool = matching_jobs or jobs
        top_jobs = sorted(
            evidence_pool,
            key=lambda j: (
                j["posted_date"] is not None,
                j["posted_date"] or dt.datetime.min,
                -j["job_id"],
            ),
            reverse=True,
        )[:3]

        evidence = []
        for job in top_jobs:
            _, job_days = recency(job.get("posted_date"), now=now)
            evidence.append(
                Evidence(
                    job_id=job["job_id"],
                    title=job.get("title"),
                    posted_date=job.get("posted_date"),
                    paraphrase=job.get("paraphrase") or "",
                    matched_terms=per_job_matches.get(job["job_id"], []),
                    technologies=job.get("technologies") or [],
                    function=job.get("function"),
                    seniority=job.get("seniority"),
                    initiative=job.get("initiative"),
                    days_ago=job_days,
                )
            )

        results.append(
            CompanyResult(
                company_id=cid,
                name=detail.get("name"),
                domain=detail.get("domain"),
                industry_raw=detail.get("industry_raw"),
                industry_canonical=detail.get("industry_canonical"),
                technologies=detail.get("technologies") or [],
                paraphrase=detail.get("paraphrase") or "",
                score=score,
                breakdown=Breakdown(
                    coverage=cov,
                    recency=rec,
                    volume=vol,
                    best_doc=best,
                    weights=dict(weights),
                    weighted_subtotal=subtotal,
                    industry_multiplier=multiplier,
                    intent_mode=chips.intent_mode,
                    matched_terms=matched,
                    unmatched_terms=unmatched,
                    matched_count=len(matched),
                    asked_count=len(chips.terms),
                    distinct_roles=distinct_roles,
                    days_since_latest_post=days_ago,
                    rrf_score=retrieval.fused.get(cid, 0.0),
                    industry_similarity=(
                        float(detail["industry_similarity"])
                        if detail.get("industry_similarity") is not None
                        else None
                    ),
                    industry_rule=rule,
                ),
                evidence=evidence,
            )
        )

    # Ties on score are common (coverage is a small set of fractions), so break them on
    # company_id: identical chips must produce a byte-identical ranking on every run.
    results.sort(key=lambda r: (-r.score, r.company_id))
    return results[:limit]
