"""Stage 4 — embeddings (ARCHITECTURE.md §5.2 stage 4).

`text-embedding-3-large`, 3072 dims, batched 100 texts per call. The dims are set by config.EMBED_DIMS;
they are 3072 so that one query vector compares against job_signal, company_signal AND job_intent —
including, if ever UNIONed, the other team's 3072-dim `lead_company_job_intent`.

**Embed the paraphrase, never the raw description.** The paraphrase is the normalized signal;
the description is 60-70% boilerplate that would dominate the vector and make every posting look
like every other posting. This is the whole reason stage 2 exists.

Rule 4: the query side must embed the *normalized query paraphrase* through this same function,
so both sides live in one vocabulary. That path is built in the retrieval phase; it must call
`embed_texts`, not the OpenAI client directly.
"""

from __future__ import annotations

import logging
from typing import Sequence

from . import config, llm

log = logging.getLogger(__name__)


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Embed texts in input order, batching per §5.2 stage 4.

    Batches run sequentially: the embeddings endpoint already takes 100 inputs per call, so the
    386-row corpus is 4 calls. Adding concurrency here would buy nothing and cost rate limit.
    """
    if not texts:
        return []

    cleaned = [_prepare(t) for t in texts]
    out: list[list[float]] = []
    for start in range(0, len(cleaned), config.EMBED_BATCH_SIZE):
        batch = cleaned[start : start + config.EMBED_BATCH_SIZE]
        vectors = await llm.embed_batch(batch)
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"embedding count mismatch: sent {len(batch)}, got {len(vectors)}"
            )
        out.extend(vectors)

    for vector in out:
        if len(vector) != config.EMBED_DIMS:
            raise RuntimeError(
                f"expected {config.EMBED_DIMS}-dim embeddings, got {len(vector)}; "
                f"the schema's vector({config.EMBED_DIMS}) columns would reject this"
            )
    return out


async def embed_text(text: str) -> list[float]:
    """Single-text convenience. Prefer `embed_texts` — one call per row is the wasteful path."""
    return (await embed_texts([text]))[0]


def _prepare(text: str) -> str:
    """The API rejects empty input; a whitespace-only paraphrase is a bug upstream, not here."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("refusing to embed empty text")
    return cleaned
