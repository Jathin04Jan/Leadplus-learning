"""SEARCH-EXPLAINED §9 — the role census. Deterministic contact classification, no LLM.

This is the contact analogue of `normalize.template_paraphrase`: it turns a raw job title into a
`(ContactFunction, ContactSeniority)` pair and a short role sentence that gets embedded. It is
deterministic on purpose — rule 1 keeps the LLM at the edges, and there is nothing here to
*comprehend*: a title is already structured enough that a regex ladder reads it more consistently
than a model would, and consistency is the whole product (§1).

**What this module never touches:** names, emails, phone numbers, LinkedIn URLs. It is handed a
title, a department, a seniority word and (for the Big-4 flag) the contact's Apollo
`employment_history` — and it emits a role, a function, a seniority and a "formerly at X" flag.
The output identifies a *seat*, not a person (§9's honest caveat: "the CFO of Acme" is still
pseudonymous — but it is the smallest thing that answers the question).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass

from .models import ContactFunction, ContactSeniority

# ---------------------------------------------------------------------------
# Function classification. First rule that fires wins, so ORDER IS PRECEDENCE.
#
# Domain-specific functions come before EXECUTIVE deliberately: a "Chief Financial Officer" is
# FINANCE, not EXECUTIVE — the domain is the answer to "who handles the money here", and the
# C-level-ness is carried by `seniority` instead. Only a general top-office title with no domain
# (CEO, President, Owner) lands in EXECUTIVE.
# ---------------------------------------------------------------------------
#
# Patterns are stem-based: `\bfinanc\w*` matches "finance" AND "financial". Short abbreviations
# carry their own trailing boundary (`\bcfo\b`) so they cannot match inside a longer word.
_FUNCTION_RULES: tuple[tuple[ContactFunction, re.Pattern[str]], ...] = (
    (
        ContactFunction.FINANCE,
        re.compile(
            r"\bcfo\b|\bfinanc\w*|\btreasur\w*|\bcontroller|\bcomptroller|\baccount(ing|ant)"
            r"|\bfp&a\b|\bfp and a\b|\baudit\w*|\bbookkeep\w*|\binvestor relations",
            re.I,
        ),
    ),
    (
        ContactFunction.LEGAL,
        re.compile(r"\blegal\b|\bcounsel\b|\battorney|\bparalegal|\bcomplianc\w*|\bclo\b", re.I),
    ),
    (
        ContactFunction.HR,
        re.compile(
            r"\bchro\b|\bhuman resource\w*|\bhr\b|\bpeople\b|\btalent\b|\brecruit\w*"
            r"|\bpersonnel\b|\bcompensation\b",
            re.I,
        ),
    ),
    (
        ContactFunction.PROCUREMENT,
        re.compile(
            r"\bprocure\w*|\bpurchasing\b|\bsourcing\b|\bbuyer\b|\bvendor manage\w*"
            r"|\bcategory manage\w*|\bsupplier\b",
            re.I,
        ),
    ),
    (
        ContactFunction.IT,
        re.compile(
            r"\bcio\b|\bcto\b|\bciso\b|\binformation technology\b|\bit\b|\bsoftware\b|\bdevelop\w*"
            r"|\bprogrammer\b|\bengineer\w*|\bdevops\b|\bsre\b|\bcloud\b|\binfrastructure\b"
            r"|\bcyber\w*|\bsecurity\b|\bdata (engineer|scien|analyt|platform)\w*|\barchitect\w*"
            r"|\btechnolog\w*|\bdigital\b|\bsystems\b|\bml\b|\bmachine learning\b|\banalytics\b"
            r"|\bbi\b",
            re.I,
        ),
    ),
    (
        ContactFunction.OPERATIONS,
        re.compile(
            r"\bcoo\b|\boperations\b|\bsupply chain\b|\blogistics\b|\bwarehouse\b|\bmanufactur\w*"
            r"|\bproduction\b|\bplant\b|\bquality\b|\bfulfil\w*|\bdistribution\b|\bmaintenance\b"
            r"|\bops\b",
            re.I,
        ),
    ),
    (
        ContactFunction.SALES,
        re.compile(
            r"\bcro\b|\bsales\b|\baccount (executive|manager)\b|\bbusiness development\b|\bbd\b"
            r"|\bcommercial\b|\bmarketing\b|\bgrowth\b|\bcustomer success\b|\bpartnership\w*",
            re.I,
        ),
    ),
    (
        ContactFunction.EXECUTIVE,
        re.compile(
            r"\bceo\b|\bchief executive\b|\bpresident\b|\bowner\b|\bfounder\b|\bco-?founder\b"
            r"|\bmanaging director\b|\bgeneral manager\b|\bgm\b|\bchairman\b|\bchairwoman\b"
            r"|\bproprietor\b|\bprincipal\b|\bpartner\b|\bboard member\b",
            re.I,
        ),
    ),
)

# ---------------------------------------------------------------------------
# Seniority classification. ORDER IS PRECEDENCE, highest rank first.
#
# `C_LEVEL` catches the chief/officer titles and the two-letter C*O family. `head of` is treated
# as DIRECTOR-band leadership (not C-level), because a "Head of Finance" at a mid-market company is
# a director, not a board seat.
# ---------------------------------------------------------------------------
#
# VP is tested BEFORE C_LEVEL on purpose: "Vice President" contains the word "president", and a
# plain "President" (a genuine C-level title) still falls through to C_LEVEL because it matches no
# VP token. Order is what disambiguates the two without a fragile lookbehind.
_SENIORITY_RULES: tuple[tuple[ContactSeniority, re.Pattern[str]], ...] = (
    (
        ContactSeniority.VP,
        re.compile(r"\bvice president\b|\bvp\b|\bsvp\b|\bevp\b|\bavp\b", re.I),
    ),
    (
        ContactSeniority.C_LEVEL,
        re.compile(
            r"\bchief\b|\bc[eflotixdmphr]o\b|\bowner\b|\bfounder\b|\bco-?founder\b|\bpresident\b"
            r"|\bmanaging director\b|\bmanaging partner\b|\bproprietor\b|\bpartner\b",
            re.I,
        ),
    ),
    (
        ContactSeniority.DIRECTOR,
        re.compile(r"\bdirector\b|\bhead of\b|\bdept head\b|\bdepartment head\b", re.I),
    ),
    (
        ContactSeniority.MANAGER,
        re.compile(r"\bmanager\b|\bmanaging\b|\bsupervisor\b|\bforeman\b|\bteam lead\b|\blead\b", re.I),
    ),
    (
        ContactSeniority.IC,
        re.compile(
            r"\bengineer\w*|\banalyst\b|\bspecialist\b|\bcoordinator\b|\bassociate\b|\bdeveloper\b"
            r"|\bconsultant\b|\badministrator\b|\brepresentative\b|\baccountant\b|\bbuyer\b"
            r"|\bofficer\b|\bclerk\b|\btechnician\b|\bassistant\b|\bplanner\b|\bdesigner\b"
            r"|\barchitect\w*|\bscientist\b|\bagent\b",
            re.I,
        ),
    ),
)

# The abbreviation given to `_SENIORITY_RULES` above is regex-only; this is the human word the
# census sentence reads with, so the embedding matches "VP of Finance" and "C-level executive".
_SENIORITY_WORD = {
    ContactSeniority.C_LEVEL: "C-level executive",
    ContactSeniority.VP: "VP-level",
    ContactSeniority.DIRECTOR: "Director-level",
    ContactSeniority.MANAGER: "Manager-level",
    ContactSeniority.IC: "individual contributor",
    ContactSeniority.OTHER: "staff",
}

_FUNCTION_WORD = {
    ContactFunction.FINANCE: "finance",
    ContactFunction.IT: "IT and technology",
    ContactFunction.OPERATIONS: "operations",
    ContactFunction.PROCUREMENT: "procurement",
    ContactFunction.SALES: "sales and commercial",
    ContactFunction.HR: "human resources",
    ContactFunction.LEGAL: "legal",
    ContactFunction.EXECUTIVE: "executive leadership",
    ContactFunction.OTHER: "general",
}

# The four firms. Matched against a PAST (non-current) employer only — an alumnus is someone who
# LEFT. `\bey\b` is deliberately narrow (word-boundaried, lowercased) to avoid matching "key",
# "eye", etc.; the four names carry the weight.
_BIG4 = re.compile(
    r"\b(deloitte|pwc|pricewaterhouse|pricewaterhousecoopers|"
    r"ernst\s*&?\s*young|\bey\b|klynveld|\bkpmg\b)\b",
    re.I,
)


def classify_function(title: str | None, department: str | None) -> ContactFunction:
    """Title (then department) -> ContactFunction. First matching rule wins."""
    haystack = f"{title or ''} {department or ''}"
    for function, pattern in _FUNCTION_RULES:
        if pattern.search(haystack):
            return function
    # Apollo/LeadPlus `department` values like `master_finance`, `master_information_technology`
    # are a fallback when the title itself was uninformative.
    dept = (department or "").lower()
    for key, function in (
        ("financ", ContactFunction.FINANCE),
        ("information_technology", ContactFunction.IT),
        ("engineering", ContactFunction.IT),
        ("operations", ContactFunction.OPERATIONS),
        ("sales", ContactFunction.SALES),
        ("marketing", ContactFunction.SALES),
        ("human_resources", ContactFunction.HR),
        ("legal", ContactFunction.LEGAL),
        ("procurement", ContactFunction.PROCUREMENT),
        ("supply", ContactFunction.OPERATIONS),
    ):
        if key in dept:
            return function
    return ContactFunction.OTHER


def classify_seniority(title: str | None, seniority_hint: str | None) -> ContactSeniority:
    """Title -> ContactSeniority. Falls back to the source `seniority` word when the title is mute."""
    haystack = title or ""
    for seniority, pattern in _SENIORITY_RULES:
        if pattern.search(haystack):
            return seniority
    hint = (seniority_hint or "").strip().lower()
    return {
        "c-level": ContactSeniority.C_LEVEL,
        "c_suite": ContactSeniority.C_LEVEL,
        "owner": ContactSeniority.C_LEVEL,
        "founder": ContactSeniority.C_LEVEL,
        "vp": ContactSeniority.VP,
        "director": ContactSeniority.DIRECTOR,
        "head": ContactSeniority.DIRECTOR,
        "manager": ContactSeniority.MANAGER,
        "lead": ContactSeniority.MANAGER,
        "senior": ContactSeniority.IC,
        "entry": ContactSeniority.IC,
        "analyst": ContactSeniority.IC,
        "coordinator": ContactSeniority.IC,
    }.get(hint, ContactSeniority.OTHER)


@dataclass(frozen=True)
class Big4History:
    """What the Apollo `employment_history` says about a contact's past — Big-4 only."""

    is_big4_alum: bool
    prior_employer: str | None
    landed_at: dt.date | None  # start date of the CURRENT role, i.e. when they "landed"


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def big4_history(apollo_data: str | None) -> Big4History:
    """Parse one `apollo_contact_data.data` JSON blob for Big-4 alumni evidence.

    An alumnus is someone whose PAST (non-current) employer is a Big-4 firm. `landed_at` is the
    start date of their CURRENT role — the answer to "recently landed", where the data carries it.

    Honest limit noted in the ingest report: `employment_history` entries carry `start_date` but
    the corpus's most-recent starts are ~2022, so "recently" is relative to the clone's snapshot.
    """
    if not apollo_data or "employment_history" not in apollo_data:
        return Big4History(False, None, None)
    try:
        data = json.loads(apollo_data)
    except (json.JSONDecodeError, TypeError):
        return Big4History(False, None, None)

    history = data.get("employment_history")
    if not isinstance(history, list):
        return Big4History(False, None, None)

    prior_big4: str | None = None
    landed_at: dt.date | None = None
    for entry in history:
        if not isinstance(entry, dict):
            continue
        org = (entry.get("organization_name") or "").strip()
        current = bool(entry.get("current"))
        if current and landed_at is None:
            landed_at = _parse_date(entry.get("start_date"))
        if not current and org and prior_big4 is None and _BIG4.search(org):
            prior_big4 = org
    return Big4History(prior_big4 is not None, prior_big4, landed_at)


