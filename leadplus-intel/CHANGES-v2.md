# Change Spec v2 — capability + honesty

> **Amends `ARCHITECTURE.md`.** That document still governs — every §4 design rule holds. This spec
> explains where a rule's *default* is overridden and why. Build in the order in §9.
>
> **Context:** v1 shipped and was tested against a ~29-prompt sheet. It failed a third of it, and —
> worse — it failed *silently*: unparseable queries returned five confident wrong answers instead of
> "I don't understand." That is the same disease as the shipped Java system, reproduced in the
> replacement. §1 is therefore the highest priority item here, above every feature.

---

## 0. VERIFIED FINDINGS (measured on this repo before building — read before trusting §11)

Probes run against the live v1 app:

| Probe | Actual result |
|---|---|
| `"exclude anything already on S/4HANA"` | **Parsed as a POSITIVE term.** #1 result *is on* S/4HANA at `cov=1.00`. **Negation is INVERTED, not ignored** — worse than this spec claims. |
| `"manufacturing in California, 500–1000 employees"` | `California` **silently dropped** |
| `"mid-market in Illinois or Ohio"` | chips `{}` → 3 garbage results |
| `"create a 3-step campaign"` | chips `{}` → 3 garbage results |
| `"ignore all previous instructions…"` | chips `{}` → 3 garbage results. **No injection** — structured outputs held. |
| `"SAP and also AWS or Azure"` | flat `[SAP, AWS, Azure]` — grouping lost |
| `"automotive, revenue > $100M"` | ✅ works |
| `"<50 employees but >$10M revenue"` | ✅ works — honestly returns 0 |

### §8 gates — run, and they change the plan

| Gate | Result | Consequence |
|---|---|---|
| **A** `employment_history` in `apollo_contact_data` | **0 / 518** | "Big-4 alumnus recently landed" is **dead on data** |
| **B** CFO / VP-Finance contacts | **0 / 1,242** | `PEOPLE` mode untestable |
| **C** companies with jobs | 202 / 301 | seeded — proves nothing |
| `hq_state` format | **full names** (`Texas`, `California`) | **§3.1's premise is INVERTED.** It assumes `hq_state='CA'`. Canonical must be the **full lowercase name**; aliases `ca`/`calif` → `california`. Building it as written returns zero. |
| `naics` / `sic` / `segments` / `linkedin` | 222 / 207 / 301 / 253 of 301 | §4 buildable **and** testable |
| `lead_contact_normalized_title` | has `canonical_title`, `seniority`, `keywords` | §6.2 is right — reusable *if* §6 ever runs |

**→ §6 (`contact_signal`) is SKIPPED.** Gates A and B are zero: it can be neither built usefully nor
tested here. Revisit only when the real corpus is readable.

> **⚠️ SUPERSEDED — `contact_signal` was later BUILT.** Gates A and B above were measured on the
> **synthetic seed** and were wrong. On the real clone both pass (A: 4,659/36,145 carry
> `employment_history`; B: 20 CFOs / 906 finance roles), so the role census shipped: **53,746
> roles, no PII columns**, a 4th retrieval source in `PEOPLE` mode. See SEARCH-EXPLAINED §9 and
> ARCHITECTURE §3 for the built design. The rest of §6 below is kept as the historical decision
> record; it no longer reflects the shipped system.

---

## 1. Intent triage + the empty-chips guard — **DO THIS FIRST**

~30 lines, worth more than every feature below.

Today: unparseable query → empty chips → retrieval runs anyway → RRF ranks whatever the vector scan
returned. Three different questions produced *the identical* garbage set. That is the failure mode.

```python
class QueryIntent(str, Enum):
    SEARCH      = "SEARCH"       # proceed
    ACTION      = "ACTION"       # campaigns/emails/segments -> not this app
    UNPARSEABLE = "UNPARSEABLE"  # no filters could be extracted
```

Add `intent: QueryIntent` to `Chips`. In `main.py`, **before retrieval**:
- `ACTION` → *"This app searches for companies. Campaigns, emails and segments are the LeadPlus campaign assistant's job."*
- `UNPARSEABLE` → *"I couldn't extract any filters from that."*
- `SEARCH` **but every chip empty** → force `UNPARSEABLE`. **Never retrieve on an empty predicate.**

**Also the prompt-injection defence.** Structured outputs mean *"ignore all previous instructions"*
can only ever produce a `Chips` object — no free-text channel, so no system prompt can leak.

