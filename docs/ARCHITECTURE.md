# Architecture — Oblivion Multi-Agent Build Automation

> Master document. If you're going to touch the code, read this first. It captures
> the **why** of every decision; the code captures the **how**. When the two
> disagree, one of them is wrong — fix it, don't ignore it.

---

## 1. What we're building (the objective)

Turn **one client meeting into four project deliverables** — wireframe, build plan,
Gantt and budget — automatically, with no manual work beyond review and editing.

It's not a single program. It's a **team of 8 specialized agents**, each with a
narrow job, coordinated by an orchestrator and kept honest by a shared judge. The
core idea (from the original whiteboard): an agent that does one thing well is
easier to reason about, test and replace than a mono-agent that does everything
adequately.

**Expected real volume:** 10–25 projects per year (from Oblivion's own market
report). This constrains EVERY infrastructure decision: we do not design for
"thousands of concurrent executions", we design for a **bursty, low-volume** load
(one meeting triggers a burst of ~18 LLM calls, then silence until the next
meeting). Over-engineering here is an anti-pattern, not a virtue.

---

## 2. The cast of agents

| # | Agent | Job in one line | Triggered by | Then triggers |
|---|-------|-----------------|--------------|---------------|
| 1 | **Meeting Notes** | Export the Plaud note, classify it (project + class) | Timer, 30 min after meeting | 7 |
| 7 | **Orchestrator** | Decide who runs and in what mode | 1 | 2, 3, 4 or 5 |
| 2 | **Wireframe** | Build an editable wireframe from the notes | 7 (or start of chain) | 3 |
| 3 | **Planner** | List needs (SW/HW/cloud) + ordered plan | 2 or 7 | 4 |
| 4 | **Gantt** | Monthly milestones + tasks + Gantt | 3 or 7 | 5 |
| 5 | **Budget** | Justified, priced budget (.docx) | 4 or 7 | — / back to 7 |
| 6 | **Judge** | Review & approve any artifact (shared) | 2, 3, 4, 5 | back to caller |
| 8 | **Final QA** | Detect scope switches in acceptance meetings vs. the agreed plan; notify a human only — never writes to the CRM | 1 (class=`final_qa`) | — (read-only finding, nothing downstream) |

Agent 8 is deliberately outside the build chain and the graph: it does not go
through the Orchestrator's routing table, the Judge loop, or LangGraph at all
— `app/main.py` calls it directly, the same way it already special-cased
`final_qa` before an owning agent existed. See §9.2.

**The build chain is linear and sequential** — there's no real parallelism to
exploit, because each agent depends on the previous one's artifact: the budget
needs the Gantt, the Gantt needs the plan, the plan needs the wireframe. We state
this explicitly so nobody chases a nonexistent parallelism later.

```
   meeting ──30min──▶ [1 MEETING NOTES] ── export + classify (project, class)
                              │
                              ▼
                      [7 ORCHESTRATOR] ── route by class; mode: create/follow-up/update
                       │    │    │    │
        onboarding →   ▼    ▼    ▼    ▼   ← follow-up/update enter directly at the owning agent
                    [2]──▶[3]──▶[4]──▶[5]
                    wire  plan  gantt budget
                     └─────┴──▶ [6 JUDGE] ◀──┴─────┘  (each artifact, max 2 rounds)

   manual edit of wireframe → re-run 3   |   manual edit of Gantt → re-run 5
```

---

## 3. Where the data comes from and how it's transformed (data flow)

This is the heart of the system. Follow the data end to end:

### 3.1. Input
1. **Google Calendar** — a meeting event. From it: end time (fires the 30-min
   timer), attendee list (emails → used for deterministic project matching), and
   implicit language.
2. **Plaud** — the meeting transcript. Two paths, both real (§9 item 7):
   - **Automatic** (calendar timer): `app/services/plaud_client.py` fetches it
     from Plaud's own MCP server (`@plaud-ai/mcp`), no manual step.
   - **Manual** (direct webhook call): `ingestion.export_plaud_note` still
     just echoes an already-obtained transcript handed in directly — for
     callers that already have one. Both paths converge on the same
     `ingest_meeting` call; nothing downstream cares which one supplied it.

