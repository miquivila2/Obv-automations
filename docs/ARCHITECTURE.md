# Architecture — Oblivion Multi-Agent Build Automation

> Master document. If you're going to touch the code, read this first. It captures
> the **why** of every decision; the code captures the **how**. When the two
> disagree, one of them is wrong — fix it, don't ignore it.

---

## 1. What we're building (the objective)

Turn **one client meeting into four project deliverables** — wireframe, build plan,
Gantt and budget — automatically, with no manual work beyond review and editing.

It's not a single program. It's a **team of 7 specialized agents**, each with a
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
2. **Plaud** — the meeting transcript. **It's a manual export today** ("Developer
   Platform JSON later", per the original doc). There is no Plaud API integration
   yet: `ingestion.export_plaud_note` is a stub that receives the already-exported
   transcript. Once the API exists, only that stub changes — nothing downstream.

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
| **final_qa** | Acceptance stage | ⚠️ No owning agent yet — see §9 Open questions |

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

### 5.1. Routing is NOT an LLM
Deliberate decision: mapping class→agent is a **table**, not a judgment. The
Orchestrator does it in deterministic code (testable, free, reproducible). The
Orchestrator's LLM is used only for the **progress summary in update mode**, which
does require reasoning about real code vs. plan.

### 5.2. Budget arithmetic in code, not in the LLM
Agent 5 generates the **line items** (hours, rate, justification) but the sums,
contingency and currency conversion are computed in Python. An LLM getting a
multiplication wrong in a document that reaches the client is a real, avoidable
risk.

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
2. ⚠️ **Final QA** — Agent 1 can classify "final_qa" but there's no owning agent.
   An Agent 8 (QA/acceptance), or just update status + generate handover docs?
3. **Progress inspection (update mode)** — GitHub commits/PRs, an issue list in the
   Axo #54–#82 style, or a status file the team maintains? Stub in
   `app/services/github_progress.py` until decided.
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
7. **Plaud integration** — blocked on Developer Platform access (`dev.plaud.ai`
   "Contact Us" → approval → `PLAUD_CLIENT_ID`/`PLAUD_API_KEY` via `portal.plaud.ai`).
   Until then, `export_plaud_note` stays a stub (manually-pasted transcript) and
   the calendar timer (§ below) reports due events as failed for this reason.
8. **`project_plan_drafts.status` vocabulary** — the CRM table has no documented
   enum; we write `'draft'` as an assumption (see `app/services/plan_persist.py`).
   Verify against real CRM data / the Lovable app's own status values before
   relying on it in production.
9. **Gantt task dependencies** — `gantt_persist.build_gantt_rows` uses a simplified
   single sequential chain (each task depends on the one directly before it across
   the whole flattened list) as a deliberate placeholder. Real parallel/independent
   tasks need a human to refine `depends_on` in the CRM, or a smarter agent later.
10. **Calendar timer → Agent 1 handoff** — `app/services/calendar_timer.py` detects
    due meetings (Google Calendar sync already exists at the CRM level) but
    `process_due_event` intentionally raises `NotImplementedError`: it needs (a)
    Plaud's transcript (item 7) and (b) confirmation of what `public.events.
    attendee_ids` actually contains (email strings? team_member uuids?) before
    resolving attendee emails for classification — not guessed at.

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