# The abbreviation a user actually types for a (function, seniority) seat. Added to the census so a
# lexical search for "CFO" hits a "Chief Financial Officer" (whose title text has no "CFO" token)
# and the semantic space anchors on the common word. This is symmetric normalization (rule 4) at
# the role grain: the query says "CFO", the document is taught to say "CFO" too.
_ROLE_ALIASES: dict[tuple[ContactFunction, ContactSeniority], str] = {
    (ContactFunction.FINANCE, ContactSeniority.C_LEVEL): "CFO chief financial officer finance leader",
    (ContactFunction.FINANCE, ContactSeniority.VP): "VP of Finance vice president of finance finance leader",
    (ContactFunction.FINANCE, ContactSeniority.DIRECTOR): "Director of Finance finance leader",
    (ContactFunction.IT, ContactSeniority.C_LEVEL): "CTO CIO chief technology officer chief information officer",
    (ContactFunction.IT, ContactSeniority.VP): "VP of IT VP of Technology",
    (ContactFunction.OPERATIONS, ContactSeniority.C_LEVEL): "COO chief operating officer operations leader",
    (ContactFunction.OPERATIONS, ContactSeniority.VP): "VP of Operations",
    (ContactFunction.SALES, ContactSeniority.C_LEVEL): "CRO chief revenue officer chief sales officer",
    (ContactFunction.SALES, ContactSeniority.VP): "VP of Sales",
    (ContactFunction.HR, ContactSeniority.C_LEVEL): "CHRO chief human resources officer chief people officer",
    (ContactFunction.LEGAL, ContactSeniority.C_LEVEL): "CLO general counsel chief legal officer",
    (ContactFunction.PROCUREMENT, ContactSeniority.C_LEVEL): "CPO chief procurement officer",
    (ContactFunction.PROCUREMENT, ContactSeniority.VP): "VP of Procurement",
    (ContactFunction.EXECUTIVE, ContactSeniority.C_LEVEL): "CEO chief executive officer",
}


