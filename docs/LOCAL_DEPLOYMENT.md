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

Apply every file in `supabase/migrations/` **in order** to the test database
(Supabase SQL editor, or `psql "$SUPABASE_DB_URI" -f <file>` for each):
`0001_agent_layer.sql`, `0002_gantt_task_ownership.sql`,
`0003_gantt_task_details.sql`, `0004_project_repos.sql`,
`0005_qa_findings.sql`, `0006_artifact_examples.sql`.

None of these is optional. `0005` backs Agent 8 (Final QA) and `0006` backs the
few-shot library that Agents 2, 5 and the Judge query on every run — a missing
table there fails the build loudly rather than degrading (docs §10).

**Then expose the `agent` schema**: Supabase → Settings → API → *Exposed schemas*
→ add `agent` → save. Supabase serves only `public` and `graphql_public` by
default, so until you do this **every** `agent.*` read and write in this codebase
fails. This is the single most likely first-run failure; step 5 checks for it
explicitly.

Also create a Storage bucket named **`budgets`** (Supabase → Storage) — the Budget
agent uploads the generated `.docx` there.

### 5. Verify everything is reachable
```bash
python -m app.healthcheck
```
It verifies config, the model provider, Supabase connectivity, that the `agent`
schema is exposed, that all six migrations landed, that every CRM column this
codebase reads or writes exists, and the `budgets` bucket. Fix any `[FAIL]`
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

If `WEBHOOK_SECRET` is set, add `-H "X-Webhook-Secret: <value>"` to that curl.

This tick fetches real transcripts from Plaud (see the next section for the
one-time setup) and resolves attendee emails under a documented assumption
about `public.events.attendee_ids` (docs §9.10) — nothing here needs Plaud
Developer Platform access anymore.

---

## One-time Plaud login (required before the timer can fetch transcripts)

`app/services/plaud_client.py` talks to **Plaud's own MCP server**
(`@plaud-ai/mcp`), not the Developer Platform API — no approval process, just
your own Plaud account. Its auth is a **browser OAuth login you do yourself,
once, on the machine that will run this backend.** This repo never sees your
Plaud password or token; there is nothing to type into a `.env` file.

**Prerequisite:** Node.js >= 20 (bundles `npx`). `python -m app.healthcheck`
checks this for you (see "Plaud (transcript fetching)" in its output).

**Do this once, on the machine that will run the calendar timer:**

```bash
npx -y @plaud-ai/mcp@latest install
```

This opens your browser, you sign in to Plaud, and a token is cached at
`~/.plaud/tokens-mcp.json`. Every later `POST /internal/calendar-timer/tick`
spawns the same MCP server as a subprocess and — per the working assumption
in docs §9 item 7 — reuses that cached token without opening a browser again.

**Why "the machine that runs this backend" matters:** the token lives on disk,
tied to whatever machine you ran `install` on. If the backend later moves to
a different host (docs §3.5, hosting is still undecided), that login has to
be redone there. Today, for local development, that machine is just this one.

**How a calendar event finds its Plaud recording:** Plaud has no idea what a
`public.events` row is, and vice versa — there's no shared id. `plaud_client.
find_recording_id` matches them by comparing the event's time window against
Plaud's recording list; if that's ambiguous (zero or several recordings
overlap), it raises rather than guessing, and that event shows up in the
tick's `failed` list with a clear reason instead of silently attaching the
wrong transcript.

---

## Wiring the artifact-changed webhook (Supabase)

The manual-edit re-trigger loop (docs §7) needs Supabase to call this app when a
human edits an artifact. The receiving endpoint exists; the Supabase side is
config, done once per environment (Supabase → Database → Webhooks → *Create a new
hook*), for each of these tables:

| Table | Schema |
|-------|--------|
| `project_plan_drafts` | `public` |
| `gantt_tasks` | `public` |
| `budget_line_items` | `public` |
| `wireframe_drafts` | `agent` |

For each hook:
- **Events**: `UPDATE` (and `INSERT` if human-created rows should also cascade)
- **Type**: HTTP Request → `POST` → `https://<your-host>/webhooks/artifact-changed`
- **HTTP Headers**: `Content-Type: application/json` and `X-Webhook-Secret: <your WEBHOOK_SECRET>`

The endpoint acts only on `source='human'` rows and ignores `source='agent'` ones,
so an agent write can never re-trigger the chain into a loop.

> ⚠️ `localhost` is not reachable from Supabase's servers. For local testing,
> expose the port with a tunnel (`ngrok http 8000`) and use that URL in the hook.
> Backend hosting for a real deployment is still undecided (docs §3.5).

---

## Linking a project to its GitHub repo (`agent.project_repos`)

The Orchestrator's **update mode** (docs §9.3) reads real build progress from
GitHub. It finds the repo through `agent.project_repos`, which maps one CRM project
to one repo. **Nothing in the codebase ever writes to this table** — rows are added
by hand, once per project. That's deliberate rather than a missing feature: which
repo a project lives in is an operational fact nobody can infer from a meeting
transcript.

Why it isn't a `github_repo` column on `public.projects`, which would be the obvious
place: the production-safety contract (docs §3.4, §9.3) forbids adding *anything* to
the CRM's `public` schema — additions count, not just alterations. So the link lives
in our own `agent` schema instead.

Add a row via Supabase → Table Editor → schema `agent` → `project_repos` → *Insert
row*, or in the SQL editor:

```sql
insert into agent.project_repos (project_id, owner, repo)
values ('00000000-0000-0000-0000-000000000000', 'miquivila2', 'Obv-automations');
```

- `project_id` — the `public.projects.id` uuid of the CRM project.
- `owner` / `repo` — split from `github.com/<owner>/<repo>`. The bare repo name
  only: no URL, no `.git` suffix.

`project_id` is `unique`, so a second insert for the same project fails; use
`update` to repoint a project at a different repo.

**Until a project has a row, update mode fails.** `fetch_code_progress_snapshot`
raises a `ValueError` naming the project and this table. That's intentional (docs
§10, "fail loud"): an empty progress summary would flow into the Gantt re-sync
looking exactly like "no work has been done".

**`GITHUB_TOKEN` (optional)** — a read-only PAT, set in `.env` (see `app/config.py`).
Without it the GitHub calls go out unauthenticated, which works fine for public
repos but at a much lower rate limit (60 requests/hour per IP, vs. 5,000
authenticated). A private repo requires the token.

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