### 3.2. Transformation (Agent 1)
The raw transcript + calendar metadata become a **classified meeting record**:
`project_id`, `class`, `sub_type`, `language`, and the text stored in `raw_notes`.
See §4 for the classification detail.

### 3.3. Artifact production (Agents 2–5)
Each agent reads what it needs, generates its artifact, passes it through the Judge,
and persists it. **Critically: the CRM already models plans, Gantts and budgets** —
so agents write into the CRM's existing tables, they do NOT invent parallel ones.
The wireframe is the only artifact the CRM has no table for, so it lives in our
`agent` schema. Sources and destinations per agent:

| Agent | Reads from | Writes to |
|-------|------------|-----------|
| 2 Wireframe | `agent.meeting_intake` + past-wireframe library (few-shot) + whiteboard photo if any | `agent.wireframe_drafts` (new) |
| 3 Planner | intake + latest wireframe | **`public.project_plan_drafts`** (existing CRM table: `payload`, `warnings`, `approved_at`) |
| 4 Gantt | latest plan draft | **`public.gantt_tasks`** (existing CRM table; `source_draft_id` links back to the plan draft) |
| 5 Budget | latest Gantt + past-budget library + rates | **`public.budget_line_items`** (existing CRM table: `quantity`/`unit_rate`/`amount`/`source`) + a `.docx` in Storage |

Rates are read from `public.projects` (`hourly_rate`, `currency`, `preferred_currency`)
and `public.team_members` (`day_rate`) — the CRM already holds them, so there is no
separate `rate_config` table.

### 3.4. Persistence & CRM integration
The Supabase database is **owned by the live Lovable CRM** (oblivionlabs.lovable.app),
in the `public` schema. Our design treats that as sacred:

- **We reuse the CRM's tables** (`projects`, `clients`, `events`, `project_plan_drafts`,
  `gantt_tasks`, `budget_line_items`, `tasks`, `team_members`) by reading/writing them
  from app code (`app/db/client.py`, PostgREST + service role key).
- **Everything we add lives in a dedicated `agent` schema** — `meeting_intake`,
  `wireframe_drafts`, `artifact_feedback`, `code_progress`, `runs`, `project_matchers`.
  See `supabase/migrations/0001_agent_layer.sql`.
- **Graph state** (LangGraph checkpoints) → `app/db/checkpointer.py`, its own
  `langgraph` schema.

**Production-safety contract (non-negotiable):**
1. Nothing is ever added to, altered in, or dropped from `public`. Our tables are in
   `agent`; the CRM schema is left untouched.
2. No foreign key points *into* a CRM table — a real FK could block a CRM
   delete/update. Cross-references (event_id, project_id) are plain uuid columns,
   enforced in app code. FKs exist only among `agent.*` tables.
3. Agents *insert new rows* into CRM artifact tables (that's the product's purpose),
   always in a `draft` / pending-approval state — a human approves in the CRM before
   anything is final. They never modify or delete existing CRM data.

### 3.5. How this plugs into the actual CRM app (not just its database)

The CRM (Lovable, React) and the agent system (this repo, Python) are **two
separate applications that never call each other directly** — they only meet at
Supabase, the database both share:

```
   Lovable (CRM, React)          Agent system (this repo, FastAPI)
          │                                  │
          └───────────────┬──────────────────┘
                           ▼
                      Supabase (one DB)
```

**Why Plan/Gantt/Budget need zero CRM changes:** agents write into the CRM's own
tables (`project_plan_drafts`, `gantt_tasks`, `budget_line_items`). The moment a
row is inserted, the Lovable UI shows it on its next read — no code in Lovable has
to change for those three artifact types. The integration is at the data layer,
not the UI layer.

