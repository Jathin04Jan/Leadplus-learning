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
from psycopg.conninfo import conninfo_to_dict

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


# ---------------------------------------------------------------------------
# LOCAL-ONLY GUARD — the hard stop before any connection is opened.
#
# The clone is complete and RDS is done with. This app must never open a socket to a hosted
# database again: it is not our data to write, another team owns rows in it, and a stray
# `DATABASE_URL` in a shell would otherwise send an ingest — 2,886 LLM-normalized rows and a
# CREATE TABLE — straight at production. The failure mode this prevents is silent: psycopg would
# connect happily and everything downstream would look like it worked.
#
# So the URL is validated at import, not at connect time: `config` is imported by every module
# and every script (they all go through `repository.connect()`, which reads DATABASE_URL from
# here), which makes this the one chokepoint that cannot be bypassed by adding a caller.
#
# Both halves must hold. Host alone is not enough — an SSH tunnel on localhost:5433 can forward
# to RDS, and the db-name check is what catches that. Name alone is not enough either.
# ---------------------------------------------------------------------------

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


class NonLocalDatabaseError(RuntimeError):
    """Raised when DATABASE_URL points anywhere but a local database. Never caught."""


def assert_local_database(url: str) -> str:
    """Reject any DATABASE_URL that is not a local database. Returns the URL when it is safe.

    Requires BOTH:
      * host is localhost / 127.0.0.1 / ::1 (or absent — a unix socket), AND
      * the database name contains 'local'.

    Parsed with **psycopg's own `conninfo_to_dict`**, deliberately, and never with `urlsplit`.
    libpq accepts two syntaxes and psycopg.connect() honours both:

        postgresql://user@host:5432/dbname                  <- URL form
        host=hostname dbname=name user=u                    <- keyword/value form

    `urlsplit` understands only the first. Handed the second it returns `hostname=None`, which
    reads as "unix socket" — so `host=<RDS> dbname=leadplus_local` passed a urlsplit-based guard
    (no host, name contains 'local') and connected straight to RDS. That was a real bypass in
    this function, found by trying it.

    The lesson generalises: a guard must parse its input with the SAME parser as the thing it is
    guarding, or the two disagree about what the string means and the gap between them is the
    vulnerability. `conninfo_to_dict` is exactly what psycopg.connect() uses.
    """
    try:
        parts = conninfo_to_dict(url)
    except Exception as exc:  # noqa: BLE001 — unparseable is not provably local
        raise NonLocalDatabaseError(f"DATABASE_URL is not parseable: {exc}") from None

    # An absent host means libpq falls back to PGHOST, then to a unix socket. Resolve that the
    # same way libpq would, or `DATABASE_URL=dbname=leadplus_local` + `PGHOST=<rds>` is the same
    # bypass wearing a different hat.
    host = str(parts.get("host") or os.environ.get("PGHOST") or "").strip().lower()
    dbname = str(parts.get("dbname") or "").strip().lower()
    # Redact credentials before quoting the URL back into an exception or a log line.
    shown = f"{host or '(socket)'}/{dbname or '(none)'}"

    if host not in _LOCAL_HOSTS:
        raise NonLocalDatabaseError(
            f"REFUSING to connect to a non-local database: {shown}\n"
            f"  host must be one of: localhost, 127.0.0.1, ::1\n"
            f"  This app is local-only. The RDS clone is complete; RDS is read-never now.\n"
            f"  If you meant the local clone, unset DATABASE_URL or set it to\n"
            f"    postgresql://leadplus:leadplus@localhost:5433/leadplus_local"
        )
    if "local" not in dbname:
        raise NonLocalDatabaseError(
            f"REFUSING to connect: database name {dbname!r} does not contain 'local' ({shown}).\n"
            f"  A local host is not sufficient — a tunnel on localhost can forward to a hosted DB.\n"
            f"  Expected a database like 'leadplus_local'."
        )
    return url


DATABASE_URL = assert_local_database(
    os.environ.get("DATABASE_URL", "postgresql://leadplus:leadplus@localhost:5433/leadplus_local")
)

# §11 as amended: gpt-4.1-mini normalizes + parses.
#
# EMBEDDINGS: text-embedding-3-large at 3072 dims, up from -small/1536. The reason is the rival
# team's `lead_company_job_intent` — their vectors are 3072-dim, and one query vector has to be
# comparable against jobs, companies AND intents (ours and, in a UNION, theirs). Two dims means
# two query embeds and no cross-source cosine. The corpus is 2,886 jobs; the delta is pennies.
CHAT_MODEL = os.environ.get("INTEL_CHAT_MODEL", "gpt-4.1-mini")
EMBED_MODEL = os.environ.get("INTEL_EMBED_MODEL", "text-embedding-3-large")
EMBED_DIMS = 3072

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
# the raw term goes to tech_review_queue for a human. Reused for intents (same ladder).
TECH_NN_THRESHOLD = 0.85

# The corpus gate. On the real clone only 2,886 of 13,082 active jobs carry a description worth
# reading; the other ~78% are stubs — a title and a few array hints, no prose. Normalizing a stub
# does not produce a weak signal, it produces an INVENTED one: with no description to constrain
# it the model has nothing to work from but the title and the company's ambient stack, which is
# precisely the failure the v5 prompt fix exists to prevent. So they are excluded at the source
# rather than filtered later — cheaper, and it keeps `job_signal` meaning "we read this posting".
#
# This is a scraper gap, not a data-modelling decision: if the scraper starts filling
# descriptions, this filter lets the new rows in automatically on the next run.
MIN_DESCRIPTION_CHARS = 200

# §5.6: repost = same (company_id, title_norm) within 90 days AND paraphrase cosine > 0.95.
REPOST_WINDOW_DAYS = 90
REPOST_COSINE_THRESHOLD = 0.95

# Pricing per 1M tokens (USD), for the cost report. gpt-4.1-mini and text-embedding-3-small.
PRICE_PER_MTOK = {
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
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