def role_aliases(function: ContactFunction, seniority: ContactSeniority) -> str:
    """The abbreviation for this seat, so "CFO" finds a "Chief Financial Officer". May be empty."""
    return _ROLE_ALIASES.get((function, seniority), "")


def census_text(
    *,
    canonical_title: str | None,
    function: ContactFunction,
    seniority: ContactSeniority,
    department: str | None,
    is_big4_alum: bool,
    prior_employer: str | None,
) -> str:
    """The sentence that gets embedded and lexically indexed. Role words only — never a name.

    Written to read like the question a PEOPLE query asks ("a VP-level person in finance …"), so
    the query embedding and the census embedding live in one vocabulary (rule 4). The raw title is
    included verbatim so a lexical search for "CFO" or "VP of Finance" hits the exact words the
    user typed, and the Big-4 clause carries both "Big Four" and "Big-4" so either spelling lexes.
    """
    title = (canonical_title or "").strip()
    parts = [
        f"A {_SENIORITY_WORD[seniority]} in {_FUNCTION_WORD[function]}."
    ]
    if title:
        parts.append(f"Role: {title}.")
    aliases = role_aliases(function, seniority)
    if aliases:
        parts.append(f"Also known as: {aliases}.")
    dept = (department or "").strip()
    if dept and not dept.lower().startswith("master_"):
        parts.append(f"Department: {dept}.")
    if is_big4_alum and prior_employer:
        parts.append(f"Big Four (Big-4) alumnus, formerly at {prior_employer}.")
    return " ".join(parts)
