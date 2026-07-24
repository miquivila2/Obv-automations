# Local deployment (Ollama, no AWS)

Run the whole agent system on one machine, with open-weight models served locally
by **Ollama** — no AWS account, no per-token cost, fully private. This is the setup
we use while AWS is pending.

> This guide is written to run on the **target local machine**, not the dev laptop
> the code was written on. Follow it there.

---

## What "local" does and doesn't change

| Layer | Local setup | Notes |
|-------|-------------|-------|
| **Models** | Ollama, on this machine | Smaller local proxies of the production models — dev exercises the pipeline with real calls, it isn't meant to match production quality |
| **Database** | Still Supabase | Agents read the CRM tables and write to the `agent` schema. Use a **test Supabase environment** (branch/staging), not production — see step 4 |
| **Backend** | FastAPI on this machine | `uvicorn app.main:app` |

The model provider is the ONLY thing that changes vs. production: `MODEL_PROVIDER=ollama`
instead of `bedrock`. Nothing in the agent code changes.

---

## Prerequisites

- **Ollama** installed and running (`ollama serve`). https://ollama.com
- **Python 3.11+**.
- Enough RAM/VRAM for the chosen model. `qwen3:8b` needs ~6-8 GB; scale the model
  to the machine (see "Choosing a model").
- Access to a **test Supabase** (URL + service role key + Postgres connection string).

---

## Steps

### 1. Pull a local model
```bash
ollama pull qwen3:8b
```
`qwen3:8b` is the default: it supports tool-calling and structured output, which the
classifier and Judge need. Verify it's there:
```bash
ollama list
```

### 2. Install the project
```bash
git clone https://github.com/miquivila2/Obv-automations.git
cd Obv-automations
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[local,dev]"
```
The `local` extra pulls in `langchain-ollama`.

### 3. Configure `.env`
```bash
cp .env.example .env
```
Then edit `.env`:
```
MODEL_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b

SUPABASE_URL=https://<test-project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<test service role key>
SUPABASE_DB_URI=postgresql://postgres:<pw>@db.<test-project>.supabase.co:5432/postgres
```

### 4. Apply the agent-layer migration (test Supabase only)
> ⚠️ Decide the test environment first (Supabase branch / staging) — do NOT apply to
> the production CRM. The migration is additive and lives entirely in the `agent`
> schema (see `supabase/migrations/0001_agent_layer.sql`), but production is off-limits
> until we've validated locally.

Apply `supabase/migrations/0001_agent_layer.sql` to the test database (Supabase SQL
editor, or `psql "$SUPABASE_DB_URI" -f supabase/migrations/0001_agent_layer.sql`).

Also create a Storage bucket named **`budgets`** (Supabase → Storage) — the Budget
agent uploads the generated `.docx` there.

### 5. Verify everything is reachable
```bash
python -m app.healthcheck
```
Expect three `[PASS]` lines (config, Ollama + model, Supabase). Fix any `[FAIL]`
before continuing.

### 6. Run the API
```bash
uvicorn app.main:app --reload --port 8000
```
Smoke-test the health endpoint:
```bash
curl http://localhost:8000/health
```
Trigger Agent 1 with a sample meeting (the calendar-timer webhook; Plaud export is
manual for now, so the transcript is passed in the body):
```bash
curl -X POST http://localhost:8000/webhooks/calendar-timer \
  -H "Content-Type: application/json" \
  -d '{"event_id":"<a public.events id>","attendee_emails":["ops@client.mx"],"language":"es","transcript_text":"<meeting transcript>"}'
```

---

## Scheduling the calendar timer

Google Calendar sync already exists at the CRM level (`public.google_credentials`
+ `public.events`) — nothing to set up there. What needs scheduling is our own
poll that watches for meetings that just ended:

```bash
# every 5 minutes, hit the tick endpoint
curl -X POST http://localhost:8000/internal/calendar-timer/tick
```

Cron (Linux/macOS): `*/5 * * * * curl -s -X POST http://localhost:8000/internal/calendar-timer/tick`
Windows: a Task Scheduler task running that `curl` on a 5-minute trigger.

Today this will report due events as **failed** with a clear reason — transcript
fetching (Plaud) and attendee-email resolution aren't wired yet (see
`app/services/calendar_timer.py`). That's expected until Plaud's integration lands.

## Choosing a model

`OLLAMA_MODEL` is one model for every agent by default (simplest). Pick by machine:

| Machine | Suggested `OLLAMA_MODEL` |
|---------|--------------------------|
| Modest (8-16 GB RAM) | `qwen3:8b` |
| Strong (24 GB+ VRAM) | `qwen3:14b` or `qwen3:32b` |

Two things to know for later:
- **Wireframe (Agent 2) needs vision** (it reads whiteboard photos). When we build it,
  it will need a local vision model (e.g. `qwen2.5vl` / `llama3.2-vision`) rather than
  the text default — that's a per-agent override we'll add then.
- Per-agent model overrides aren't wired yet; today all agents share `OLLAMA_MODEL`.
  That's fine for exercising the pipeline.

---

## Going to production later

Flip `MODEL_PROVIDER=ollama` → `bedrock` and set AWS creds + region. The frontier
models in `app/config.py:MODEL_REGISTRY` take over per agent. No code changes.
