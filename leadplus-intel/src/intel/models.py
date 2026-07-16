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

    `PEOPLE` is deliberately absent: CHANGES-v2 §6 is skipped (gates A and B measured **zero** on
    this corpus), so there is no contact side for a term to be routed to.
    """

    USES = "USES"
    HIRING = "HIRING"
    ANY = "ANY"


class QueryIntent(str, Enum):
    """CHANGES-v2 §1 — what the user's sentence *is*, decided before any retrieval runs.

    This exists because v1 failed silently, which is the same disease as the shipped Java system.
    An unparseable query produced empty chips, retrieval ran anyway, and RRF confidently ranked
    whatever the vector scan happened to return — three different questions produced the
    *identical* garbage set. A search that cannot say "I don't understand" will lie instead.

    `ACTION` and `UNPARSEABLE` never reach retrieval. See `main.triage`.
    """

    SEARCH = "SEARCH"  # proceed
    ACTION = "ACTION"  # campaigns/emails/segments -> not this app (§11)
    UNPARSEABLE = "UNPARSEABLE"  # no filters could be extracted


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


class TermGroup(BaseModel):
    """CHANGES-v2 §2 — one requirement, possibly satisfiable several ways.

    **Alternates within a group OR. Groups AND for coverage. Fields AND.** One level of nesting,
    and deliberately no more: a full boolean AST is a week of work plus a UI nobody can edit
    (§2). "SAP and also AWS or Azure" is `[{any_of:[SAP]}, {any_of:[AWS, Azure]}]` — coverage 2/2.

    A *positive* group is never a filter (rule 2): it feeds coverage (§8.4), so 2-of-2 outranks
    1-of-2 and the 1-of-2 company is still returned. That is the AND/OR cliff, deleted.

    A *negated* group is the deliberate exception (§2.1): **positive terms rank, negative terms
    remove.** A positive false-positive merely ranks lower and a human can see it; a user who says
    "exclude anything on S/4HANA" and is shown S/4HANA companies has caught the tool lying.
    """

    model_config = ConfigDict(extra="forbid")

    any_of: list[str] = Field(description="Alternates. The group matches if ANY of these match.")
    source: TermSource = TermSource.ANY
    negate: bool = False

    @property
    def label(self) -> str:
        """How the group reads on a card and in a `Breakdown`: `AWS or Azure`."""
        return " or ".join(v.strip() for v in self.any_of if v.strip())


class Value(BaseModel):
    """CHANGES-v2 §2 — a single value that may be asked for or excluded. Reused by
    `industries` and `locations`."""

    model_config = ConfigDict(extra="forbid")

    value: str
    negate: bool = False


class Chips(BaseModel):
    """§6[1] — the parsed query, rendered editable in the UI.

    The UI shows these because a wrong parse must be a visible one-click fix, not an invisible
    reason the results are bad. `/api/search/structured` takes this object directly and runs the
    identical deterministic core, with no LLM anywhere.

    Field-by-field, what is and is not allowed to remove a company:
      * `intent`          — CHANGES-v2 §1. `ACTION`/`UNPARSEABLE` remove *everything*, on purpose:
                            they refuse rather than answer a question nobody asked.
      * positive `terms`  — never filter (rule 2). Coverage only.
      * negated `terms`   — HARD, company-level, and matched against canonical `technologies[]`
                            ONLY (§2.1's non-negotiable guard rail — never prose, never `tsv`).
      * `industries`      — soft multiplier (§8.5) by default; HARD when `industry_strict`, and
                            always HARD when negated (§5).
      * `locations`       — HARD. A place is a fact (§3.2).
      * `segments` / `naics` / `sic` / `has_linkedin` — HARD. Facts on `lead_company` (§4).
      * `since_days`      — HARD filter. `posted_date` is a fact.
      * employees/revenue — HARD filters. Firmographic facts.
      * `function` / `seniority` — filter the JOB evidence set only (see retrieve.py).

    Rule 2 is *sharpened* by the new hard filters, not broken by them (§12): the rule was never
    "don't filter", it was "don't filter on fuzzy things". A state name and a NULL check are
    exact; `industry` stays free text and therefore stays soft unless the user says "strictly".
    """

    model_config = ConfigDict(extra="forbid")

    intent: QueryIntent = QueryIntent.SEARCH
    terms: list[TermGroup] = Field(default_factory=list)

    # §5 — was `industry: str | None`. A list, because "manufacturing or automotive" is one
    # question, and `industry_strict` because the user is allowed to override the soft default.
    industries: list[Value] = Field(default_factory=list)
    industry_strict: bool = False

    # §3 — positives OR each other; each negative is its own AND-NOT.
    locations: list[Value] = Field(default_factory=list)

    # §4 — structured facts riding the `lead_company` join that already exists.
    segments: list[str] = Field(default_factory=list)  # closed set: Enterprise | Mid-Market | SMB
    naics: list[str] = Field(default_factory=list)
    sic: list[str] = Field(default_factory=list)
    has_linkedin: bool | None = None

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

    def positive_groups(self) -> list[TermGroup]:
        """The groups that rank. Only these enter coverage's denominator (§2, §10)."""
        return [g for g in self.terms if not g.negate and g.label]

    def negated_groups(self) -> list[TermGroup]:
        """The groups that remove. Never counted toward coverage — they are filters (§2)."""
        return [g for g in self.terms if g.negate and g.label]

    def is_empty(self) -> bool:
        """CHANGES-v2 §1 — is there any predicate here at all?

        `intent_mode` is excluded deliberately: it selects a weight profile (§8.2), it is not
        something the user asked for. A `Chips` carrying nothing but `intent_mode=EITHER` is the
        empty parse that v1 happily retrieved on.
        """
        return not any(
            (
                [g for g in self.terms if g.label],
                self.industries,
                self.locations,
                self.segments,
                self.naics,
                self.sic,
                self.has_linkedin is not None,
                self.since_days is not None,
                self.function is not None,
                self.seniority is not None,
                self.min_employees is not None,
                self.max_employees is not None,
                self.min_revenue_usd is not None,
                self.max_revenue_usd is not None,
            )
        )


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

    # §10 — groups, not flat terms: `AWS or Azure` is ONE requirement, and reporting it as two
    # would say a company matching only AWS covered half of it. It covered all of it.
    matched_groups: list[str] = Field(default_factory=list)
    unmatched_groups: list[str] = Field(default_factory=list)

    # §10 — which negated group removed this company. Non-empty **only** on the companies in
    # `SearchResponse.excluded`, which carry the score they *would* have had.
    # **An exclusion the user cannot see is one they cannot trust.**
    excluded_by: list[str] = Field(default_factory=list)

    # The rank this company would have held in the FULL unfiltered ranking — i.e. the position it
    # occupied before the negation removed it. Measured by ranking the whole un-negated candidate
    # set, not by counting positions within `excluded`: those two numbers differ, and only the
    # first one is the claim a user would read it as.
    would_rank: int | None = None

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

    # CHANGES-v2 §3/§4 — the facts the new hard filters select on, echoed back on every card.
    # A filter the user cannot verify from the result is a filter they have to take on faith, and
    # "every result really is in California" should be readable rather than believed.
    hq_city: str | None = None
    hq_state: str | None = None
    hq_country: str | None = None
    segments: list[str] = Field(default_factory=list)
    linkedin_url: str | None = None


class SearchResponse(BaseModel):
    chips: Chips
    companies: list[CompanyResult]
    total_candidates: int
    timing_ms: dict[str, float]
    query_paraphrase: str
    notes: list[str] = Field(default_factory=list)

    # CHANGES-v2 §10 — the companies a negated group removed, each carrying the score and
    # `breakdown.excluded_by` explaining what it would have scored and which group deleted it.
    # Ordered by the rank they would have held. Empty when the query negates nothing.
    excluded: list[CompanyResult] = Field(default_factory=list)

    # CHANGES-v2 §1 — set when `intent` is ACTION or UNPARSEABLE. When this is non-null,
    # `companies` is empty **because we refused**, not because nothing matched. The UI must render
    # the difference; an empty list is exactly the silent failure this field exists to end.
    refusal: str | None = None
