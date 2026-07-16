"""OpenAI access — structured outputs, backoff, cost accounting, and the tracing seam.

Two rules from ARCHITECTURE.md govern this file:

  * §5.2 stage 2 / §9: normalization uses **OpenAI structured outputs against a pydantic schema**,
    never free-text parsing. There is no `json.loads` of a model reply anywhere in this project.
  * Rule 1: the LLM lives at the edges only. This module is imported by ingest (normalize) and,
    later, by the query parser. It must never be imported by the ranking path.

§11: LangSmith is optional observability. With no `LANGSMITH_API_KEY` set — the case here —
`@traced` is an identity decorator and nothing is emitted. It must never hard-fail.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence, TypeVar

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel

from . import config

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# Tracing seam (§11) — LangSmith if a key is present, otherwise a no-op.
# ---------------------------------------------------------------------------

TRACING_ENABLED = bool(os.environ.get("LANGSMITH_API_KEY"))

if TRACING_ENABLED:  # pragma: no cover — no key in this environment.
    try:
        from langsmith import traceable as _traceable

        def traced(name: str) -> Callable[[F], F]:
            return _traceable(name=name)  # type: ignore[return-value]

    except ImportError:
        log.warning("LANGSMITH_API_KEY is set but langsmith is not installed; tracing disabled.")
        TRACING_ENABLED = False

if not TRACING_ENABLED:

    def traced(name: str) -> Callable[[F], F]:  # noqa: D103
        def decorator(fn: F) -> F:
            return fn

        return decorator


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    """Token counters, so the ingest can report what it actually spent (§13 phase 4)."""

    calls: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    embed_tokens: int = 0
    embed_calls: int = 0
    by_model: dict[str, int] = field(default_factory=dict)

    def add_chat(self, model: str, prompt: int, completion: int, cached: int = 0) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.cached_tokens += cached
        self.completion_tokens += completion
        self.by_model[model] = self.by_model.get(model, 0) + 1

    def add_embed(self, tokens: int) -> None:
        self.embed_calls += 1
        self.embed_tokens += tokens

    @property
    def cost_usd(self) -> float:
        """Actual cost, crediting the cached-input discount.

        The normalizer prompt is ~6.6k tokens and identical on every call, so OpenAI's automatic
        prompt caching serves most of it at a discount. Billing raw `prompt_tokens` at full price
        would overstate the real spend, sometimes by a lot.
        """
        chat = config.PRICE_PER_MTOK.get(config.CHAT_MODEL, {"input": 0.0, "output": 0.0})
        embed = config.PRICE_PER_MTOK.get(config.EMBED_MODEL, {"input": 0.0, "output": 0.0})
        fresh = max(0, self.prompt_tokens - self.cached_tokens)
        return (
            fresh / 1e6 * chat["input"]
            + self.cached_tokens / 1e6 * chat["input"] * config.CACHED_INPUT_DISCOUNT
            + self.completion_tokens / 1e6 * chat["output"]
            + self.embed_tokens / 1e6 * embed["input"]
        )

    @property
    def cost_usd_uncached(self) -> float:
        """What it would have cost with no prompt caching — the pessimistic bound."""
        chat = config.PRICE_PER_MTOK.get(config.CHAT_MODEL, {"input": 0.0, "output": 0.0})
        embed = config.PRICE_PER_MTOK.get(config.EMBED_MODEL, {"input": 0.0, "output": 0.0})
        return (
            self.prompt_tokens / 1e6 * chat["input"]
            + self.completion_tokens / 1e6 * chat["output"]
            + self.embed_tokens / 1e6 * embed["input"]
        )

    def report(self) -> str:
        hit = (100.0 * self.cached_tokens / self.prompt_tokens) if self.prompt_tokens else 0.0
        return (
            f"chat: {self.calls} calls, {self.prompt_tokens:,} in / {self.completion_tokens:,} out tokens\n"
            f"       {self.cached_tokens:,} of the input was cache-hit ({hit:.0f}%)\n"
            f"embed: {self.embed_calls} calls, {self.embed_tokens:,} tokens\n"
            f"cost: ${self.cost_usd:.4f}  (list price without caching: ${self.cost_usd_uncached:.4f})"
        )


USAGE = Usage()


# ---------------------------------------------------------------------------
# Client + retry
# ---------------------------------------------------------------------------

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=config.openai_api_key(), max_retries=0, timeout=90.0)
    return _client


class TokenBucket:
    """Client-side tokens-per-minute budget.

    Refills continuously at `limit/60` tokens per second and caps at a full minute's budget, so a
    burst is allowed only if the preceding idle time paid for it. Requests estimate their cost and
    wait for the budget instead of firing and being rejected.

    This is the difference between "retry until it works" and "don't exceed the limit". A retry
    ladder alone cannot fix a sustained overrun: if arrivals outpace the budget, every retry lands
    on an empty bucket and rows dead-letter for no reason.
    """

    def __init__(self, limit_per_minute: int, margin: float = 1.0) -> None:
        self.capacity = max(1.0, limit_per_minute * margin)
        self.rate = self.capacity / 60.0
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float) -> None:
        # Never deadlock on a single request larger than the whole budget.
        need = min(float(tokens), self.capacity)
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= need:
                    self._tokens -= need
                    return
                wait = (need - self._tokens) / self.rate
            await asyncio.sleep(min(wait, 5.0))


_bucket: TokenBucket | None = None


def bucket() -> TokenBucket:
    global _bucket
    if _bucket is None:
        _bucket = TokenBucket(config.TPM_LIMIT, config.TPM_MARGIN)
    return _bucket


def estimate_tokens(*texts: str) -> int:
    """Rough token estimate for budgeting: ~4 chars/token, plus room for the reply.

    Deliberately crude — the bucket only needs to be approximately right, and over-estimating
    costs a little throughput while under-estimating costs a 429.
    """
    chars = sum(len(t) for t in texts)
    return int(chars / 3.5) + 800


RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)


def _retry_after(exc: Exception) -> float | None:
    """The server's own instruction, when it gives one.

    A 429 carries `retry-after` / `retry-after-ms`. Honouring it beats guessing: a TPM window is
    60s wide, so a blind exponential ladder can exhaust its attempts inside a single window and
    dead-letter rows that would have succeeded a few seconds later.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    for header, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(header)
        if raw:
            try:
                return float(raw) * scale
            except (TypeError, ValueError):
                continue
    return None