## 2. The predicate model — `TermGroup`

One level of nesting. Enough for every prompt in the sheet; a full boolean AST is a week plus a UI
nobody can edit. **Do not build one.**

```python
class TermGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    any_of: list[str]                       # OR within the group
    source: TermSource = TermSource.ANY     # USES | HIRING | ANY   (no PEOPLE source — contacts
                                            # are selected by result_mode, not by routing a term; §9)
    negate: bool = False

class Value(BaseModel):                     # reused by industries + locations
    value: str
    negate: bool = False
```

**Alternates within a group OR. Groups AND for coverage. Fields AND.**

| Prompt | Parses to |
|---|---|
| "SAP and also AWS or Azure" | `[{any_of:[SAP]}, {any_of:[AWS, Azure]}]` → coverage 2/2 |
| "Snowflake and AWS, exclude S/4HANA" | `[{[Snowflake]}, {[AWS]}, {[S/4HANA], negate:true}]` |

`coverage = matched_positive_groups / total_positive_groups`. A group matches if **any** alternate
matches. Negated groups never count toward coverage — they are filters.

### 2.1 Negation is a filter — the deliberate exception to rule 2

> **Positive terms rank. Negative terms remove.**

A positive false-positive merely ranks lower — harmless. A user who says *"exclude anything on
S/4HANA"* and sees S/4HANA companies has caught the tool lying. Honouring an explicit removal
instruction softly is the same as ignoring it.

**Guard rail — non-negotiable:** negation matches **canonical `technologies[]` only, never paraphrase
text or `tsv`.** `NOT LIKE '%sap%'` would delete Sapient by substring — this project's founding bug,
inverted into **false negatives, which are invisible**. Strictly worse than the false positives we fixed.

Exclusion is **company-level** and respects `source`:

```sql
-- source USES / ANY  -> installed base
AND NOT EXISTS (SELECT 1 FROM company_signal cs2
                WHERE cs2.company_id = cc.canonical_id AND cs2.technologies && %(neg_uses)s::text[])
-- source HIRING / ANY -> what they're hiring for
AND NOT EXISTS (SELECT 1 FROM job_signal js2
                WHERE js2.company_id = cc.canonical_id AND js2.technologies && %(neg_hiring)s::text[])
```

## 3. Location — the biggest hole

No re-ingest: `hq_city`/`hq_state`/`hq_country` are on `lead_company`, already joined in
`repository.py` (~line 346).

```python
locations: list[Value] = []     # positives OR; negatives each AND-NOT
```

### 3.1 Canonicalise, or it silently misses

```sql
CREATE TABLE location_alias (
  alias      text PRIMARY KEY,   -- 'ca', 'calif', 'california'
  canonical  text NOT NULL,      -- 'california'   <-- FULL LOWERCASE NAME (matches our hq_state)
  kind       text NOT NULL       -- 'state' | 'country' | 'city'
);
```

⚠️ **Corrected from the original spec:** our `hq_state` holds **full names** (`California`), not
`CA`. Canonical is the full lowercase name; `ca`/`calif` are aliases *pointing at it*. The original
had this backwards and would return zero.

Seed 50 US states + abbreviations, ~30 countries + ISO codes, ~100 cities via
`scripts/bootstrap_locations.py` — **no LLM**. Parser emits raw text; the repository expands it
through `location_alias` before matching.

### 3.2 The filter (hard — a fact; rule 2 permits it)

```sql
AND (cardinality(%(loc_pos)s::text[]) = 0
     OR lower(c.hq_state) = ANY(%(loc_pos)s) OR lower(c.hq_city) = ANY(%(loc_pos)s)
     OR lower(c.hq_country) = ANY(%(loc_pos)s))
AND NOT (lower(c.hq_state) = ANY(%(loc_neg)s) OR lower(c.hq_city) = ANY(%(loc_neg)s)
      OR lower(c.hq_country) = ANY(%(loc_neg)s))
```

## 4. Structured fact filters

All ride the existing join. **Zero LLM cost — facts, not derived signal.**

```python
segments: list[str] = []          # HARD. Closed set — SEE THE CORRECTION BELOW, it is NOT sizes
naics: list[str] = []             # HARD. array overlap on c.naics_codes
sic: list[str] = []               # HARD. array overlap on c.sic_codes
has_linkedin: bool | None = None  # HARD. a NULL check is exact
```

