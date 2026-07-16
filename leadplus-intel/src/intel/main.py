"""FastAPI app — the API (ARCHITECTURE.md §10) and the UI (§13 phase 8).

The two search endpoints share one deterministic core and differ by exactly one step:

    POST /api/search             q -> [LLM parse] -> Chips -> core
    POST /api/search/structured                      Chips -> core     <- evals use this

That is rule 1 drawn as a diagram. The LLM is at the edge, converting a sentence into chips; the
core that retrieves, fuses, scores and orders is arithmetic. `/api/search/structured` is not a
test hook bolted on for convenience — it is the same code path with the nondeterministic step
removed, which is what makes an eval number mean something (§14).

Run: `.venv/bin/uvicorn intel.main:app --app-dir src --port 8000`
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import parse, repository, retrieve, score
from .models import Chips, Function, IntentMode, SearchResponse, Seniority, Term, TermSource

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent

app = FastAPI(title="LeadPlus Intent Search", version="0.2.0")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


class SearchRequest(BaseModel):
    q: str
    limit: int = 20


class StructuredRequest(Chips):
    """§10's `/api/search/structured` body: the chips themselves, plus a result depth."""

    limit: int = 20


# ---------------------------------------------------------------------------
# The core, shared by both endpoints and by the UI
# ---------------------------------------------------------------------------


class Timer:
    """Wall-clock per stage, so §6's '<100ms excluding the LLM parse' is a measurement.

    Every stage gets its own mark, including the dull ones (`connect_ms`, `industry_ms`). Folding
    them into a neighbour would let a database round-trip be reported as embedding time, and a
    latency budget you cannot attribute is a latency budget you cannot defend.
    """

    def __init__(self) -> None:
        self.marks: dict[str, float] = {}
        self._t0 = time.perf_counter()
        self._last = self._t0

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.marks[name] = round((now - self._last) * 1000, 2)
        self._last = now

    def total(self) -> dict[str, float]:
        return {**self.marks, "total": round((time.perf_counter() - self._t0) * 1000, 2)}


async def run_search(
    conn: psycopg.Connection,
    chips: Chips,
    *,
    limit: int,
    timer: Timer,
    notes: list[str] | None = None,
) -> SearchResponse:
    """§6 [2]-[6]. Given chips, everything from here is deterministic.

    `now` is pinned once for the whole request: `recency` reads a clock, and a clock that ticks
    mid-request would make one company's decay measured against a different instant than the
    next one's.
    """
    now = dt.datetime.now(dt.timezone.utc)

    # §8.5's first tier is a string equality against the §5.5 vocabulary, so the asked industry is
    # spelled the vocabulary's way before anything compares against it. Echoed back in `chips` so
    # the UI shows the user what was actually applied.
    chips = chips.model_copy(update={"industry": retrieve.canonical_industry(conn, chips.industry)})
    timer.mark("industry_ms")

    vectors = await retrieve.prepare(chips)
    timer.mark("embed_ms")

    retrieval = retrieve.retrieve(conn, chips, vectors)
    timer.mark("retrieve_ms")

    matcher = score.TermMatcher(repository.fetch_known_technologies(conn))
    timer.mark("vocab_ms")

    companies = score.rank(
        retrieval=retrieval,
        chips=chips,
        matcher=matcher,
        asked_industry=chips.industry,
        now=now,
        limit=limit,
    )
    timer.mark("score_ms")

    return SearchResponse(
        chips=chips,
        companies=companies,
        total_candidates=retrieval.list_sizes["candidates"],
        timing_ms=timer.total(),
        query_paraphrase=vectors.paraphrase,
        notes=list(notes or []),
    )


