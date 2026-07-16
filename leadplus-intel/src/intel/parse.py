"""Phase 9 — the NL query parser (ARCHITECTURE.md §6[1], §9).

This is one of the LLM's exactly two jobs (rule 1): normalize documents at ingest, and parse the
query here. It sits at the **edge**. What it produces is `Chips`, and from that point the search
is deterministic arithmetic — which is why `/api/search/structured` can skip this module entirely
and why the evals use that endpoint.

Two guarantees this module owes the rest of the system:

  * **Cache on the normalized query string** (§6[1]). The same question asked twice must not cost
    two LLM calls, and must not risk two different parses.
  * **The model cannot invent terms.** §9's named anti-pattern is a prompt with no extraction rule
    for keywords, so the model guesses and the same sentence parses differently on different runs.
    `prompts/query_parser.md` fixes that with a rule; `_reject_invented_terms` is the belt to that
    braces, because a term the user never said silently re-ranks everything around it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import psycopg

from . import config, llm, repository
from .canonicalize import tech_key
from .models import Chips, TermGroup

log = logging.getLogger(__name__)

PROMPT = "query_parser"

# The §6[1] cache: normalized query string -> parse. A dict rather than `functools.lru_cache`
# because the parse is async and the value is worth inspecting in tests.
_CACHE: dict[str, tuple[Chips, list[str]]] = {}


def prompt_version() -> str:
    return config.load_prompt(PROMPT).qualified_version


def normalize_query(q: str) -> str:
    """The cache key: case- and whitespace-insensitive, so trivial re-typings share one parse."""
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def _system_prompt(conn: psycopg.Connection) -> str:
    """Load the prompt file and inject the live industry vocabulary.

    §5.5: reuse `lead_query WHERE type='COMPANY_INDUSTRY'` — the list the Java system already
    injects as `{{INDUSTRY_LIST}}`. Injecting it rather than hard-coding it in the prompt file is
    what keeps the parser's idea of "an industry" and the ingest's from drifting apart; two
    taxonomies for one concept is how two systems silently disagree about what "Manufacturing"
    means.
    """
    prompt = config.load_prompt(PROMPT)
    vocabulary = repository.fetch_industry_vocabulary(conn)
    rendered = "\n".join(f"- {value}" for value in vocabulary) or "- (none configured)"
    return prompt.body.replace("{{INDUSTRY_LIST}}", rendered)


def _term_is_grounded(value: str, query: str) -> bool:
    """Did the user actually say this?

    Two ways to be satisfied, because the parser is allowed to tidy a name but not to invent one:

      * the term's punctuation-stripped key appears in the query's — catches casing and spacing
        differences (`S/4 HANA` in the query -> `SAP S/4HANA` as a term);
      * or any word of the term appears as a word of the query — catches a legitimate expansion
        to the catalogue's name (`azure` in the query -> `Microsoft Azure` as a term).

    A wholly fabricated term ("Snowflake" from a query that only said "data platforms") satisfies
    neither and is dropped.
    """
    query_key = tech_key(query)
    if tech_key(value) and tech_key(value) in query_key:
        return True
    query_words = set(re.findall(r"[a-z0-9]+", query.lower()))
    term_words = [w for w in re.findall(r"[a-z0-9]+", value.lower()) if len(w) >= 3]
    return any(word in query_words for word in term_words)


def _reject_invented_terms(chips: Chips, query: str) -> tuple[Chips, list[str]]:
    """Drop terms and locations the query does not contain, and say so out loud.

    Dropped rather than kept-and-flagged: `terms` drives coverage, so an invented term makes every
    genuinely-correct company look like a partial match (2-of-3 instead of 2-of-2) and reorders
    the whole list. Dropping restores the user's actual question. The note is returned in the
    response, and the chips are editable, so nothing is hidden.

    CHANGES-v2 raises the stakes on both sides, so both are checked now:

      * a **negated** group is a hard filter (§2.1), so an invented negation does not merely
        re-rank — it *deletes* companies the user never asked to exclude, and a false negative is
        invisible by construction. Nobody can see the company that isn't there.
      * a **location** is a hard filter too (§3.2). An invented `hq_state` empties the entire
        result set, and an empty page reads as "no such companies exist" rather than "the parser
        made something up".

    An alternate is checked individually: `{any_of: [AWS, Azure]}` where the user said only "AWS"
    keeps the group and drops `Azure`, because the group is still a real requirement. A group
    whose every alternate was invented is dropped whole.
    """
    kept: list[TermGroup] = []
    notes: list[str] = []
    for group in chips.terms:
        alternates = [a for a in group.any_of if a.strip()]
        grounded = [a for a in alternates if _term_is_grounded(a, query)]
        for invented in [a for a in alternates if a not in grounded]:
            notes.append(
                f"dropped invented term {invented!r}: not present in the query (§9 — the parser "
                "may tidy a name, never add one)"
            )
            log.warning("query_parser invented a term: %r for query %r", invented, query)
        if grounded:
            kept.append(group.model_copy(update={"any_of": grounded}))

    locations = []
    for location in chips.locations:
        if _term_is_grounded(location.value, query):
            locations.append(location)
        else:
            notes.append(
                f"dropped invented location {location.value!r}: not present in the query. A "
                "location is a HARD filter (§3.2), so an invented one empties the result set."
            )
            log.warning("query_parser invented a location: %r for query %r", location.value, query)

    return chips.model_copy(update={"terms": kept, "locations": locations}), notes


async def parse_query(conn: psycopg.Connection, q: str) -> tuple[Chips, list[str]]:
    """NL query -> `Chips` (+ any notes about what was rejected). Cached per §6[1].

    Raises whatever the LLM call raises; the caller decides what a failed parse means. There is
    no silent fallback to an empty `Chips`, because an empty parse is a *valid* query object that
    would return a confidently-ranked list of nothing in particular.
    """
    key = normalize_query(q)
    if key in _CACHE:
        return _CACHE[key]

    chips, _raw = await llm.structured(system=_system_prompt(conn), user=q, schema=Chips)
    chips, notes = _reject_invented_terms(chips, q)

    _CACHE[key] = (chips, notes)
    return chips, notes


def cache_stats() -> dict[str, Any]:
    return {"entries": len(_CACHE)}
