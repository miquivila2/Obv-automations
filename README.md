# Obv-automations

**Oblivion Multi-Agent Build Automation** — eight specialized AI agents that turn a
client meeting into four project deliverables (wireframe, build plan, Gantt, budget),
automatically, plus a read-only Final QA scope check at acceptance time.

> New here? Read **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** first. It's the
> master document: objective, data flow, classification logic, model choices, and the
> reasoning behind every decision. This README is just how to run it.

---

## What it does, in one picture

```
meeting ──30min──▶ [1 Meeting Notes] ─▶ [7 Orchestrator] ─▶ [2 Wireframe] ─▶ [3 Planner] ─▶ [4 Gantt] ─▶ [5 Budget]
                   export + classify     route (code)        └──── each one passes through [6 Judge] (max 2 rounds) ────┘
```

- **Agent 1** ingests the meeting (Plaud transcript + Google Calendar metadata) and
  classifies it: which project, what kind of meeting.
- **Agent 7 (Orchestrator)** routes — deterministic code, not an LLM — to the right
  entry point and mode (create / follow-up / update).
- **Agents 2–5** build the four artifacts in a linear chain, each reading the previous
  one's output from Supabase.
- **Agent 6 (Judge)** reviews every artifact before it's written (max 2 rounds, then
  flagged for human review).
- **Agent 8 (Final QA)** runs outside this chain: when a meeting is classified
  `final_qa`, it compares the notes against the agreed plan and notifies a human
  (via `agent.qa_findings`) only if the client is asking for something beyond it —
  it never writes to the CRM or changes anything.

## Models

All eight agents run **open-weight models on AWS Bedrock on-demand** (pay-per-token, no
idle GPU). The registry — which model runs which agent, and why — is the single source
of truth in [`app/config.py`](app/config.py) → `MODEL_REGISTRY`. Strategy and tradeoffs
are explained in [ARCHITECTURE §5](docs/ARCHITECTURE.md).

## Tech stack

Python · FastAPI · LangGraph (build graph + Postgres checkpointer) · Supabase
(Postgres, the CRM) · AWS Bedrock (`langchain-aws`).

## Layout

```
app/
  config.py            # settings + MODEL_REGISTRY (single source of truth for models)
  main.py              # FastAPI: the push triggers (all behind X-Webhook-Secret)
  db/                  # Supabase client + LangGraph checkpointer (two separate connections)
  services/            # ingestion, classification, persistence, model factory,
                       # github progress, run tracking, calendar timer, plaud client
  graph/
    state.py           # BuildState threaded through the graph
    build.py           # graph assembly + Judge-loop edges + agent.runs tracking
    nodes/             # orchestrator, judge, and the 4 builder nodes
supabase/migrations/   # 0001-0004, applied in order — the schema, commented
docs/ARCHITECTURE.md   # the master document
docs/LOCAL_DEPLOYMENT.md  # running locally with Ollama + wiring the Supabase webhook
tests/                 # 90 tests: routing, classification, judge loop + feedback,
                       # persistence (wireframe/plan/gantt/budget), docx, github
                       # progress, run tracking, calendar timer, HTTP endpoints
```

## Run it

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env      # then fill in Supabase + AWS values

# 3. Apply the schema to your Supabase project, in order
#    (Supabase SQL editor or psql): every file in supabase/migrations/

# 4. Verify the 7 models are enabled in your region (see docs §9.5)
aws bedrock list-foundation-models --region us-east-1 --query "modelSummaries[].modelId"

# 5. Serve
uvicorn app.main:app --reload

# 6. Tests
pytest
```

Running locally without AWS (Ollama) and wiring the Supabase webhook:
see [`docs/LOCAL_DEPLOYMENT.md`](docs/LOCAL_DEPLOYMENT.md).

## Status

The build chain runs end to end (Agents 1–7, Judge loop, persistence to the CRM
and the `agent` schema), plus Agent 8 (Final QA). Remaining gaps, all tracked
in [ARCHITECTURE §9](docs/ARCHITECTURE.md):

- **Plaud transcript fetch** (§9.7) — implemented via Plaud's own MCP server
  (`app/services/plaud_client.py`); the OAuth token-reuse assumption is
  verified. Only unverified piece left: `public.events.start_at`, needed for
  the calendar-event/recording time-window match, against production data.
- **`events.attendee_ids` shape** (§9.10) — resolved under a documented
  assumption (email strings); verify against production data.
- **`project_plan_drafts.status` vocabulary** (§9.8) — we write `'draft'` as an
  assumption; confirm against the real CRM.
- **Supabase Database Webhook** (§11 gap #4) — no code left to write; the setup
  steps are in `docs/LOCAL_DEPLOYMENT.md`.
- **Backend hosting** (§3.5) — undecided.