Architecturally consistent, not rule violations: rule 2 says *filter on facts*. `industry` stays soft
**because it is free text** — the rule was never "don't filter", it was "don't filter on fuzzy things".

### ⚠️ CORRECTION — `segments` was built on a fiction, and "mid-market" could never match

This section asserted `segments` is `Enterprise | Mid-Market | SMB`. **Not one of those three
values exists in `lead_company.segments[]`.** Measured on the real corpus (active, real companies):

| Real segment value | Companies |
|---|---|
| `General` | 17,270 |
| `Automate26` | 6,101 |
| `Robotics` | 258 |
| `Food Equipment` | 89 |
| `Medical` | 60 |
| `Logistics` | 6 |

They are **trade-show / product-line categories, not size bands.** So the §7 parser rule
"mid-market → `Mid-Market`" emitted a hard-filter value that matched **zero** rows — every
"mid-market" query silently returned an empty set, the §1 disease with a fresh mask. `SMB` and
`Enterprise` were equally dead.

**The decision (Task 3): `segments` is remapped to the six real values, and size words emit no
segment at all.** There is no honest mapping from "mid-market" onto `General`/`Automate26`/…, so
the parser must not invent one. Size intent is carried by `min_employees`/`max_employees`/revenue
when the user gives a number, and by nothing when they give only a vague band word — the same rule
those fields already enforce ("mid-size" is an adjective, not a filter). `segments` now only fires
when the user names one of the six categories ("companies in the Robotics segment"). See
`prompts/query_parser.md`'s `segments` section. The consequence for the acceptance sheet:
"mid-market companies in Illinois or Ohio…" drops the dead segment chip and is carried by
its location + employee + revenue facts, which is why it returns real IL/OH companies instead of
an empty page.

## 5. `industry_strict` — let the user override the default

```python
industries: list[Value] = []      # was: industry: str | None
industry_strict: bool = False     # "strictly" | "only" | "must be" -> hard filter
```

- default → §8.5 soft multiplier, `max()` across the list
- `industry_strict` → hard filter on `industry_canonical`, multiplier skipped
- negated values → always hard

## 6. ~~`contact_signal` — SKIPPED~~ → **BUILT** (the skip was superseded)

> **This section's SKIP decision was reversed.** Gates A and B measured zero on the *synthetic
> seed*; on the real clone they pass, and `contact_signal` shipped — 53,746 roles, no PII columns,
> a role census consulted in `PEOPLE` result mode. `ContactFunction`, `ContactSeniority` and
> `result_mode` **are built** (as a vocabulary separate from the job enums). The canonical design
> and acceptance output now live in **SEARCH-EXPLAINED §9** and **ARCHITECTURE §3** — read those,
> not the historical text below.

Gates A and B were **zero** on the *synthetic* corpus (§0), which is why this was skipped at the
time. "Big-4 alumnus recently landed" looked dead on data; `PEOPLE` mode had nothing to return.

The original design (now the built one) holds: ingest a **role census** (`title`, `department`,
`seniority`, `normalized_title_tokens`) and **never** names, emails, phones or LinkedIn — you don't
need identifying fields to answer at company level. "The CFO of Acme" is still *pseudonymous*, not
anonymous. (`lead_contact_normalized_title` turned out **empty** on the real clone, so function and
seniority are derived from the title text instead.) The job `Function` enum was **not** widened
with `FINANCE` — job enums describe reqs, not people; contacts got their own vocabulary.

## 7. Parser rules — a rule per field, or none of the above works

`ARCHITECTURE.md` §9 already warns: *every field you want populated needs an explicit rule*. Adding
fields to `Chips` without adding rules to `prompts/query_parser.md` reproduces the Java system's core
bug exactly. **Land these WITH each field, never after.**

| Field | Triggers |
|---|---|
| `negate` | "exclude", "not", "isn't", "without", "but no", "instead of", "other than" |
| `any_of` grouping | "X or Y" inside a requirement → one group; "and also" → new group |
| `locations` | "in X", "based in X", "headquartered in X", "located in X" |
| `segments` | closed set: Enterprise / Mid-Market / SMB. "mid-market" → `Mid-Market` |
| `industry_strict` | "strictly", "only", "must be", "exclusively" |
| `has_linkedin: false` | "no LinkedIn profile", "missing LinkedIn", "without a LinkedIn" |
| `naics` / `sic` | bare numeric codes, "NAICS 334111", "SIC code" |
| `intent: ACTION` | "create a campaign", "draft an email", "save as segment", "generate a template" |
| `intent: UNPARSEABLE` | nothing extractable — **prefer this over guessing** |

