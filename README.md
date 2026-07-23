# Obv-automations

**Oblivion Multi-Agent Build Automation** — seven specialized AI agents that turn a
client meeting into four project deliverables (wireframe, build plan, Gantt, budget),
automatically.

> New here? Read **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** first. It's the
> master document: objective, data flow, classification logic, model choices, and the
> reasoning behind every decision. This README is just how to run it.

---

## What it does, in one picture

```
reunión ──30min──▶ [1 Meeting Notes] ─▶ [7 Orchestrator] ─▶ [2 Wireframe] ─▶ [3 Planner] ─▶ [4 Gantt] ─▶ [5 Budget]
                    export + clasifica    enruta (código)      └──── cada uno pasa por [6 Judge] (máx 2 rondas) ────┘
```

- **Agent 1** ingests the meeting (Plaud transcript + Google Calendar metadata) and
  classifies it: which project, what kind of meeting.
- **Agent 7 (Orchestrator)** routes — deterministic code, not an LLM — to the right
  entry point and mode (create / follow-up / update).
- **Agents 2–5** build the four artifacts in a linear chain, each reading the previous
  one's output from Supabase.
- **Agent 6 (Judge)** reviews every artifact before it's written (max 2 rounds, then
  flagged for human review).

## Models

All seven agents run **open-weight models on AWS Bedrock on-demand** (pay-per-token, no
idle GPU). The registry — which model runs which agent, and why — is the single source
of truth in [`app/config.py`](app/config.py) → `MODEL_REGISTRY`. Strategy and tradeoffs
are explained in [ARCHITECTURE §5](docs/ARCHITECTURE.md#5-registro-de-modelos-todos-open-weight-vía-aws-bedrock).

## Tech stack

Python · FastAPI · LangGraph (build graph + Postgres checkpointer) · Supabase
(Postgres, the CRM) · AWS Bedrock (`langchain-aws`).

## Layout

```
app/
  config.py            # settings + MODEL_REGISTRY (single source of truth for models)
  main.py              # FastAPI: the 3 push triggers
  db/                  # Supabase client + LangGraph checkpointer (two separate connections)
  services/            # ingestion, classification, artifacts, bedrock factory, github stub
  graph/
    state.py           # BuildState threaded through the graph
    build.py           # graph assembly + the Judge-loop edge mechanics
    nodes/             # orchestrator, judge, and the 4 builder nodes
supabase/migrations/   # 0001_init_schema.sql — the full schema, commented
docs/ARCHITECTURE.md   # the master document
tests/                 # classification + routing
```

## Run it

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env      # then fill in Supabase + AWS values

# 3. Apply the schema to your Supabase project
#    (via the Supabase SQL editor or CLI): supabase/migrations/0001_init_schema.sql

# 4. Verify the 7 models are enabled in your region (see docs §9.5)
aws bedrock list-foundation-models --region us-east-1 --query "modelSummaries[].modelId"

# 5. Serve
uvicorn app.main:app --reload

# 6. Tests
pytest
```

## Status

Skeleton per the build order in [ARCHITECTURE §8/§9](docs/ARCHITECTURE.md). Known stubs,
all documented in §9: Plaud export (manual for now), GitHub progress inspection, the exact
wireframe JSON schema, the Final QA agent, and the .docx rendering step for the budget.
