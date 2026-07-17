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


# ---------------------------------------------------------------------------
# The deterministic company template — §5.3's paraphrase, written without an LLM.
#
# WHY THIS EXISTS. `company_signal` held 462 of 22,876 canonical companies (2%), because §5.3's
# extract only walked companies with a text-bearing job. Every structural query — "manufacturers
# in California", "companies running SAP ECC" — returned 0, and it looked like a search defect. It
# was not: the companies were not in the index. A lead-search tool whose leads are 98% absent is
# not a search problem, it is an empty database.
#
# WHY NOT THE LLM. The reason the other 22,414 were skipped was cost: ~22.5k `gpt-4.1-mini` calls,
# ~$25. But re-read what the LLM is actually being asked to do in `company_normalizer.md` — it
# reads `industry`, `hq_city`, `employee_range` and `technologies[]` and writes them into a
# sentence. It is not extracting anything; there is nothing to extract, because §5.3's input is
# already structured. It is a paid string formatter, and it is also a *non-deterministic* one.
# A template does the same job for the price of the embedding (~$0.13 for all 22.4k) and does it
# identically on every run. The LLM earns its money on job descriptions, where there is prose to
# comprehend. Here there is none.
#
# RULE 4 (symmetric normalization) IS WHAT SHAPES THE TEMPLATE. The query side embeds
# `retrieve.query_paraphrase`, which renders chips as "Manufacturing company running SAP and
# Snowflake." The 462 LLM rows read "Mid-size industrial machinery manufacturer in Ohio running
# SAP ECC, Salesforce and AWS." All three must live in one vocabulary or the cosines compare form
# rather than meaning. So the template copies the LLM prompt's own conventions, and two of them
# are worth naming because the obvious template violates both:
#
#   * NEVER NAME THE COMPANY. `company_normalizer.md` forbids it ("the name adds no signal and
#     pollutes matching"), and the pollution is concrete: `tsv` is built from this text, ~4,000
#     companies have "Manufacturing" in their legal name, and "{name} is a ..." would make a
#     lexical search for manufacturing match a logistics firm called "Acme Manufacturing Co".
#   * VERBALISE THE SIZE, never echo the enum. "with RANGE_10001_PLUS employees" is not a token
#     any query will ever produce.
# ---------------------------------------------------------------------------

# The prompt's own mapping (`company_normalizer.md`, "Field: paraphrase"), applied to a real
# headcount instead of to `employee_range`.
#
# `employee_range` is NOT used, and that is a data-quality finding rather than a preference:
# 16,931 of 22,941 active real companies are stamped `RANGE_10001_PLUS`, including every company
# whose `employee_count` reads `201-500 employees`. The enum is wrong on the majority of the
# corpus, so the 462 LLM paraphrases faithfully call 200-person companies "Enterprise". The
# template reads the headcount instead and falls back to the enum only when there is no headcount
# to read — better to be right on the 22,414 than consistently wrong with the 462.
_SIZE_BANDS: tuple[tuple[int, str], ...] = ((500, "Small"), (1000, "Mid-size"), (10000, "Large"))
_SIZE_ABOVE = "Enterprise"

_RANGE_TO_SIZE = {
    "RANGE_0_500": "Small",
    "RANGE_501_1000": "Mid-size",
    "RANGE_1001_5000": "Large",
    "RANGE_5001_10000": "Large",
    "RANGE_10001_PLUS": "Enterprise",
}

# How many technologies the PROSE names. `technologies[]` on the row always carries every one of
# them — that array is the authority that coverage (§8.4) and negation (§2.1) match against, and
# it is never truncated. This cap only governs the sentence.
#
# 150 because the distribution says so: of the 2,788 companies carrying any technographics the
# median is small, p99 is 167, and exactly **36 companies** exceed 150 (max 1,532). So the cap is
# a no-op for 22,905 of 22,941 companies, and on the other 36 it is the difference between a
# paraphrase and a 6,000-token dump that would (a) approach the embedding model's 8,191-token
# input limit, (b) drown the company's industry and location in the cosine, and (c) make `ts_rank`
# meaningless by matching everything. A search document is prose, not a serialisation.
TEMPLATE_TECH_CAP = 150


def _size_word(company: CompanySource) -> str | None:
    """The prompt's size vocabulary, from the parsed headcount (falling back to the enum)."""
    headcount = company.emp_high if company.emp_high is not None else company.emp_low
    if headcount is not None:
        for ceiling, word in _SIZE_BANDS:
            if headcount <= ceiling:
                return word
        return _SIZE_ABOVE
    return _RANGE_TO_SIZE.get((company.employee_range or "").strip().upper())


def _place(company: CompanySource) -> str | None:
    """`San Jose, California` — city and/or state, then country only when it adds something.

    Deduplicated because the corpus really does hold `hq_city = hq_state = 'Singapore'`, and
    "in Singapore, Singapore" is a token the document side would not otherwise produce.
    """
    parts: list[str] = []
    for value in (company.hq_city, company.hq_state, company.hq_country):
        cleaned = (value or "").strip()
        if cleaned and cleaned.lower() not in {p.lower() for p in parts}:
            parts.append(cleaned)
    return ", ".join(parts) or None


def template_paraphrase(company: CompanySource, technologies: Sequence[str]) -> str:
    """One canonical company + its canonical technologies -> the §5.3 paraphrase, deterministically.

    Every clause is dropped when its field is empty rather than rendered with a placeholder. That
    is the whole of "degrade gracefully": `None` must never reach the text, and neither must a
    phrase like "no technologies listed" — `company_normalizer.md`'s "Never mention absence" is
    not a style rule here. `tsv` and the embedding are built from this string, so a company with no
    technographics that says so in prose becomes *findable by the words describing its absence*,
    and would rank against every other company that also says it. Silence is the only honest way
    to say nothing.

    The floor is `"Company."` — reached by 0 companies today (22,938 of 22,941 have an industry),
    but `paraphrase` is NOT NULL and `embed._prepare` refuses empty input, so the floor has to
    exist and has to be a real sentence.
    """
    subject = " ".join(p for p in (_size_word(company), (company.industry or "").strip()) if p)
    subject = f"{subject} company" if subject else "Company"

    clauses: list[str] = []
    place = _place(company)
    if place:
        clauses.append(f"in {place}")

    named = [t.strip() for t in technologies if t and t.strip()][:TEMPLATE_TECH_CAP]
    if named:
        # "running A, B and C" — the same verb and list shape `company_normalizer.md` mandates
        # ("Use the verb runs/uses") and `retrieve.query_paraphrase` emits on the query side.
        listed = named[0] if len(named) == 1 else f"{', '.join(named[:-1])} and {named[-1]}"
        clauses.append(f"running {listed}")

    return f"{subject} {' '.join(clauses)}.".replace("  ", " ").strip() if clauses else f"{subject}."


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
