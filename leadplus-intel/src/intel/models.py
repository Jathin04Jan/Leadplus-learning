"""The contract — pydantic v2 models and the closed enums (ARCHITECTURE.md §5.2, §5.9).

`SignalRecord` and `CompanyRecord` are handed straight to OpenAI structured outputs as the
response schema. They are therefore *strict*: every field required, no defaults, no `| None`.
The model must choose an enum value — `UNKNOWN`/`OTHER` is how it declines to guess (§9).
"""

from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# §5.9 — provisional enums. Closed sets; each value is described in prompts/job_normalizer.md.
# ---------------------------------------------------------------------------


class Initiative(str, Enum):
    NEW_IMPLEMENTATION = "NEW_IMPLEMENTATION"
    MIGRATION = "MIGRATION"
    MODERNIZATION = "MODERNIZATION"
    SCALE_OUT = "SCALE_OUT"
    MAINTENANCE = "MAINTENANCE"
    UNKNOWN = "UNKNOWN"


class Function(str, Enum):
    DATA_ENGINEERING = "DATA_ENGINEERING"
    ERP = "ERP"
    CLOUD_INFRA = "CLOUD_INFRA"
    SECURITY = "SECURITY"
    APP_DEV = "APP_DEV"
    ANALYTICS = "ANALYTICS"
    INTEGRATION = "INTEGRATION"
    NETWORKING = "NETWORKING"
    OTHER = "OTHER"


class Seniority(str, Enum):
    INTERN = "INTERN"
    JUNIOR = "JUNIOR"
    MID = "MID"
    SENIOR = "SENIOR"
    LEAD = "LEAD"
    ARCHITECT = "ARCHITECT"
    MANAGER = "MANAGER"
    DIRECTOR = "DIRECTOR"
    EXEC = "EXEC"


class EngagementType(str, Enum):
    PERMANENT = "PERMANENT"
    CONTRACT = "CONTRACT"
    CONSULTING = "CONSULTING"
    UNKNOWN = "UNKNOWN"


class IntentMode(str, Enum):
    """§8.2 — selects the weight profile. Not a filter; it only re-weights the axes.

    `USES`   — "companies running X". Company technographics answer this; jobs are corroboration.
    `HIRING` — "companies hiring for X". Job postings answer this, and freshness is the point.
    `EITHER` — the user did not say. The balanced profile.
    """

    USES = "USES"
    HIRING = "HIRING"
    EITHER = "EITHER"


class TermSource(str, Enum):
    """§8.4 — which haystack a term is checked against.

    This is the axis that makes "companies *using* Snowflake" a different question from
    "companies *hiring for* Snowflake" (§5.1), rather than two phrasings of one query.
    """

    USES = "USES"
    HIRING = "HIRING"
    ANY = "ANY"


# ---------------------------------------------------------------------------
# LLM outputs (structured outputs schemas)
# ---------------------------------------------------------------------------


class SignalRecord(BaseModel):
    """§5.2 stage 2 — the normalized form of one job posting."""

    model_config = ConfigDict(extra="forbid")

    initiative: Initiative
    function: Function
    seniority: Seniority
    engagement_type: EngagementType
    technologies: list[str] = Field(
        description="Named products/platforms only; raw extraction, canonicalised in stage 3."
    )
    paraphrase: str = Field(description="1-2 sentences, signal only, no boilerplate.")
    confidence: float = Field(description="0..1 confidence in this extraction.")


class CompanyRecord(BaseModel):
    """§5.3 — the normalized form of one company profile.

    Same vocabulary as `SignalRecord.paraphrase` (rule 4: symmetric normalization).
    """

    model_config = ConfigDict(extra="forbid")

    technologies: list[str]
    paraphrase: str
    confidence: float


# ---------------------------------------------------------------------------
# Source rows (read-only projections of LeadPlus tables — we never write these)
# ---------------------------------------------------------------------------


class JobSource(BaseModel):
    """A row of the §5.2 stage-1 extract query."""

    id: int
    lead_company_id: int
    title: str | None = None
    description: str | None = None
    department: str | None = None
    location: str | None = None
    type: str | None = None
    posted_date: dt.datetime | None = None
    skills: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    company_name: str | None = None
    industry: str | None = None
    employee_range: str | None = None

    @property
    def title_norm(self) -> str:
        """§5.6: `title_norm = lower(trim(title))`."""
        return (self.title or "").strip().lower()


class CompanySource(BaseModel):
    """One canonical company, with its structured fields unioned across `member_ids` (§5.4)."""

    canonical_id: int
    member_ids: list[int]
    domain: str | None = None
    name: str | None = None
    industry: str | None = None
    hq_city: str | None = None
    hq_state: str | None = None
    hq_country: str | None = None
    region: str | None = None
    employee_count: str | None = None
    employee_range: str | None = None
    revenue_usd: float | None = None
    keywords: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    scraped_technologies: list[str] = Field(default_factory=list)
    scraped_tools: list[str] = Field(default_factory=list)
    scraped_services: list[str] = Field(default_factory=list)