async def _with_backoff(fn: Callable[[], Any], *, what: str) -> Any:
    """§5.7: exponential backoff with jitter, capped, honouring Retry-After when present.

    The SDK's own retries are disabled (`max_retries=0`) so this is the only ladder — one place
    to reason about, one place to fix.
    """
    last: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            return await fn()
        except RETRYABLE as exc:
            last = exc
            if attempt == config.MAX_RETRIES - 1:
                break
            # Full jitter (AWS's "Exponential Backoff and Jitter"): sleeping a random point in
            # [0, backoff] de-synchronises 20 workers that all hit the same limit together.
            # Without it they retry in lockstep and re-trigger the same 429.
            backoff = min(config.BACKOFF_BASE * (2**attempt), config.BACKOFF_MAX)
            sleep = random.uniform(backoff / 2, backoff)
            server = _retry_after(exc)
            if server is not None:
                sleep = max(sleep, server + random.uniform(0, 1.0))
            log.warning(
                "%s: %s — retry %d/%d in %.1fs",
                what, type(exc).__name__, attempt + 1, config.MAX_RETRIES, sleep,
            )
            await asyncio.sleep(sleep)
    raise last  # type: ignore[misc]


T = TypeVar("T", bound=BaseModel)


@traced("normalize")
async def structured(
    *, system: str, user: str, schema: type[T], model: str | None = None
) -> tuple[T, str]:
    """One structured-outputs call. Returns (parsed model, raw JSON) — raw goes to the dead-letter.

    `.parse()` enforces the schema server-side, so a malformed reply is impossible by
    construction rather than by a fragile parser (§5.2 stage 2). A refusal raises, and the
    caller dead-letters it.
    """
    model = model or config.CHAT_MODEL
    await bucket().acquire(estimate_tokens(system, user))

    async def _call() -> Any:
        return await client().chat.completions.parse(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
            temperature=0,
        )

    completion = await _with_backoff(_call, what=f"structured({model})")

    if completion.usage:
        details = getattr(completion.usage, "prompt_tokens_details", None)
        USAGE.add_chat(
            model,
            completion.usage.prompt_tokens,
            completion.usage.completion_tokens,
            cached=getattr(details, "cached_tokens", 0) or 0,
        )

    message = completion.choices[0].message
    raw = message.content or ""
    if getattr(message, "refusal", None):
        raise ValueError(f"model refused: {message.refusal}")
    parsed = message.parsed
    if parsed is None:
        raise ValueError(f"structured output returned no parsed value; raw={raw[:500]!r}")
    return parsed, raw


@traced("embed_batch")
async def embed_batch(texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
    """One embeddings call. §5.2 stage 4 batches 100 texts per call; batching is the caller's job."""
    model = model or config.EMBED_MODEL
    await bucket().acquire(estimate_tokens(*texts))

    async def _call() -> Any:
        return await client().embeddings.create(
            model=model, input=list(texts), dimensions=config.EMBED_DIMS
        )

    response = await _with_backoff(_call, what=f"embed({model})")
    if response.usage:
        USAGE.add_embed(response.usage.total_tokens)
    # The API preserves input order, but index is authoritative — sort rather than trust it.
    return [item.embedding for item in sorted(response.data, key=lambda d: d.index)]


async def gather_limited(coros: Sequence[Any], *, limit: int | None = None) -> list[Any]:
    """§5.7: concurrency 20. Exceptions are returned, not raised — the caller dead-letters them."""
    semaphore = asyncio.Semaphore(limit or config.INGEST_CONCURRENCY)

    async def _run(coro: Any) -> Any:
        async with semaphore:
            return await coro

    return await asyncio.gather(*(_run(c) for c in coros), return_exceptions=True)
