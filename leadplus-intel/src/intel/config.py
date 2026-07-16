"""Configuration — env loading, model ids, prompt loading + versioning.

ARCHITECTURE.md §9: prompts are files, never inline strings. §5.7: `prompt_version` goes in
every row, and changing a prompt is a data migration.

The version is declared in the prompt file's YAML front-matter rather than hashed from the file
bytes: hashing would make a whitespace edit look like a new prompt and silently re-normalize the
whole corpus. Bumping the version is therefore a deliberate act, which is exactly what §5.7 wants.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# The repo root is three levels up from src/intel/config.py.
ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = ROOT / "prompts"
SQL_DIR = ROOT / "sql"

# `load_dotenv()` with no args walks up from the *cwd*, which breaks when a script is run from
# elsewhere. Always point at the known file.
load_dotenv(ROOT / ".env")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in (see README)."
        )
    return value


DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://leadplus:leadplus@localhost:5433/leadplus_local"
)

# §11: gpt-4.1-mini normalizes + parses; text-embedding-3-small at 1536 dims.
CHAT_MODEL = os.environ.get("INTEL_CHAT_MODEL", "gpt-4.1-mini")
EMBED_MODEL = os.environ.get("INTEL_EMBED_MODEL", "text-embedding-3-small")
EMBED_DIMS = 1536

# §5.2 / §5.7: concurrency 20 with exponential backoff; embeddings batch 100 texts per call.
INGEST_CONCURRENCY = int(os.environ.get("INTEL_CONCURRENCY", "20"))
EMBED_BATCH_SIZE = 100
FETCH_BATCH_SIZE = 100

# Retry ladder for 429/5xx. Capped so a retry never sleeps longer than a TPM window is wide.
MAX_RETRIES = 8
BACKOFF_BASE = 2.0
BACKOFF_MAX = 45.0

# Client-side tokens-per-minute budget (llm.py's token bucket).
#
# Concurrency alone cannot respect a TPM limit. This account allows 200k TPM and the job
# normalizer costs ~5.3k tokens per call, so the ceiling is ~37 calls/min NO MATTER how many run
# in parallel. Without a budget, 20 workers saturate the bucket, every retry lands on a still-full
# bucket, and rows dead-letter on 429s that were never really failures — which is exactly what the
# first full run did (14 rows, then 44).
#
# So the bucket throttles admission and concurrency stays at the spec's 20. Set INTEL_TPM to your
# org's limit; the margin leaves room for the estimator being wrong.
TPM_LIMIT = int(os.environ.get("INTEL_TPM", "200000"))
TPM_MARGIN = 0.80

# §5.2 stage 3: embedding nearest-neighbour hit threshold. Below this we never auto-guess —
# the raw term goes to tech_review_queue for a human.
TECH_NN_THRESHOLD = 0.85

# §5.6: repost = same (company_id, title_norm) within 90 days AND paraphrase cosine > 0.95.
REPOST_WINDOW_DAYS = 90
REPOST_COSINE_THRESHOLD = 0.95

# Pricing per 1M tokens (USD), for the cost report. gpt-4.1-mini and text-embedding-3-small.
PRICE_PER_MTOK = {
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
}
# Cached input tokens bill at 25% of the input rate. The normalizer's ~6.6k-token system prompt is
# identical on every call, so most of it is served from cache after the first few requests.
CACHED_INPUT_DISCOUNT = 0.25


def openai_api_key() -> str:
    return _require("OPENAI_API_KEY")


@dataclass(frozen=True)
class Prompt:
    """A prompt file: its declared version and its body (front-matter stripped)."""

    name: str
    version: str
    body: str

    @property
    def qualified_version(self) -> str:
        """What lands in `prompt_version` columns, e.g. `job_normalizer/v1`."""
        return f"{self.name}/{self.version}"


_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@lru_cache(maxsize=None)
def load_prompt(name: str) -> Prompt:
    """Load `prompts/<name>.md`, requiring a `version:` key in its front-matter."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt file missing: {path}")
    raw = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(raw)
    if not match:
        raise ValueError(f"{path} has no YAML front-matter; a `version:` key is required (§5.7)")
    meta = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("\"'")
    if "version" not in meta:
        raise ValueError(f"{path} front-matter has no `version:` key (§5.7)")
    return Prompt(name=name, version=meta["version"], body=raw[match.end() :].strip())
