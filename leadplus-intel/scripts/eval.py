"""Phase 10 — the eval (ARCHITECTURE.md §14).

    .venv/bin/python scripts/eval.py            # measure /api/search/structured (server must be up)
    .venv/bin/python scripts/eval.py --tune     # sweep §8.2 weights + §8.5 tiers, in-process

**This is the gate the existing system never had.** 560 unit tests pass over there while search
returns garbage, because not one of them asserts anything about *result quality*. A test that
says `search()` returned 200 OK is not a test that it returned the right companies.

Two things it measures, and one it cannot:

  * `--measure` (default) posts each golden query's **chips** to `/api/search/structured`, never
    `/api/search`. §14 is explicit about why: the LLM parse is the one nondeterministic step, and
    letting it into the measurement means a regression in ranking and a bad day for the parser
    look identical.
  * `--tune` sweeps the invented numbers in §8.2/§8.5 against the same set. Retrieval is run once
    per query and re-scored for every candidate profile — the weights do not affect retrieval, so
    re-retrieving would be a slow way to get the same lists back.
  * It **cannot** tell you the ranking is good. See the header of `evals/golden.yaml`: the labels
    are machine-authored from the corpus, so this measures regressions, not relevance.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  — puts src/ on the path

import argparse
import asyncio
import datetime as dt
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx
import yaml

from intel import repository, retrieve, score
from intel.models import Chips, IntentMode

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "evals" / "golden.yaml"

P_AT = 10
R_AT = 50


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def precision_at(ranked: Sequence[int], relevant: set[int], k: int = P_AT) -> float:
    return len([c for c in ranked[:k] if c in relevant]) / k


def precision_ceiling(relevant: set[int], k: int = P_AT) -> float:
    """The best precision@k this query can possibly score.

    A query with one correct answer caps at 0.1, however perfect the ranking. Reporting a bare
    mean precision@10 across queries of different sizes would therefore mostly measure how many
    labels each query has. The ceiling makes the number readable, and `p@10/ceiling` is what the
    tuner optimises.
    """
    return min(len(relevant), k) / k


def recall_at(ranked: Sequence[int], relevant: set[int], k: int = R_AT) -> float:
    if not relevant:
        return 0.0
    return len([c for c in ranked[:k] if c in relevant]) / len(relevant)


def recall_ceiling(relevant: set[int], k: int = R_AT) -> float:
    """The best recall@k this query can possibly score.

    `industry-manufacturing-aws` has 95 correct answers and the endpoint returns 50, so its
    recall@50 caps at 0.526 — a *perfect* ranking scores 0.526 there. Without this the tuner
    would chase an unreachable number on that query and trade away real precision elsewhere to
    do it.
    """
    if not relevant:
        return 0.0
    return min(len(relevant), k) / len(relevant)


def mrr(ranked: Sequence[int], relevant: set[int]) -> float:
    for i, cid in enumerate(ranked):
        if cid in relevant:
            return 1.0 / (i + 1)
    return 0.0


@dataclass
class Row:
    id: str
    p_at_10: float
    ceiling: float
    recall_at_50: float
    recall_ceil: float
    mrr: float
    violations: list[int]
    discriminating: bool

    @property
    def normalized_p(self) -> float:
        return self.p_at_10 / self.ceiling if self.ceiling else 0.0

    @property
    def normalized_r(self) -> float:
        return self.recall_at_50 / self.recall_ceil if self.recall_ceil else 0.0

    @property
    def composite(self) -> float:
        """The tuner's objective: each component scaled against what is actually reachable.

        Both precision@10 and recall@50 have query-dependent ceilings on this set (a query with
        one correct answer caps p@10 at 0.1; a query with 95 caps r@50 at 0.526). Optimising the
        raw numbers would mostly optimise the label counts.
        """
        return (self.normalized_p + self.normalized_r + self.mrr) / 3


def evaluate(query: dict[str, Any], ranked: Sequence[int]) -> Row:
    relevant = set(query["relevant"])
    forbidden = set(query.get("forbidden") or [])
    depth = query.get("forbidden_in_top", len(ranked))
    return Row(
        id=query["id"],
        p_at_10=precision_at(ranked, relevant),
        ceiling=precision_ceiling(relevant),
        recall_at_50=recall_at(ranked, relevant),
        recall_ceil=recall_ceiling(relevant),
        mrr=mrr(ranked, relevant),
        violations=[c for c in ranked[:depth] if c in forbidden],
        discriminating=bool(query.get("discriminating")),
    )


def report(rows: Sequence[Row], *, title: str) -> dict[str, float]:
    print(f"\n{title}")
    print("=" * 96)
    print(
        f"{'query':<38} {'p@10':>6} {'ceil':>6} {'r@50':>6} {'ceil':>6} {'MRR':>6} {'gate':>8}"
    )
    print("-" * 96)
    for row in rows:
        gate = f"FAIL {row.violations}" if row.violations else "ok"
        star = " *" if row.discriminating else "  "
        print(
            f"{row.id + star:<38} {row.p_at_10:>6.3f} {row.ceiling:>6.2f} "
            f"{row.recall_at_50:>6.3f} {row.recall_ceil:>6.2f} {row.mrr:>6.3f} {gate:>8}"
        )
    print("-" * 96)

    def mean(values: Iterable[float]) -> float:
        values = list(values)
        return sum(values) / len(values) if values else 0.0

    means = {
        "precision@10": mean(r.p_at_10 for r in rows),
        "precision@10_normalized": mean(r.normalized_p for r in rows),
        "recall@50": mean(r.recall_at_50 for r in rows),
        "recall@50_normalized": mean(r.normalized_r for r in rows),
        "mrr": mean(r.mrr for r in rows),
        "composite": mean(r.composite for r in rows),
    }
    discriminating = [r for r in rows if r.discriminating]
    print(
        f"{'MEAN (all ' + str(len(rows)) + ')':<38} {means['precision@10']:>6.3f} "
        f"{'':>6} {means['recall@50']:>6.3f} {'':>6} {means['mrr']:>6.3f}"
    )
    if discriminating:
        print(
            f"{'MEAN (* discriminating ' + str(len(discriminating)) + ')':<38} "
            f"{mean(r.p_at_10 for r in discriminating):>6.3f} {'':>6} "
            f"{mean(r.recall_at_50 for r in discriminating):>6.3f} {'':>6} "
            f"{mean(r.mrr for r in discriminating):>6.3f}"
        )
        means["composite_discriminating"] = mean(r.composite for r in discriminating)
    violations = sum(len(r.violations) for r in rows)
    print(
        f"\nnormalized by reachable ceiling — precision@10: "
        f"{means['precision@10_normalized']:.3f}   recall@50: "
        f"{means['recall@50_normalized']:.3f}   composite: {means['composite']:.3f}"
    )
    print(f"forbidden-company gate: {'PASS' if violations == 0 else f'FAIL ({violations})'}")
    print(
        "\n* = the rule is not a restatement of a single scoring axis; only these rows can "
        "falsify a weight.\n  Unstarred rows are near-tautological regression tests — see the "
        "header of evals/golden.yaml."
    )
    return means


# ---------------------------------------------------------------------------
# Measure — over HTTP, against the real endpoint
# ---------------------------------------------------------------------------


def measure(base_url: str, queries: list[dict[str, Any]]) -> list[Row]:
    rows: list[Row] = []
    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        for query in queries:
            body = {**query["chips"], "limit": R_AT}
            response = client.post("/api/search/structured", json=body)
            response.raise_for_status()
            ranked = [c["company_id"] for c in response.json()["companies"]]
            rows.append(evaluate(query, ranked))
    return rows


# ---------------------------------------------------------------------------
# Tune — in-process, so retrieval is paid for once per query
# ---------------------------------------------------------------------------


@dataclass
class Prepared:
    query: dict[str, Any]
    chips: Chips
    retrieval: Any
    matcher: Any
    asked_industry: str | None


async def prepare_all(queries: list[dict[str, Any]]) -> list[Prepared]:
    out: list[Prepared] = []
    with repository.connect() as conn:
        matcher = score.TermMatcher(repository.fetch_known_technologies(conn))
        for query in queries:
            chips = Chips.model_validate(query["chips"])
            chips = chips.model_copy(
                update={"industry": retrieve.canonical_industry(conn, chips.industry)}
            )
            vectors = await retrieve.prepare(chips)
            retrieval = retrieve.retrieve(conn, chips, vectors)
            out.append(
                Prepared(
                    query=query,
                    chips=chips,
                    retrieval=retrieval,
                    matcher=matcher,
                    asked_industry=chips.industry,
                )
            )
            print(f"  retrieved {query['id']}: {retrieval.list_sizes['candidates']} candidates")
    return out


def score_rows(prepared: list[Prepared], tuning: score.Tuning, now: dt.datetime) -> list[Row]:
    rows: list[Row] = []
    for item in prepared:
        results = score.rank(
            retrieval=item.retrieval,
            chips=item.chips,
            matcher=item.matcher,
            asked_industry=item.asked_industry,
            now=now,
            limit=R_AT,
            tuning=tuning,
        )
        rows.append(evaluate(item.query, [r.company_id for r in results]))
    return rows


def composite(rows: Sequence[Row]) -> float:
    return sum(r.composite for r in rows) / len(rows) if rows else 0.0


def weight_grid() -> list[dict[str, float]]:
    """Candidate §8.2 profiles. Coarse on purpose — the golden set cannot support fine tuning."""
    grid: list[dict[str, float]] = []
    step = 0.05
    for coverage in [round(x * step, 2) for x in range(4, 17)]:  # 0.20 .. 0.80
        for recency in [round(x * step, 2) for x in range(0, 13)]:  # 0.00 .. 0.60
            for volume in [round(x * step, 2) for x in range(0, 5)]:  # 0.00 .. 0.20
                best_doc = round(1.0 - coverage - recency - volume, 2)
                if best_doc < 0.0 or best_doc > 0.80:
                    continue
                grid.append(
                    dict(coverage=coverage, recency=recency, volume=volume, best_doc=best_doc)
                )
    return grid


def industry_grid() -> list[tuple[float, float, float]]:
    return [
        (near_t, near_m, far_m)
        for near_t in (0.74, 0.78, 0.82, 0.86, 0.90)
        for near_m in (0.50, 0.60, 0.75, 0.90, 1.00)
        for far_m in (0.05, 0.10, 0.20, 0.35, 0.50, 0.75)
        if far_m <= near_m
    ]


def _l1(profile: dict[str, float], reference: dict[str, float]) -> float:
    return sum(abs(profile[k] - reference[k]) for k in reference)


def _pick(winners: list[Any], distance: Any) -> Any:
    """Choose among equally-scoring candidates: the one closest to the spec's hypothesis.

    This matters more than it looks. Dozens of profiles tie at the top on this corpus, because a
    machine-authored golden set derived from the corpus's own structure simply cannot tell them
    apart. Taking whichever the grid happened to visit first would dress an arbitrary choice up
    as a tuned result, and would silently rewrite §8.2's numbers on no evidence at all.

    So: move a number only as far as the measurement actually forces it. Everything the eval is
    indifferent to stays where the spec put it, where the next person can see it was a
    hypothesis rather than a finding.
    """
    return min(winners, key=distance)


async def tune(queries: list[dict[str, Any]]) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    print("retrieving once per query (weights do not affect retrieval)…")
    prepared = await prepare_all(queries)

    base = score.Tuning.default()
    before = score_rows(prepared, base, now)
    report(before, title="BEFORE — the invented numbers from §8.2 / §8.5")

    # ---- §8.5 first: the multiplier is independent of the weight profile.
    industry_items = [p for p in prepared if p.asked_industry]
    spec_tiers = (base.near_threshold, base.near_multiplier, base.far_multiplier)
    baseline = composite(score_rows(industry_items, base, now))
    print(f"\nsweeping §8.5 tiers over {len(industry_items)} industry queries "
          f"({len(industry_grid())} combos), baseline composite {baseline:.4f}…")
    scored_tiers = [
        (
            composite(
                score_rows(
                    industry_items,
                    score.Tuning(
                        weights=base.weights,
                        near_threshold=near_t,
                        near_multiplier=near_m,
                        far_multiplier=far_m,
                    ),
                    now,
                )
            ),
            (near_t, near_m, far_m),
        )
        for near_t, near_m, far_m in industry_grid()
    ]
    top = max(value for value, _ in scored_tiers)
    tier_winners = [tiers for value, tiers in scored_tiers if value >= top - 1e-9]
    best_tiers = _pick(
        tier_winners, lambda t: (sum(abs(a - b) for a, b in zip(t, spec_tiers)), t)
    )
    print(f"  best tiers: threshold={best_tiers[0]} near={best_tiers[1]} far={best_tiers[2]} "
          f"-> composite {top:.4f} (+{top - baseline:.4f});  "
          f"{len(tier_winners)}/{len(scored_tiers)} combos tie at this value"
          f"{' — kept the spec values' if best_tiers == spec_tiers else ''}")

    # ---- §8.2 next, per mode, using only the queries that actually run in that mode.
    tuned_weights = {mode: dict(w) for mode, w in base.weights.items()}
    grid = weight_grid()
    for mode in IntentMode:
        items = [p for p in prepared if p.chips.intent_mode == mode]
        if not items:
            print(f"\n§8.2 {mode.value}: no golden query uses this mode — left at the spec's values.")
            continue
        def tuning_for(profile: dict[str, float]) -> score.Tuning:
            return score.Tuning(
                weights={**tuned_weights, mode: profile},
                near_threshold=best_tiers[0],
                near_multiplier=best_tiers[1],
                far_multiplier=best_tiers[2],
            )

        spec_profile = dict(base.weights[mode])
        baseline = composite(score_rows(items, tuning_for(spec_profile), now))
        print(f"\nsweeping §8.2 {mode.value} over {len(items)} queries "
              f"({len(grid)} profiles), baseline composite {baseline:.4f}…")

        scored = [(composite(score_rows(items, tuning_for(p), now)), p) for p in grid]
        top = max(value for value, _ in scored)
        winners = [p for value, p in scored if value >= top - 1e-9]
        best_profile = _pick(
            winners, lambda p: (_l1(p, spec_profile), tuple(sorted(p.items())))
        )
        tuned_weights[mode] = best_profile
        kept = " — kept the spec values" if best_profile == spec_profile else ""
        print(f"  best: {best_profile} -> composite {top:.4f} (+{top - baseline:.4f});  "
              f"{len(winners)}/{len(grid)} profiles tie at this value; "
              f"took the one nearest the spec's hypothesis (L1 "
              f"{_l1(best_profile, spec_profile):.2f}){kept}")

    final = score.Tuning(
        weights=tuned_weights,
        near_threshold=best_tiers[0],
        near_multiplier=best_tiers[1],
        far_multiplier=best_tiers[2],
    )
    after = score_rows(prepared, final, now)
    report(after, title="AFTER — swept against the golden set")

    print("\n" + "=" * 96)
    print("TUNED CONSTANTS — paste into src/intel/score.py only if the deltas above justify it:")
    print("=" * 96)
    print("WEIGHTS = {")
    for mode, profile in tuned_weights.items():
        print(
            f"    IntentMode.{mode.value}: dict(coverage={profile['coverage']:.2f}, "
            f"recency={profile['recency']:.2f}, volume={profile['volume']:.2f}, "
            f"best_doc={profile['best_doc']:.2f}),"
        )
    print("}")
    print(f"INDUSTRY_NEAR_THRESHOLD  = {best_tiers[0]}")
    print(f"INDUSTRY_NEAR_MULTIPLIER = {best_tiers[1]}")
    print(f"INDUSTRY_FAR_MULTIPLIER  = {best_tiers[2]}")


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000", help="base URL of the API")
    parser.add_argument("--tune", action="store_true", help="sweep §8.2/§8.5 in-process")
    parser.add_argument("--only", help="run a single query id")
    args = parser.parse_args()

    queries = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))["queries"]
    if args.only:
        queries = [q for q in queries if q["id"] == args.only]
        if not queries:
            print(f"no such query id: {args.only}")
            return 2

    if args.tune:
        asyncio.run(tune(queries))
        return 0

    rows = measure(args.url, queries)
    report(rows, title=f"MEASURED — POST {args.url}/api/search/structured (no LLM in this path)")
    failed = sum(len(r.violations) for r in rows)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