# ---------------------------------------------------------------------------
# §10 — the API
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> JSONResponse:
    """Green means: Postgres reachable, pgvector loaded, and every derived table exists."""
    try:
        with repository.connect() as conn:
            payload = repository.health(conn)
    except Exception as exc:  # noqa: BLE001 — health must report failure, never raise it.
        return JSONResponse(
            status_code=503, content={"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        )
    return JSONResponse(status_code=200 if payload["status"] == "ok" else 503, content=payload)


@app.post("/api/search", response_model=SearchResponse)
async def search(body: SearchRequest) -> SearchResponse:
    """§10 — natural language in, ranked companies out. The LLM parse is the only fuzzy step."""
    timer = Timer()
    with repository.connect() as conn:
        timer.mark("connect_ms")
        chips, notes = await parse.parse_query(conn, body.q)
        timer.mark("parse_ms")
        return await run_search(conn, chips, limit=body.limit, timer=timer, notes=notes)


@app.post("/api/search/structured", response_model=SearchResponse)
async def search_structured(body: StructuredRequest) -> SearchResponse:
    """§10 — chips in, ranked companies out. No LLM. This is what the evals measure (§14)."""
    timer = Timer()
    chips = Chips.model_validate(body.model_dump(exclude={"limit"}))
    with repository.connect() as conn:
        timer.mark("connect_ms")
        return await run_search(conn, chips, limit=body.limit, timer=timer)


# ---------------------------------------------------------------------------
# §13 phase 8 — the UI
# ---------------------------------------------------------------------------


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(str(value).strip()) if value and str(value).strip() else None
    except ValueError:
        return None


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(str(value).strip()) if value and str(value).strip() else None
    except ValueError:
        return None


def _enum_or_none(enum: Any, value: str | None) -> Any:
    if not value or not str(value).strip():
        return None
    try:
        return enum(str(value).strip())
    except ValueError:
        return None


def chips_from_form(form: Any) -> tuple[Chips, int]:
    """Rebuild `Chips` from the edited chip form.

    This is the payoff of §6[1]'s "returned in the response so the UI renders them editable": the
    user corrects a chip and the query re-runs through `/ui/refine`, which never calls the LLM.
    A wrong parse is one click from right, and the correction cannot itself be re-guessed.
    """
    values = form.getlist("term_value")
    sources = form.getlist("term_source")
    terms = [
        Term(value=v.strip(), source=_enum_or_none(TermSource, s) or TermSource.ANY)
        for v, s in zip(values, sources)
        if v and v.strip()
    ]
    chips = Chips(
        terms=terms,
        industry=(form.get("industry") or "").strip() or None,
        since_days=_int_or_none(form.get("since_days")),
        function=_enum_or_none(Function, form.get("function")),
        seniority=_enum_or_none(Seniority, form.get("seniority")),
        intent_mode=_enum_or_none(IntentMode, form.get("intent_mode")) or IntentMode.EITHER,
        min_employees=_int_or_none(form.get("min_employees")),
        max_employees=_int_or_none(form.get("max_employees")),
        min_revenue_usd=_float_or_none(form.get("min_revenue_usd")),
        max_revenue_usd=_float_or_none(form.get("max_revenue_usd")),
    )
    return chips, _int_or_none(form.get("limit")) or 20


def _render(request: Request, response: SearchResponse, *, q: str, llm_used: bool) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_results.html",
        {
            "r": response,
            "q": q,
            "llm_used": llm_used,
            "functions": [f.value for f in Function],
            "seniorities": [s.value for s in Seniority],
            "modes": [m.value for m in IntentMode],
            "sources": [s.value for s in TermSource],
        },
    )


def _error(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_error.html", {"message": message}, status_code=200
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {})


@app.post("/ui/search", response_class=HTMLResponse)
async def ui_search(request: Request) -> HTMLResponse:
    """Box -> LLM parse -> chips + cards."""
    form = await request.form()
    q = (form.get("q") or "").strip()
    if not q:
        return _error(request, "Type a query first.")
    timer = Timer()
    try:
        with repository.connect() as conn:
            chips, notes = await parse.parse_query(conn, q)
            timer.mark("parse_ms")
            response = await run_search(conn, chips, limit=20, timer=timer, notes=notes)
    except Exception as exc:  # noqa: BLE001 — the page must show the failure, not a blank div.
        log.exception("search failed")
        return _error(request, f"{type(exc).__name__}: {exc}")
    return _render(request, response, q=q, llm_used=True)


@app.post("/ui/refine", response_class=HTMLResponse)
async def ui_refine(request: Request) -> HTMLResponse:
    """Edited chips -> cards. Deterministic: this path never touches the LLM."""
    form = await request.form()
    q = (form.get("q") or "").strip()
    chips, limit = chips_from_form(form)
    timer = Timer()
    try:
        with repository.connect() as conn:
            response = await run_search(conn, chips, limit=limit, timer=timer)
    except Exception as exc:  # noqa: BLE001
        log.exception("refine failed")
        return _error(request, f"{type(exc).__name__}: {exc}")
    return _render(request, response, q=q, llm_used=False)
