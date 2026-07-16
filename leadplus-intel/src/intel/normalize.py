"""Stage 2 — normalize documents with the LLM (ARCHITECTURE.md §5.2, §5.3).

The prompts live in `prompts/*.md` and are loaded, never inlined (§9). This module only builds
the user message — every *instruction* is in the prompt file, so the rules can be read and
reviewed without reading Python. That is the whole point of §9: the shipped system's core bug is
an omission inside a prompt file nobody read.

Rule 4 (symmetric normalization): the query parser must eventually feed the same vocabulary these
two prompts emit. Both write third-person, signal-only paraphrases for exactly that reason.
"""

from __future__ import annotations

from typing import Any, Sequence

from . import config, llm
from .models import CompanyRecord, CompanySource, JobSource, SignalRecord

JOB_PROMPT = "job_normalizer"
COMPANY_PROMPT = "company_normalizer"


def job_prompt_version() -> str:
    return config.load_prompt(JOB_PROMPT).qualified_version


def company_prompt_version() -> str:
    return config.load_prompt(COMPANY_PROMPT).qualified_version


def _field(label: str, value: Any) -> str | None:
    """Render one labelled field, or nothing when it is empty.

    Empty fields are omitted rather than sent as `null`: an explicit empty field invites the
    model to fill it in, which is exactly the guessing both prompts forbid.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if v is not None and str(v).strip()]
        return f"{label}: {', '.join(items)}" if items else None
    text = str(value).strip()
    return f"{label}: {text}" if text else None


def _join(parts: Sequence[str | None]) -> str:
    return "\n".join(p for p in parts if p)


def job_user_message(job: JobSource) -> str:
    """Serialise one posting for the normalizer."""
    header = _join(
        [
            _field("Job title", job.title),
            _field("Department", job.department),
            _field("Location", job.location),
            _field("Employment type field", job.type),
            _field("Posted date", job.posted_date.date().isoformat() if job.posted_date else None),
            _field("Company industry", job.industry),
            _field("Company employee range", job.employee_range),
        ]
    )
    hints = _join(
        [
            _field("scraper technologies[]", job.technologies),
            _field("scraper tools[]", job.tools),
            _field("scraper services[]", job.services),
            _field("scraper skills[]", job.skills),
            _field("scraper requirements[]", job.requirements),
        ]
    )
    # The company NAME is deliberately withheld: both prompts forbid naming the company in the
    # paraphrase, and withholding it is a stronger guarantee than instructing against it. It also
    # removes the "Sapient Consulting Group" -> "SAP" trap at the source.
    return _join(
        [
            "## Posting",
            header,
            "",
            "## Description (authoritative)",
            (job.description or "").strip() or "(no description)",
            "",
            "## Scraper hints (incomplete, sometimes wrong — the description wins)",
            hints or "(none)",
        ]
    )


def company_user_message(company: CompanySource) -> str:
    """Serialise one canonical company for the normalizer.

    §5.3: structured columns only. `notes`, `account_summary` and `salesperson_name` are never
    selected by the repository, so they cannot leak here even by accident.
    """
    return _join(
        [
            "## Company fields",
            _join(
                [
                    _field("Industry", company.industry),
                    _field("HQ city", company.hq_city),
                    _field("HQ state", company.hq_state),
                    _field("HQ country", company.hq_country),
                    _field("Region", company.region),
                    _field("Employee count", company.employee_count),
                    _field("Employee range", company.employee_range),
                    _field(
                        "Revenue (USD)",
                        f"{company.revenue_usd:,.0f}" if company.revenue_usd else None,
                    ),
                    _field("keywords[]", company.keywords),
                    _field("technologies[] (curated technographics)", company.technologies),
                    _field("scraped_technologies[] (from its job ads)", company.scraped_technologies),
                    _field("scraped_tools[] (from its job ads)", company.scraped_tools),
                    _field("scraped_services[] (service categories, NOT products)", company.scraped_services),
                ]
            )
            or "(no fields populated)",
        ]
    )


async def normalize_job(job: JobSource) -> tuple[SignalRecord, str]:
    """One posting -> one SignalRecord. Raises on failure; the caller dead-letters it (§5.7)."""
    prompt = config.load_prompt(JOB_PROMPT)
    return await llm.structured(
        system=prompt.body, user=job_user_message(job), schema=SignalRecord
    )


async def normalize_company(company: CompanySource) -> tuple[CompanyRecord, str]:
    """One canonical company -> one CompanyRecord."""
    prompt = config.load_prompt(COMPANY_PROMPT)
    return await llm.structured(
        system=prompt.body, user=company_user_message(company), schema=CompanyRecord
    )
