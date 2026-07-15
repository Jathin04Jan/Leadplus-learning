# CONTEXT — LeadPlus Learning Course (session continuity)

Read this first if you are resuming the course, or if you are a **fresh AI/Claude Code session**
picking this up. It records what this repo is, the teaching contract, where the real code lives,
and exactly how to continue where we left off.

---

## 1. What this repo is
A **step-by-step architecture course** teaching the **LeadPlus** platform, authored by Claude Code
for **Jathin** — a software architect + developer who is joining the LeadPlus project and wants a
complete mental model of the platform before working on it.

- The course itself lives in [`README.md`](./README.md) — one growing document, taught as numbered
  **Steps** (Step 1, Step 2, …). It renders as the repo homepage on GitHub.
- [`ISSUES.md`](./ISSUES.md) — a **living register of problems/gaps/tech-debt** found in the platform.
  Append to it whenever a new issue surfaces while teaching.
- This `CONTEXT.md` is meta: it tracks progress and how to resume. It is **not** course content.

## 2. Teaching contract (how the course is delivered)
- Teach **one Step at a time, in depth**, then **stop and invite follow-up questions**. Advance to
  the next Step only when the learner asks (they say "next", or ask a question first).
- Audience is a **software architect + developer** — be technical and precise. Use concrete file
  paths, class names, and code references from the real codebase. **Verify claims against the live
  code before asserting** (the platform has drift and migration gaps; don't teach from memory alone).
- After finishing a Step: append it to `README.md`, update the **Progress** table below, then
  **commit + push** (see §6).

## 3. Where the REAL code lives (what we're teaching)
- The LeadPlus codebase is on **this Ubuntu VM at `/home/jathin/Corelabs`**, branch **`main`**.
  It is a separate git repo (GitHub `Jathin04Jan/Corelabs`) — this learning repo only holds notes.
- Structure there:
  - `Leadplus-corelabs/leadplus-service` — Java 21 / Spring Boot 3.5 **modular-monolith** backend
    (11 modules under `src/main/java/ai/leadplus/`; note the package root is `ai.leadplus`, NOT `com.leadplus`).
  - `Leadplus-corelabs/leadplus-portal` — Next.js 16 / React 19 frontend (static export).
  - `Limark/` — a **frozen** legacy backup; never modify. Useful only for diffing/reference.
  - `Docs/` — architecture decisions, audits, SCHEMA.md, run guide, known issues.
- Deep prior analysis of the platform is stored in **Claude Code memory** at
  `/home/jathin/.claude/projects/-home-jathin-Corelabs/memory/` — read `MEMORY.md` (the index) and
  the `leadplus-*.md` notes for grounding (boundary enforcement, doc/code drift, security gaps,
  local run setup, the Limark-migration repair history).

## 4. Key platform facts (grounding, so a fresh session isn't starting cold)
- Two halves, one backend: **LeadGen engine** (search→campaign→outreach→tracking) + **RFQ marketplace**
  (buyer/vendor/rfq). Multi-tenant; `tenant.modules` gates which half a tenant sees.
- **11 modules** in 3 areas: Portal (`auth`, `portal/buyer`, `portal/vendor`, `portal/rfq`),
  LeadGen (`leadgen/search`, `leadgen/campaign`, `leadgen/outreach`, `leadgen/tracking`),
  Shared (`shared/workspace`, `shared/admin`, `shared/ai`).
- **Boundary rule (core idea):** a module never imports another module's `Service`/`Repository`/`Client`/`Entity`;
  cross-module calls go through the target's public `<Name>Module` facade or via published events.
  Enforced by `ModuleBoundariesTest` (fails the build on violations).
- Postgres, **no FK constraints**; schema is external — Hibernate `ddl-auto: validate` against
  `src/main/resources/schema.sql`. The app does NOT create its own tables.
- Build = **Gradle** (not Maven). Container via **jib**. Deploy = AWS ECS. JDK 21 at
  `/home/jathin/jdk/jdk-21.0.11+10`. Local Postgres = docker container `leadplus-pg` (db/user/pass all `leadplus`).
- Recent history: Anjali's "Limark migration" (PRs #41/#42) added new features (contact add/import,
  AI column-mapping, data-source tagging, email preview, campaign summaries) but broke `main`'s build;
  it was repaired by PRs #44/#43/#45. `main` is currently green and boots. (See the memory note
  `leadplus-main-broken-limark-migration.md`.)

## 5. Progress tracker
Update this every time a Step is written.

| Step | Title | Status |
|------|-------|--------|
| 1 | Big picture — domain, deployables, tech, architectural style, request trace | ✅ written |
| 2 | Modular-monolith architecture — modules, `*Module` facades, boundary rule & enforcement, events, cycles | ✅ written |
| 3 | Identity & multi-tenancy — auth (JWT/login/signup/OAuth) + workspace (tenant→workspace→user) | ✅ written |
| 4 | LeadGen engine — search → campaign → outreach → tracking | ✅ written |
| 5 | RFQ marketplace — buyer / vendor / rfq | ✅ written |
| 6 | AI module — AIServicesModule, prompts, chat memory, Python seam | ✅ written |
| 7 | Cross-cutting — security chains, exceptions, timezones, events, schedulers, infra | ✅ written |
| 8 | Frontend — Next.js structure, token/auth flow, module gating, data-fetching | ✅ written |
| 9 | Build / run / deploy — Gradle, schema/validate, CI/CD, config & secrets | ✅ written |
| 10 | Migration context — refactor, phases, Java-now/Python-later, known issues | ⬜ |

**Current position:** Steps 1–9 written & pushed. **Next action = teach Step 10 (Migration context: modular refactor, phases, Java-now/Python-later, known issues) — the FINAL step.**

## 6. How to resume (do this in a fresh session)
1. Read this `CONTEXT.md` and `README.md` to see how far the course got (Progress table + last Step).
2. Read the memory notes at `/home/jathin/.claude/projects/-home-jathin-Corelabs/memory/` for grounding.
3. Open the real code at `/home/jathin/Corelabs` (branch `main`) and **verify before teaching**.
4. Continue from the next `⬜` Step. Teach it in depth (per §2), then stop for questions.

### Workflow for each new Step
```bash
# 1. Append the Step's content to README.md (in the learning repo).
# 2. Update the Progress table in CONTEXT.md (mark the Step ✅, set "Current position").
# 3. If the Step surfaced any problems/gaps, append them to ISSUES.md.
cd ~/leadplus-learning
git add -A
git commit -m "course: add Step N — <title>"
git push
```

> Keep `ISSUES.md` current: any bug, risk, smell, or gap found while teaching gets a row there
> (next sequential ID, severity, status, location). It's the platform's tech-debt register.

## 7. Repo / environment notes
- This learning repo: local `~/leadplus-learning`, remote `https://github.com/Jathin04Jan/Leadplus-learning.git`, branch `main`.
- Auth for pushing uses Jathin's GitHub PAT (same token as the Corelabs remote on this VM).
- Keep this repo separate from `/home/jathin/Corelabs` — never mix course notes into the product repo.
