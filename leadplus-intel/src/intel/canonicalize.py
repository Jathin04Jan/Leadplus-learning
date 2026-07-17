"""Canonicalisation — companies (§5.4), technologies (§5.2 stage 3), industries (§5.5).

Design rule 5: *enums where closed, canonicalise where long-tail.* Free-texting technologies gives
you `SAP S/4HANA`, `S/4 HANA`, `SAP S4`, `S4/HANA` as four different things — and once they are
four different things, the only way to match them is substring matching, which is defect #2 of the
system this project exists to replace. Everything in this file is the alternative to that.

Nothing here auto-guesses. When a term cannot be resolved with evidence it goes to
`tech_review_queue` for a human. `Sapient Cloud Suite` is the live test of that: it *looks* like
SAP and is not, and no stage in this file is permitted to decide otherwise on resemblance.

**What this file deliberately does NOT canonicalise: intent phrases (§5.8).** Rule 5 is "enums
where closed, canonicalise where LONG-TAIL" — it is not "canonicalise everything". A technology is
a named product with an official form; an intent (`icd-10 coding accuracy`, `embedded electronics
design`) is a description of work and has none. Applying this ladder to intents was tried and
measured: 5,209 distinct phrases in 8,114 rows, 3.9% resolved, nearest-match cosines 0.32-0.52. It
was removed. Intents are matched semantically via `job_intent.intent_embedding`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import psycopg

from . import config, embed, repository
from .models import CanonicalCompany

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# §5.4 — canonical companies
# ---------------------------------------------------------------------------


@dataclass
class FoldResult:
    """What the fold did, so `bootstrap_canonical.py` can report rather than assume (§5.4)."""

    active_companies: int
    canonical_companies: int
    groups_with_multiple_members: int
    without_domain: int
    shared_rows: int
    largest_groups: list[tuple[str, int]] = field(default_factory=list)

    @property
    def collapsed(self) -> int:
        return self.active_companies - self.canonical_companies

    @property
    def is_noop(self) -> bool:
        return self.collapsed == 0


def fold_companies(conn: psycopg.Connection) -> tuple[list[CanonicalCompany], FoldResult]:
    """Fold `lead_company` by `lower(domain)`, preferring the shared row, else lowest id (§5.4).

    LeadPlus uses copy-on-write: a shared row (`tenant_id IS NULL`) plus a per-tenant copy for
    every tenant that touched the company — same domain, different id. Ingesting naively indexes
    the same real company two or three times and returns it two or three times.

    Companies with no domain are their own canonical row: with no domain there is no evidence two
    rows are the same company, and merging on name would be a guess.
    """
    rows = repository.fetch_companies_for_fold(conn)

    groups: dict[str, list[dict]] = {}
    solo: list[dict] = []
    for row in rows:
        key = row["domain_key"]
        if not key:
            solo.append(row)
        else:
            groups.setdefault(key, []).append(row)

    canonicals: list[CanonicalCompany] = []
    multi = 0

    for key, members in groups.items():
        # Prefer the shared row (tenant_id IS NULL); tie-break on lowest id.
        chosen = min(members, key=lambda m: (m["tenant_id"] is not None, m["id"]))
        member_ids = sorted(m["id"] for m in members)
        if len(member_ids) > 1:
            multi += 1
        canonicals.append(
            CanonicalCompany(
                canonical_id=chosen["id"], domain=key, member_ids=member_ids
            )
        )

    for row in solo:
        canonicals.append(
            CanonicalCompany(canonical_id=row["id"], domain=None, member_ids=[row["id"]])
        )

    canonicals.sort(key=lambda c: c.canonical_id)

    largest = sorted(
        ((c.domain or "(no domain)", len(c.member_ids)) for c in canonicals),
        key=lambda kv: -kv[1],
    )[:5]

    result = FoldResult(
        active_companies=len(rows),
        canonical_companies=len(canonicals),
        groups_with_multiple_members=multi,
        without_domain=len(solo),
        shared_rows=sum(1 for r in rows if r["tenant_id"] is None),
        largest_groups=largest,
    )
    return canonicals, result


# ---------------------------------------------------------------------------
# §5.2 stage 3 — technology canonicalisation
# ---------------------------------------------------------------------------


def tech_key(term: str) -> str:
    """The match key: lowercased, punctuation-stripped (§5.2 stage 3).

    This folds `SAP S/4HANA`, `SAP S/4 HANA`, `S/4HANA` -> `saps4hana`, `s4hana`. It does NOT
    fold `SAP` into `SAP S/4HANA` (`sap` != `saps4hana`) and it does NOT fold `Sapient Cloud
    Suite` into either (`sapientcloudsuite`). Punctuation is noise; letters are not.
    """
    return re.sub(r"[^a-z0-9]", "", (term or "").lower())


def location_key(value: str) -> str:
    """The `location_alias.alias` key: lowercased, whitespace-collapsed (CHANGES-v2 §3.1).

    Deliberately NOT `tech_key`. Stripping punctuation there is right because `S/4HANA` and
    `S4HANA` are the same product; here it would fold `st. louis` and `stlouis` together but also
    collapse the space in `new york`, and the canonical value has to survive a round trip to
    `lower(lead_company.hq_city)` — which really does contain `St. Louis`, space and period
    included. So the key keeps the shape of the name, and the *aliases* carry the variants
    (`st louis` -> `st. louis`), seeded by scripts/bootstrap_locations.py.
    """
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def industry_key(value: str) -> str:
    """The `industry_alias.alias` key: lowercased, whitespace-collapsed, punctuation kept.

    Deliberately `location_key`'s rule and NOT `tech_key`'s. Stripping punctuation is right for
    products (`S/4HANA` and `S4HANA` are one thing) and wrong here: the taxonomy contains
    `Transportation, Logistics, Supply Chain and Storage` and `Glass, Ceramics and Concrete
    Manufacturing`, whose commas carry the sense, and `tech_key` would also collapse the space in
    `supply chain` — turning two words a user types into one token nothing matches.

    Note `retrieve.canonical_industries` still resolves the *chip* against the §5.5 vocabulary with
    `tech_key`, and that is a different job: matching one spelling of one value. This key is for
    the alias table, where a phrase has to survive a round trip to `lead_company.industry`.
    """
    return re.sub(r"\s+", " ", (value or "").strip().lower())


@dataclass
class TechResolution:
    raw_term: str
    canonical: str | None
    method: str  # exact | alias | embedding | unresolved
    similarity: float | None = None
    nearest: str | None = None


class TechCanonicalizer:
    """The §5.2 stage-3 ladder: exact -> alias -> embedding NN >0.85 -> review queue.

    The technology vocabulary, bootstrapped from Apollo's curated `lead_company.technologies[]`.

    **This ladder is for TECHNOLOGIES ONLY, and that is rule 5 read correctly.** It was once a
    generic base class with a second subclass for intent phrases; that was a category error and
    the class hierarchy was its main symptom. A technology is a named product — `SAP S/4HANA` /
    `S/4 HANA` / `SAP S4` are one thing, so snapping them to an official form is right and the
    ladder is load-bearing (`ROS` -> `ROSS` @ 0.79 is a real trap it catches). An intent is a
    descriptive phrase — `icd-10 coding accuracy` — with no official form to snap to, measured at
    5,209 distinct phrases in 8,114 rows and 3.9% resolution. Intents are matched semantically via
    `job_intent.intent_embedding`. Do not re-generalise this class to reach them.

    Per-run memo cache: the corpus repeats the same handful of terms hundreds of times, and each
    distinct term must be embedded and resolved only once.

    **Caveat on `occurrences`, because the column's name over-promises:** the cache means a
    repeated term short-circuits before `_enqueue_review`, so `occurrences` counts *first
    sightings*, not corpus frequency — it is ~1 for nearly everything and cannot be used to
    prioritise the review queue by weight. It is left as-is rather than "fixed" by re-enqueuing on
    every hit: a counter that has to be maintained is worse than a GROUP BY over the data itself,
    and `tech_review_queue` is small enough (875) to read whole.
    """

    queue_name = "tech_review_queue"

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self._cache: dict[str, TechResolution] = {}

    # --- the vocabulary calls ---------------------------------------------------------------
    def _key(self, term: str) -> str:
        return tech_key(term)

    def _find_exact(self, key: str) -> str | None:
        return repository.find_tech_exact(self.conn, key)

    def _find_alias(self, key: str) -> str | None:
        return repository.find_tech_alias(self.conn, key)

    def _find_nearest(self, vector: list[float]) -> tuple[str, float] | None:
        return repository.find_tech_nearest(self.conn, vector)

    def _add_alias(self, term: str, alias: str) -> None:
        repository.add_tech_alias(self.conn, term, alias)

    def _enqueue_review(self, *, raw_term: str, nearest: str | None, similarity: float | None) -> None:
        repository.enqueue_tech_review(
            self.conn, raw_term=raw_term, nearest=nearest, similarity=similarity
        )

    # --- the ladder ------------------------------------------------------------------------
    async def resolve_many(self, terms: list[str]) -> dict[str, TechResolution]:
        """Resolve a list of raw terms. Returns raw_term -> resolution (canonical may be None)."""
        unique = list(dict.fromkeys(t.strip() for t in terms if t and t.strip()))
        todo: list[str] = []

        for term in unique:
            key = self._key(term)
            if not key:
                continue
            if key in self._cache:
                continue
            hit = self._find_exact(key)
            if hit:
                self._cache[key] = TechResolution(term, hit, "exact", 1.0, hit)
                continue
            hit = self._find_alias(key)
            if hit:
                self._cache[key] = TechResolution(term, hit, "alias", 1.0, hit)
                continue
            todo.append(term)

        if todo:
            vectors = await embed.embed_texts(todo)
            for term, vector in zip(todo, vectors):
                key = self._key(term)
                nearest = self._find_nearest(vector)
                if nearest and nearest[1] > config.TECH_NN_THRESHOLD:
                    canonical, similarity = nearest
                    # "hit + record alias" (§5.2 stage 3) — the next run takes the cheap path.
                    self._add_alias(canonical, term)
                    self._cache[key] = TechResolution(term, canonical, "embedding", similarity, canonical)
                else:
                    # Below threshold: a human resolves this. We NEVER auto-guess.
                    nearest_term = nearest[0] if nearest else None
                    similarity = nearest[1] if nearest else None
                    self._enqueue_review(
                        raw_term=term, nearest=nearest_term, similarity=similarity
                    )
                    self._cache[key] = TechResolution(
                        term, None, "unresolved", similarity, nearest_term
                    )

        out: dict[str, TechResolution] = {}
        for term in unique:
            key = self._key(term)
            if key in self._cache:
                resolution = self._cache[key]
                out[term] = TechResolution(
                    raw_term=term,
                    canonical=resolution.canonical,
                    method=resolution.method,
                    similarity=resolution.similarity,
                    nearest=resolution.nearest,
                )
        return out

    async def canonical_list(self, terms: list[str]) -> list[str]:
        """Map raw terms -> deduped canonical terms. Unresolved terms are dropped, not guessed.

        Dropping is deliberate: an unresolved term is already recorded in the review queue.
        Writing the raw term into `technologies` would put an uncontrolled value into a
        controlled vocabulary — which is the exact failure rule 5 forbids.
        """
        resolved = await self.resolve_many(terms)
        out: list[str] = []
        for term in terms:
            resolution = resolved.get(term.strip())
            if resolution and resolution.canonical and resolution.canonical not in out:
                out.append(resolution.canonical)
        return out


# ---------------------------------------------------------------------------
# §5.5 — industry canonicalisation
# ---------------------------------------------------------------------------

# §8.5 compares cosine(emb(company.industry_raw), emb(asked_industry)) > 0.82. Use the same
# number here so the ingest-time mapping and the query-time multiplier agree about what "close"
# means. Below it we leave industry_canonical NULL rather than force a wrong bucket — §8.5
# already handles an unmapped industry gracefully via the embedding comparison.
INDUSTRY_THRESHOLD = 0.82


@dataclass
class IndustryResolution:
    raw: str | None
    canonical: str | None
    method: str  # exact | embedding | unresolved | empty
    similarity: float | None = None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    # text-embedding-3-large returns unit-norm vectors, so the dot product IS the cosine.
    return dot


class IndustryCanonicalizer:
    """§5.5: map free-text `industry` onto `lead_query WHERE type='COMPANY_INDUSTRY'`.

    Reuses the vocabulary the Java system already injects as `{{INDUSTRY_LIST}}`. We do not
    invent a second taxonomy — two taxonomies for one concept is how the two systems would
    silently disagree about what "Manufacturing" means.
    """

    def __init__(self, vocabulary: list[str], vectors: list[list[float]]) -> None:
        self.vocabulary = vocabulary
        self.vectors = vectors
        self._by_key = {tech_key(v): v for v in vocabulary}

    @classmethod
    async def build(cls, conn: psycopg.Connection) -> "IndustryCanonicalizer":
        vocabulary = repository.fetch_industry_vocabulary(conn)
        if not vocabulary:
            raise RuntimeError(
                "lead_query has no COMPANY_INDUSTRY rows — §5.5 requires this vocabulary"
            )
        vectors = await embed.embed_texts(vocabulary)
        return cls(vocabulary, vectors)

    def resolve(self, raw: str | None, raw_vector: list[float] | None) -> IndustryResolution:
        if not raw or not raw.strip():
            return IndustryResolution(raw, None, "empty")

        key = tech_key(raw)
        if key in self._by_key:
            return IndustryResolution(raw, self._by_key[key], "exact", 1.0)

        if raw_vector is None:
            return IndustryResolution(raw, None, "unresolved")

        scored = [
            (value, _cosine(raw_vector, vector))
            for value, vector in zip(self.vocabulary, self.vectors)
        ]
        best, similarity = max(scored, key=lambda kv: kv[1])
        if similarity > INDUSTRY_THRESHOLD:
            return IndustryResolution(raw, best, "embedding", similarity)
        return IndustryResolution(raw, None, "unresolved", similarity)