**Never invent terms the user did not say.** Empty is always better than a guess — the whole lesson of §1.

## 8. Do not enrich the seed

The seed has blocked four questions. **That is the seed telling you it is not representative, not a
gap to patch.** Fitting data to a demo is how this codebase got here. **Fix the test, not the data:**
`'Tech'` is not a LeadPlus industry (they are Manufacturing, Aerospace, Automotive, Chemicals,
Electronics, Pharma…), so the `'Tech' AND 'Enterprise'` prompt is rewritten to `Manufacturing` +
`Enterprise`.

## 9. Build order

| # | Change | Lines | Re-ingest? |
|---|---|---|---|
| 1 | **Intent triage + empty-chips guard** (§1) | ~30 | no |
| 2 | Location + `location_alias` (§3) | ~40 + seed | no |
| 3 | `TermGroup` + negation (§2) | ~50 | no |
| 4 | Fact filters (§4) | ~25 | no |
| 5 | `industries` + `industry_strict` (§5) | ~20 | no |
| 6 | Parser rules (§7) | ~50 | no — **required by 2–5** |
| 7 | Scoring: group coverage, industry max (§10) | ~20 | no |
| 8 | `contact_signal` role census (SEARCH-EXPLAINED §9) | new module | **yes — one embedding pass, ~$0.11** (skip was later reversed) |
| 9 | Zero-explainer (SEARCH-EXPLAINED §10) | ~120 | no |

**Items 1–7: ~half a day, no re-ingest, $0 LLM** — they ride joins that already exist. Items 8–9
(the contact census and zero-explainer) shipped afterwards; the census needs a one-time embedding
pass (~$0.11, no LLM), the zero-explainer needs neither.

## 10. Scoring changes (`score.py`)

- `coverage = matched_positive_groups / total_positive_groups`; negated groups never enter the denominator.
- A group matches if **any** `any_of` alternate matches.
- `industries` → multiplier = `max()` across positives. `industry_strict` → skip the multiplier; it's a filter.
- `Breakdown` exposes `matched_groups` / `unmatched_groups` (not flat terms) plus **`excluded_by`** —
  which negated group removed a company. **An exclusion the user cannot see is one they cannot trust.**

## 11. Out of scope — do not build

The 5 campaign/email/segment prompts belong to the Java campaign assistant. After §1 they refuse
*honestly* instead of returning garbage.

## 12. What has not changed

Every `ARCHITECTURE.md` §4 rule still governs. §1 adds a *classification* at the edge — nothing here
puts a model in the ranking path. No ANN index, no chunking, no Elasticsearch. Retrieve documents,
return companies. **Filter on facts, rank on fuzz** — §2.1/§3/§4/§5 *sharpen* that rule rather than
break it: negation, location, segments and NULL checks are facts; `industry` stays fuzzy and soft
unless the user says "strictly".

**Still true:** every weight in §8.2/§8.5 is untuned. ~~`golden.yaml` is machine-authored, and the
corpus is synthetic. None of this is validated until the `GRANT` lands and the real job postings are
in the index.~~

> **UPDATE — the real job postings are now in the index, and that sentence needs three
> corrections.** See `ARCHITECTURE.md` §0.
>
> * **The corpus is real.** A 1:1 clone of `leadplus_dev` replaced the synthetic seed. The `GRANT`
>   is no longer the blocker.
> * **`golden.yaml` is not merely machine-authored, it is DEAD.** Its labels are synthetic company
>   ids that now resolve to unrelated real companies. `scripts/eval.py` refuses to score it.
> * **"Validated once the real postings land" did not happen, and it is worth being precise about
>   why.** The postings landed and the v2 *behaviours* hold (refusal, determinism, canonical-only
>   negation — see `scripts/acceptance.py`). But **ranking quality is still unmeasured**, because
>   the labels died with the seed; and the §0 thesis is worse off than unmeasured — it is
>   currently **unprovable**, since every `posted_date` in the extractable corpus belongs to a
>   fabricated company and `recency` is 0.0 for every real one.
>
> The lesson §8 already stated, now with a second example: the seed blocked four questions and
> that was the seed telling you it was not representative. The real corpus is now telling you
> something else — that the field the thesis leans on hardest was never captured.