class CanonicalCompany(BaseModel):
    """§5.4 — a fold group: one real company, one or more `lead_company` rows."""

    canonical_id: int
    domain: str | None = None
    member_ids: list[int]


# ---------------------------------------------------------------------------
# Derived rows we own
# ---------------------------------------------------------------------------


class JobSignalRow(BaseModel):
    """A `job_signal` row, ready to upsert."""

    job_id: int
    company_id: int
    initiative: str | None = None
    function: str | None = None
    seniority: str | None = None
    engagement_type: str | None = None
    technologies: list[str] = Field(default_factory=list)
    paraphrase: str
    confidence: float | None = None
    title_norm: str | None = None
    is_repost: bool = False
    embedding: list[float] | None = None
    posted_date: dt.datetime | None = None
    prompt_version: str
    model: str


class CompanySignalRow(BaseModel):
    """A `company_signal` row, ready to upsert."""

    company_id: int
    paraphrase: str
    technologies: list[str] = Field(default_factory=list)
    industry_raw: str | None = None
    industry_canonical: str | None = None
    industry_embedding: list[float] | None = None
    embedding: list[float] | None = None
    prompt_version: str
    model: str


# ---------------------------------------------------------------------------
# §6[1] — the query contract. `Chips` is both the LLM parser's structured output
# and the hand-written input to /api/search/structured.
# ---------------------------------------------------------------------------


class Term(BaseModel):
    """§6[1] — one thing the user asked for, and which side of the corpus should answer it.

    A term is NEVER a filter (rule 2). It feeds coverage (§8.4). 3-of-3 outranks 1-of-3; the
    1-of-3 company is still returned. That is the AND/OR cliff, deleted.
    """

    model_config = ConfigDict(extra="forbid")

    value: str
    source: TermSource = TermSource.ANY


class Chips(BaseModel):
    """§6[1] — the parsed query, rendered editable in the UI.

    The UI shows these because a wrong parse must be a visible one-click fix, not an invisible
    reason the results are bad. `/api/search/structured` takes this object directly and runs the
    identical deterministic core, with no LLM anywhere.

    Field-by-field, what is and is not allowed to remove a company:
      * `terms`        — never filters (rule 2). Coverage only.
      * `industry`     — never filters (rule 2). A soft multiplier (§8.5).
      * `since_days`   — HARD filter. `posted_date` is a fact.
      * employees/revenue — HARD filters. Firmographic facts.
      * `function` / `seniority` — filter the JOB evidence set only (see retrieve.py).
    """

    model_config = ConfigDict(extra="forbid")

    terms: list[Term] = Field(default_factory=list)
    industry: str | None = None  # soft multiplier — NEVER a filter
    since_days: int | None = None  # hard filter — a fact
    function: Function | None = None
    seniority: Seniority | None = None
    intent_mode: IntentMode = IntentMode.EITHER

    # Beyond §6[1]'s listing, but required by rule 2 / §6[2], which name employee and revenue
    # range as the other two hard filters. §6[1]'s Chips has no field to carry them.
    min_employees: int | None = None
    max_employees: int | None = None
    min_revenue_usd: float | None = None
    max_revenue_usd: float | None = None


# ---------------------------------------------------------------------------
# §10 — the response. Every ranking must be explainable from this object alone.
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    """One job posting, as the reason a company ranked. The paraphrase is the product (§1)."""

    job_id: int
    title: str | None = None
    posted_date: dt.datetime | None = None
    paraphrase: str
    matched_terms: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    function: str | None = None
    seniority: str | None = None
    initiative: str | None = None
    days_ago: int | None = None


class Breakdown(BaseModel):
    """§8.6's inputs, itemised — the four axes, their weights, and the multiplier.

    `weighted_subtotal * industry_multiplier == score`, exactly. The response carries this so a
    ranking can be argued with rather than believed.
    """

    coverage: float
    recency: float
    volume: float
    best_doc: float
    weights: dict[str, float]
    weighted_subtotal: float
    industry_multiplier: float
    intent_mode: IntentMode
    matched_terms: list[str] = Field(default_factory=list)
    unmatched_terms: list[str] = Field(default_factory=list)
    matched_count: int
    asked_count: int
    distinct_roles: int
    days_since_latest_post: int | None = None
    rrf_score: float
    industry_similarity: float | None = None
    industry_rule: str


class CompanyResult(BaseModel):
    company_id: int
    name: str | None = None
    domain: str | None = None
    industry_raw: str | None = None
    industry_canonical: str | None = None
    technologies: list[str] = Field(default_factory=list)
    paraphrase: str
    score: float
    breakdown: Breakdown
    evidence: list[Evidence] = Field(default_factory=list)


class SearchResponse(BaseModel):
    chips: Chips
    companies: list[CompanyResult]
    total_candidates: int
    timing_ms: dict[str, float]
    query_paraphrase: str
    notes: list[str] = Field(default_factory=list)