**Why the Wireframe is different:** it lives in `agent.wireframe_drafts`, a table
the CRM has never heard of and never queries. Today there is **no screen anywhere**
where a human can see or edit an agent-generated wireframe.

Decided (this session):
- **Wireframe UI**: deferred. When built, it must be a screen **inside the CRM
  itself** (not a separate external tool) — built via Lovable directly (the user
  builds it with Lovable prompts; this repo does not touch Lovable's code). Blocked
  today on Lovable access/tokens.
- **Backend hosting**: undecided/deferred. Whatever calls this repo's FastAPI app
  (the calendar-timer scheduler, a future CRM button) needs it reachable somewhere
  — local-with-Ollama is not reachable from a cloud-hosted CRM unless exposed.
  Revisit once ready for real deployment.
- **Triggering**: **automatic only** for now (calendar timer + the artifact-changed
  webhook). No manual "regenerate" button inside the CRM yet — that would require
  Lovable calling `POST /orchestrator/run`, plus an auth mechanism between the two
  apps, neither of which is built. Revisit if/when manual triggering is wanted.

---

## 4. Classification: how and by what criteria (Agent 1)

Classification answers **two questions** about each meeting:

### 4.1. Which project does it belong to?
Two-stage entity resolution, **cheapest first** (`app/services/classification.py`):

1. **Deterministic match — no LLM.** Cross the calendar attendees' emails against
   `projects.attendee_emails`, and the title/notes against `projects.aliases`
   (+ the project name). If **exactly one** project matches → done, confidence 1.0,
   zero cost, zero non-determinism.
2. **LLM fallback — only if step 1 yields 0 or >1 candidates.** GLM-4.7-Flash
   receives the list of active projects + the meeting excerpt and returns a
   structured classification.

> **Key design criterion:** we never auto-create a new project from this flow. A
> mis-transcribed client name creating a duplicate project is worse than one extra
> item in the review queue. If nothing matches, a name is suggested and it goes to
> human review.

### 4.2. What class of meeting is it?
A 4-class taxonomy (mutually exclusive in the current model). These are the criteria
the Orchestrator later uses to route:

| Class | Criterion | What it triggers |
|-------|-----------|------------------|
| **onboarding** | New project | Full chain from Agent 2 (create mode): 2→3→4→5 |
| **follow_up** | An existing artifact needs revising (`sub_type` says which) | Jump to the owning agent in follow-up mode, then re-flow downstream |
| **update** | Progress update on a live build | Orchestrator inspects the repo, compares vs. plan, and triggers what's needed (usually 4) |
| **final_qa** | Acceptance stage | Agent 8: read-only scope-switch check against the agreed plan; notifies a human via `agent.qa_findings` if the client is asking for something beyond it — see §9.2 |

### 4.3. Confidence threshold
A result is only auto-applied if `confidence >= classification_confidence_threshold`
(default 0.70, in `app/config.py`). Below that, the meeting stays
`status='pending_review'` — this is the review queue the original whiteboard assumed
but never specified.

---

## 5. Model registry (all open-weight, via AWS Bedrock)

**Authoritative source of truth: `app/config.py` → `MODEL_REGISTRY`.** Don't
duplicate the full table here so it can't drift; this section explains the
*strategy*, the code holds the exact IDs and the reasoning for each one.

- **Hosting:** AWS Bedrock **on-demand** (pay-per-token, serverless, Project Mantle
  inference layer). Chosen over a self-hosted GPU (EC2/SageMaker) because the load
  is bursty: a reserved GPU would sit idle >95% of the time. Bedrock meets the
  accepted privacy guarantee (AWS doesn't train on your data, everything stays in
  your account/region).
- **Strategy:** specialize. A capable model where an error propagates (Planner,
  Judge); a cheap model where the task is mechanical (classification,
  transformation).

| Agent | Model | Why (summary) |
|-------|-------|---------------|
| 1 Meeting Notes | `zai.glm-4.7-flash` | Trivial classification; cheapest in the catalog |
| 7 Orchestrator (update) | `minimax.minimax-m2.1` | Only the progress summary; routing is code, not LLM |
| 2 Wireframe | `moonshotai.kimi-k2.5` | Native vision (whiteboard photos) + JSON + tool-calling; cheaper AND better than Qwen3-VL |
| 3 Planner | `deepseek.v3.2` | Multi-step reasoning; its errors cascade |
| 4 Gantt | `qwen.qwen3-next-80b-a3b-instruct` | Structured transformation; cheap |
| 5 Budget | `qwen.qwen3-next-80b-a3b-instruct` | Long context for few-shot; arithmetic in code, not LLM |
| 6 Judge | `moonshotai.kimi-k2-thinking` | Different lineage than the builders so it doesn't share blind spots |
| 8 Final QA | `deepseek.v3.2` | Same tier as Planner — missing a real scope switch ships it unnoticed; asymmetric risk vs. a false positive |

### 5.1. Routing is NOT an LLM
Deliberate decision: mapping class→agent is a **table**, not a judgment. The
Orchestrator does it in deterministic code (testable, free, reproducible). The
Orchestrator's LLM is used only for the **progress summary in update mode**, which
does require reasoning about real code vs. plan.

### 5.2. Budget arithmetic in code, not in the LLM
Agent 5 generates the **structure** of each line (category, description, hours,
tier, justification); every **number** is computed in Python. An LLM getting a
multiplication wrong in a document that reaches the client is a real, avoidable
risk — so the model never does arithmetic.

What the code actually computes today is deliberately small:
- `budget_math.price_line_items` — `amount = hours × unit_rate` per line, and the
  subtotal. `Decimal` with half-up rounding, no floats in the math. An unknown tier
  raises `KeyError` rather than silently pricing the work at zero (§10, fail loud).
- `budget_docx.render_budget_docx` — groups the priced lines by month and emits a
  per-month subtotal plus a grand total. Presentation only; it adds no new math.

And what it deliberately does **not** do:
- **No IVA and no discounts** — locked decision, a human adds those in the CRM.
- **No contingency.** Nothing anywhere computes one.
- **No currency conversion.** `rates.resolve_currency` only *picks* a currency label
  (USD for English meetings, MXN for Spanish, overridden by the project's
  `preferred_currency`); it never converts an amount between currencies, and no
  other code does either. A budget is priced in one currency, start to finish.
- **One rate tier.** `rates.resolve_rates` returns a tier→rate dict but populates
  only `'standard'`, from `projects.hourly_rate`. The dict shape means adding tiers
  later is additive and doesn't change callers.

The last three differ from the budget format the original spec says we reproduce.
That was examined and **settled in favour of the current format** — see §9.12.

### 5.3. Judge/Wireframe overlap (accepted tradeoff)
The Judge (Kimi K2 Thinking) shares a lab with the Wireframe (Kimi K2.5). The
general rule is "Judge of a different lineage than the builder" so they don't share
blind spots. The overlap is accepted **only here** because the wireframe Judge
evaluates only **JSON structure vs. notes**, never the visual render — so the shared
vision lineage isn't exercised in that review. Decision taken explicitly; documented
in case the Judge's role ever changes.

---

## 6. Two patterns the builder agents share

### 6.1. The Judge loop
Agents 2–5 never write straight to the CRM. Each one: generates a draft → submits it
to the Judge with the source notes and examples → receives `APPROVE` or actionable
feedback → revises → resubmits, **max 2 rounds**. After round 2:
- If it approved at some point → the approved version is written.
- If it never approved → the artifact is marked `status='needs_human_review'`, it is
  **not** silently accepted (ADR decision — see §8). The graph `interrupt()`s and
  waits for human intervention.

Built **once** as a shared helper called by all four builders — Lean: don't repeat
the `while` with a counter in each agent.

Key rule: **don't over-feedback**. If the draft is already good, approve it cleanly
instead of inventing changes.

### 6.2. create / follow-up / update modes
Each builder has a mode:
- **create** — build fresh from the notes (onboarding).
- **follow-up** — load the latest saved version, diff it against what the notes ask,
  change only that.
- **update** — like follow-up, but the Orchestrator has already inspected the repo
  and passes in a progress summary (real build vs. planned), so the agent updates
  against reality, not just the notes.

---

## 7. Manual-edit re-trigger

Artifacts land in the CRM as editable. When a human edits one, the downstream
artifact is now stale and the chain must re-flow:
- Human edits wireframe → re-triggers Agent 3 → which re-triggers 4 → 5.
- Human edits Gantt/tasks → re-triggers Agent 5.

This needs two things from the data model, both already in the schema:
1. Every artifact is **versioned**.
2. Each version records `source` (`agent`/`human`). **Only** a `source='human'`
   version fires the re-trigger — an agent write must never trigger itself into a
   loop.

**Trigger transport:** Supabase **Database Webhooks** (Postgres trigger → HTTP POST
to FastAPI). It's push, not polling: no cycles spent asking "is there anything new?"
and no 24/7 worker watching the table. Chosen over a dedicated queue
(Redis/RabbitMQ) because at this volume the queue is infrastructure with no benefit.

---

## 8. Architecture decisions (ADR summary)

| Decision | Chosen | Alternatives rejected | Reason |
|----------|--------|-----------------------|--------|
| Model hosting | Bedrock on-demand | Self-hosted GPU (EC2/SageMaker); local | Bursty load: reserved GPU idle >95% |
| Inter-agent transport | Supabase DB Webhooks | Dedicated queue; polling; direct call | Push with no new infra, proportional to volume |
| Orchestrator routing | Deterministic rules | All-LLM | Reproducible, testable, free |
| Judge after 2 rounds w/o approval | `needs_human_review` | Silently accept "best version" | Don't send the client something the system rated insufficient |
| Budget arithmetic | Code | LLM | Avoid calculation errors in a client document |
| Orchestration framework | LangGraph | Hand-rolled orchestration; Temporal | Judge loop = native cyclic graph; checkpoints + interrupt() for free |
| Backend | Python (FastAPI) | Node/TS; Go | Mature boto3/langchain-aws; LLM ecosystem |
| CRM data model | Reuse CRM tables + `agent` schema for gaps | Own parallel tables; separate DB | CRM already models plan/gantt/budget; a separate DB can't populate them anyway |
| Cross-refs to CRM tables | Plain uuid columns, no FK | Real FKs | A FK into a CRM table could block a CRM delete → production risk |

---

## 9. Open questions (blocking ones marked ⚠️)

1. ~~**Wireframe tooling**~~ **DECIDED (Session 5):** renders in the CRM as structured
   JSON, persisted to `agent.wireframe_drafts` (the CRM has no table for this artifact
   type). Schema: `{"screens": [{"name", "purpose", "components": [...], "visible_to_roles":
   [...], "navigates_to": [...]}]}` — see `app/graph/nodes/wireframe.py:WireframeDraft`.
2. ~~**Final QA**~~ **DECIDED & IMPLEMENTED (this session): Agent 8, scoped
   deliberately narrow.** Not an acceptance/handover agent — it does one thing:
   when Agent 1 classifies a meeting `final_qa`, compare what the client asks
   for against the ORIGINAL agreed plan (`public.project_plan_drafts`, Agent
   3's needs+phases — chosen over the wireframe or the raw onboarding
   transcript because it's already the structured scope baseline Gantt/Budget
   are built from downstream of). If the notes ask for something materially
   different from or beyond that plan, it writes a finding to
   `agent.qa_findings` (migration `0005_qa_findings.sql`) for a human to
   review. It does **not** update any status, generate handover docs, or
   touch any `public.*` row — a clean Final QA meeting produces no row at
   all, nothing to notify. See `app/services/qa_check.py`.

   Deliberately outside the graph: Agent 8 doesn't go through the
   Orchestrator's routing table, the Judge loop, or LangGraph — `app/main.py`
   calls `run_final_qa_check` directly from both `POST /webhooks/calendar-timer`
   and `POST /orchestrator/run`, the same place that used to short-circuit to
   `final_qa_unhandled`. `orchestrator.route()` still raises
   `NotImplementedError` for `final_qa` as a defensive fallback (docs §10
   "fail loud") for a caller that somehow reaches it without going through
   those two entry points — that path is unchanged and still tested
   (`tests/test_routing.py`).
3. ~~**Progress inspection (update mode)**~~ **DECIDED & IMPLEMENTED:** GitHub
   commits/PRs via the REST API, chosen over an issue-list convention or a
   hand-maintained status file because it needs zero process discipline from the
   team. `app/services/github_progress.py` fetches recent commits + PRs and
   persists each snapshot to `agent.code_progress` (`source_ref` = head sha).
   The project→repo link lives in `agent.project_repos` (migration 0004) — NOT a
   new column on `public.projects`, per the no-touch-`public` contract. Auth is
   an optional read-only PAT (`GITHUB_TOKEN`); update mode raises a clear error
   if a project has no repo row rather than summarizing nothing.
4. **Example libraries** — where do past wireframes/budgets live and how are the
   "best" ones tagged so the few-shot uses good references, not just recent ones?
   NOTE: client examples (e.g. the Axo Capital budget) are CONFIDENTIAL — never
   commit them to this public repo (see `.gitignore`); they belong in Supabase
   Storage or a private location.
5. **Model-in-region verification** — confirm the 7 model IDs are enabled in
   `AWS_REGION` with `aws bedrock list-foundation-models`, and that `langchain-aws`
   talks to them via the Converse API. First real technical step.
6. ~~**Re-trigger cascade** — total or scoped?~~ **DECIDED:** full, automatic cascade
   (editing the wireframe regenerates plan→Gantt→budget with no intervention).
   Accepted risk: it can silently change a budget already shared with the client —
   mitigate operationally (don't edit wireframes of projects with a budget already
   sent without knowing). See `circuit breaker` as a future improvement: a per-project
   flag to pause automation.
7. ~~**Plaud integration** — blocked on Developer Platform access~~ **DECIDED
   & IMPLEMENTED (Session 6): use Plaud's own MCP server instead.** Plaud ships
   `@plaud-ai/mcp` (docs.plaud.ai/plaud-mcp-cli/mcp) — installable by any Plaud
   user, no `dev.plaud.ai` "Contact Us" approval needed. `app/services/
   plaud_client.py` runs it as a local subprocess (`npx -y @plaud-ai/mcp@latest`,
   stdio transport) and calls its tools directly via the official `mcp` Python
   client SDK — no LLM in that loop, a plain tool call.

   Two things this unblocked but did not eliminate:
   - ~~**ASSUMPTION TO VERIFY: OAuth token reuse.**~~ **VERIFIED (this
     session):** after one interactive login (`npx -y @plaud-ai/mcp@latest
     install`, browser OAuth, token cached at `~/.plaud/tokens-mcp.json`), a
     later *non-interactive* headless call to `_call_tool("list_files", {})`
     succeeded in ~9s with no browser prompt and returned real recording data.
     One login does leave a token every later subprocess call can reuse — the
     symptom described below (hang waiting for a browser) is not what
     happens in practice. Still bounded by `_CALL_TIMEOUT_SECONDS` as a
     defensive fallback in case the token ever expires and the server
     re-prompts.
   - **The matching problem (new — not anticipated by the original spec).** A
     Plaud recording carries no reference to a CRM `public.events` row; nothing
     links "this calendar event" to "this Plaud recording". `find_recording_id`
     resolves it by TIME WINDOW OVERLAP against `list_files`' date filters —
     the only correlation Plaud's tool surface exposes — using the same rule as
     classification's deterministic project match (§4.1): exactly one
     candidate → use it; zero or more than one → raise, never guess. This also
     means `public.events` needs a `start_at` column (mirroring the
     already-used `end_at`) — itself an unverified ASSUMPTION, tracked
     alongside `attendee_ids` in item 10 below.

   `export_plaud_note` (Agent 1's manual-webhook path, `app/services/
   ingestion.py`) is unaffected — it still takes an already-obtained transcript
   directly, for callers that already have one (e.g. pasted by hand). Plaud's
   MCP path is specifically for the *automatic* calendar-timer trigger, which
   starts with no transcript at all.
8. **`project_plan_drafts.status` vocabulary** — the CRM table has no documented
   enum; we write `'draft'` as an assumption (see `app/services/plan_persist.py`).
   Verify against real CRM data / the Lovable app's own status values before
   relying on it in production.
9. ~~**Gantt task dependencies**~~ **DECIDED (gap #1/#2 fix, this session):**
   `gantt_persist.plan_gantt_upsert` still uses a simplified single sequential
   chain (each task depends on the one directly before it) — real parallel/
   independent tasks need a human to refine `depends_on` in the CRM. What's new:
   regeneration now UPDATEs/DELETEs the agent's own prior rows (tracked in
   `agent.gantt_task_ownership`, since `gantt_tasks` has no source column) instead
   of leaving duplicate rows behind — see §5 and `0002_gantt_task_ownership.sql`.
   A human-created/edited row (no ownership record) is never touched.
10. ~~**Calendar timer → Agent 1 handoff**~~ **DECIDED & IMPLEMENTED (Session
    6): fully wired end to end**, under three documented ASSUMPTIONS, none of
    them blockers, all **verify against production data**:
    - `public.events.attendee_ids` holds email strings
      (`calendar_timer._resolve_attendee_emails`). If they're `team_members`
      uuids instead, that one function becomes an id→email lookup and nothing
      else changes.
    - `public.events` has a `start_at` column mirroring the already-used
      `end_at` — needed to window-match the event against Plaud recordings
      (item 7, `find_recording_id`).
    - `language` has no per-project source on this automatic path yet and
      defaults to `'es'` (same fallback the artifact-changed re-trigger uses,
      `app/main.py`).

    All three are checked by `python -m app.healthcheck`, which reports the
    observed shape of `attendee_ids` and will fail loudly on a missing
    `start_at` column.
11. **Audit findings (post-Session 5 review against the original spec doc)** —
    tracked status of the four gaps found:
    - ~~**Gap #1: source-tracking for regeneration**~~ **DECIDED & FIXED** —
      see item 9. `agent.gantt_task_ownership` (Gantt) + `source='agent'`
      (Budget, already existed) let regeneration UPDATE/DELETE only the agent's
      own rows, never a human's.
    - ~~**Gap #2: Budget had no follow-up mode**~~ **FIXED** — `budget.py` now
      loads current agent-authored lines and revises via the same upsert.
    - ~~**Gap #3: Gantt tasks have no description field**~~ **FIXED** —
      `agent.gantt_task_details` (0003) holds the description per gantt_task_id,
      since `public.gantt_tasks` has no column for one and we don't alter CRM
      tables. Same lifecycle as `gantt_task_ownership`: written/deleted alongside
      the matching CRM row, never touching a row the agent doesn't own.
    - ~~**Gap #4: Supabase Database Webhook is unconfigured**~~ **DOCUMENTED,
      pending execution** — the exact per-table setup steps are now in
      `docs/LOCAL_DEPLOYMENT.md` ("Wiring the artifact-changed webhook").
      It remains a deployment task the user performs in the Supabase dashboard;
      there is no code left to write. Related: every trigger endpoint now
      requires an `X-Webhook-Secret` header matching `WEBHOOK_SECRET` — these
      endpoints write to the production CRM and spend model tokens, so a
      publicly reachable unauthenticated URL was not acceptable.
12. ~~**Budget format vs. the original spec (FLOWSIGHT / Axo Capital)**~~ **DECIDED
    (Session 6): the current, simpler format is the one we want.** The spec
    (`Oblivion_MultiAgent_Structure_Plan.docx`, §3 "Agent 5 — Budget", the "Reuse"
    row) describes the format we reproduce as "two-tier hourly rates, monthly
    subtotals, contingency, market comparison, milestone payments" (wording verified
    against the source document). Measured against what is actually built (§5.2):
    - **Monthly subtotals** — implemented. `budget_docx.py` groups lines by month and
      emits a per-month subtotal row, plus a grand total.
    - **Two-tier hourly rates** — partial. `rates.resolve_rates` returns a tier→rate
      dict, but populates only `'standard'`, because the CRM stores a single
      `projects.hourly_rate`. The shape is ready; the second tier has no source of
      truth yet.
    - **Contingency, market comparison, milestone payments** — **not implemented at
      all.** No code computes a contingency, compares rates against a market
      reference, or emits a payment schedule.

    **The resolution:** hours × rate with monthly subtotals is the format we ship.
    Contingency, market comparison and milestone payments are **not** going to be
    built — each would need an input nobody holds today (a contingency percentage, a
    market-rate reference, milestone payment terms), and the simpler document is what
    is actually wanted. The gap was real; the answer is that the spec's "Reuse" row
    over-promises, not that the code under-delivers.

    Consequence to honour: **the spec is now the stale one, not the code.** If that
    "Reuse" row is ever used as a requirements source again, it should be read as
    describing the historical Axo document, not this system's output.

    **Not to be confused with the IVA/discount decision.** IVA and discounts are
    absent *on purpose* (locked: a human adds them in the CRM, see §5.2). The items
    above were absent because the question had never been asked; it has now been
    asked and answered. Neither decision reopens the other.

### 3.6. Pending Lovable-side work (tracked here so it isn't lost)

This repo never touches Lovable's code — anything requiring a CRM screen has to
be built by the user, in Lovable. This is a **private, internal CRM**: no
approval-workflow ceremony is wanted (no "pending review" states, no "human must
approve" gates, no source-flip-on-edit mechanics). Default assumption is
**nothing needs building** — agent-written rows in `agent.wireframe_drafts` /
`agent.meeting_intake` can be viewed via Supabase's own Table Editor when needed,
zero Lovable work required. If something in either table doesn't look right, it
gets edited directly like any other CRM data — no special workflow.

- **Wireframe screen** — optional convenience, not a blocker. Deferred (blocked
  on Lovable access/tokens anyway). If/when built, it should be a screen *inside*
  the CRM itself (docs §3.5), via Lovable prompts — but the Table Editor already
  covers "can a human see it" today.
- **General rule going forward**: only flag a Lovable-facing need to the user
  when it's a real functional blocker, not a nice-to-have or a formal workflow
  addition. Don't propose approval states, review-queue screens, or similar
  ceremony unless the user asks for it.

---

## 10. Code principles (Lean Coding)

How code is written here, non-negotiable:

- **Single source of truth.** Model IDs live only in `MODEL_REGISTRY`. The schema
  lives only in the SQL migration. Don't duplicate either in prose that will drift.
- **Low coupling.** Each agent is a node that reads/writes Supabase and doesn't know
  the others' implementation. External integrations (Plaud, GitHub) are stubs with a
  stable interface: swap the stub, not the caller.
- **Fail loud.** `model_id_for` raises KeyError on a typo instead of silently falling
  back to a default model. Idempotency via DB constraint, not scattered checks.
- **No speculative complexity.** No Kafka/K8s/multi-tenant/CQRS at 10–25 projects/
  year. Add it when volume justifies it, not before.
- **The why is documented.** Every non-obvious decision carries a comment pointing
  back to this doc. If something looks odd, there's probably a reason — it's written
  down.
