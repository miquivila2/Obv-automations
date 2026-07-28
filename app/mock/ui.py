"""The mock-CRM test console: CRUD over fake meetings + a button that runs the
real pipeline against them.

Mounted at /mock by app/main.py, and ONLY when DATA_SOURCE=mock — these routes
write freely and run the chain without the webhook secret, which would be
unacceptable against production. The guard is in main.py, not here.

Intentionally one file with inline HTML: this is a test harness whose value is
iteration speed, not a product surface. No build step, no bundler, no assets.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/mock", tags=["mock"])


class MeetingPayload(BaseModel):
    """One fake CRM meeting. Mirrors the shape of public.events plus the fields
    the pipeline needs, with `transcript` standing in for Plaud's output."""

    id: str | None = None
    title: str = ""
    description: str = ""
    organizer: str = ""
    attendee_ids: list[str] = Field(default_factory=list)
    location: str = ""
    status: str = "completed"
    start_at: str | None = None
    end_at: str | None = None
    project_id: str | None = None
    language: str = "es"
    transcript: str = ""


def _store():
    from app.mock.store import get_mock_store

    return get_mock_store()


# --------------------------------------------------------------------------
# Meetings CRUD
# --------------------------------------------------------------------------
@router.get("/api/meetings")
async def list_meetings() -> dict:
    store = _store()
    return {"meetings": store.rows("public", "events"), "counts": store.counts()}


@router.post("/api/meetings")
async def create_meeting(payload: MeetingPayload) -> dict:
    store = _store()
    now = datetime.now(timezone.utc)
    row = payload.model_dump()
    row["id"] = row.get("id") or str(uuid.uuid4())
    row["start_at"] = row.get("start_at") or (now - timedelta(hours=1)).isoformat()
    row["end_at"] = row.get("end_at") or (now - timedelta(minutes=40)).isoformat()

    events = store.rows("public", "events")
    if any(e.get("id") == row["id"] for e in events):
        raise HTTPException(status_code=409, detail=f"meeting {row['id']} already exists")
    events.append(row)
    store.replace_table("public", "events", events)
    return {"meeting": row}


@router.put("/api/meetings/{event_id}")
async def update_meeting(event_id: str, payload: MeetingPayload) -> dict:
    store = _store()
    events = store.rows("public", "events")
    for i, existing in enumerate(events):
        if existing.get("id") == event_id:
            updated = {**existing, **payload.model_dump(exclude_none=True), "id": event_id}
            events[i] = updated
            store.replace_table("public", "events", events)
            return {"meeting": updated}
    raise HTTPException(status_code=404, detail=f"no meeting {event_id}")


@router.delete("/api/meetings/{event_id}")
async def delete_meeting(event_id: str) -> dict:
    store = _store()
    events = store.rows("public", "events")
    remaining = [e for e in events if e.get("id") != event_id]
    if len(remaining) == len(events):
        raise HTTPException(status_code=404, detail=f"no meeting {event_id}")
    store.replace_table("public", "events", remaining)
    return {"deleted": event_id}


# --------------------------------------------------------------------------
# Pipeline execution
# --------------------------------------------------------------------------
@router.post("/api/run/{event_id}")
async def run_one(event_id: str) -> dict:
    from app.mock.runner import run_pipeline_for_event

    trace = await run_pipeline_for_event(event_id)
    return trace.as_dict()


@router.post("/api/run-all")
async def run_all() -> dict:
    from app.mock.runner import run_pipeline_for_all

    store = _store()
    ids = [e["id"] for e in store.rows("public", "events")]
    traces = await run_pipeline_for_all(ids)
    return {
        "traces": [t.as_dict() for t in traces],
        "passed": sum(1 for t in traces if t.ok),
        "failed": sum(1 for t in traces if not t.ok),
    }


# --------------------------------------------------------------------------
# Database inspection / reset
# --------------------------------------------------------------------------
@router.get("/api/db")
async def inspect_db(schema: str | None = None, table: str | None = None) -> dict:
    store = _store()
    if schema and table:
        return {"schema": schema, "table": table, "rows": store.rows(schema, table)}
    return {"counts": store.counts()}


@router.post("/api/reset")
async def reset_db(seed: bool = True) -> dict:
    store = _store()
    store.reset()
    if seed:
        from app.mock.seed import seed_store

        seed_store(store)
    return {"reset": True, "counts": store.counts()}


@router.post("/api/generate")
async def generate_meetings(count: int = 5) -> dict:
    """Bulk-generate variants quickly, for volume/edge-case testing."""
    import random

    store = _store()
    events = store.rows("public", "events")
    projects = store.rows("public", "projects")
    now = datetime.now(timezone.utc)

    classes = [
        ("kickoff / onboarding", "Necesitan un sistema nuevo con varios roles de usuario y reportes mensuales."),
        ("budget follow-up", "El cliente pide revisar el presupuesto y reducir horas de QA."),
        ("progress update", "Progress check: ingestion done, charting half finished, timeline needs re-planning."),
        ("final QA", "Acceptance review. They also now want a mobile app that was never scoped."),
    ]

    created = []
    for _ in range(max(1, min(count, 50))):
        project = random.choice(projects) if projects else {"id": None}
        label, body = random.choice(classes)
        row = {
            "id": str(uuid.uuid4()),
            "title": f"{project.get('name', 'Unknown')} — {label}",
            "description": f"Auto-generated fixture ({label}).",
            "organizer": "generated@example.test",
            "attendee_ids": ["generated@example.test"],
            "location": random.choice(["Google Meet", "Zoom", "On-site", "Phone"]),
            "status": "completed",
            "start_at": (now - timedelta(hours=random.uniform(2, 48))).isoformat(),
            "end_at": (now - timedelta(hours=random.uniform(0.5, 1.9))).isoformat(),
            "project_id": project.get("id"),
            "language": random.choice(["es", "en"]),
            "transcript": body,
        }
        events.append(row)
        created.append(row)

    store.replace_table("public", "events", events)
    return {"created": len(created), "meetings": created}


# --------------------------------------------------------------------------
# The console
# --------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def console() -> str:
    return _HTML


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mock CRM — pipeline test console</title>
<style>
  :root {
    --bg:#0f1416; --panel:#161d20; --line:#2a3438; --ink:#e6ebec; --muted:#8fa0a6;
    --accent:#5fbacb; --ok:#5fc98a; --bad:#e8735c; --warn:#e0b055;
    --mono: ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.5 system-ui, sans-serif; }
  header { padding:14px 18px; border-bottom:1px solid var(--line); display:flex; gap:14px;
           align-items:center; flex-wrap:wrap; position:sticky; top:0; background:var(--bg); z-index:5; }
  h1 { font:600 15px var(--mono); margin:0; letter-spacing:.04em; }
  .grow { flex:1; }
  button { background:var(--panel); color:var(--ink); border:1px solid var(--line);
           padding:7px 12px; border-radius:4px; cursor:pointer; font:500 13px system-ui; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); color:#04191d; border-color:var(--accent); font-weight:600; }
  button.danger:hover { border-color:var(--bad); color:var(--bad); }
  main { display:grid; grid-template-columns:minmax(340px,1fr) minmax(420px,1.3fr); gap:0; align-items:start; }
  @media (max-width:900px){ main { grid-template-columns:1fr; } }
  section { padding:16px 18px; }
  section + section { border-left:1px solid var(--line); }
  h2 { font:600 11px var(--mono); letter-spacing:.12em; text-transform:uppercase;
       color:var(--accent); margin:0 0 12px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:5px;
          padding:11px 12px; margin-bottom:9px; }
  .card h3 { margin:0 0 4px; font-size:14px; font-weight:600; }
  .meta { font:11px var(--mono); color:var(--muted); word-break:break-all; }
  .row { display:flex; gap:6px; margin-top:9px; flex-wrap:wrap; }
  label { display:block; font:11px var(--mono); color:var(--muted); margin:9px 0 3px;
          letter-spacing:.06em; text-transform:uppercase; }
  input, textarea, select { width:100%; background:#0c1113; color:var(--ink);
          border:1px solid var(--line); border-radius:4px; padding:7px 9px; font:13px var(--mono); }
  textarea { min-height:96px; resize:vertical; }
  .trace { background:var(--panel); border:1px solid var(--line); border-left-width:3px;
           border-radius:5px; padding:11px 12px; margin-bottom:10px; }
  .trace.ok { border-left-color:var(--ok); }
  .trace.bad { border-left-color:var(--bad); }
  .badge { display:inline-block; font:10px var(--mono); padding:2px 6px; border-radius:3px;
           letter-spacing:.06em; }
  .badge.ok { background:#12331f; color:var(--ok); }
  .badge.bad { background:#3a1c15; color:var(--bad); }
  pre { background:#0c1113; border:1px solid var(--line); border-radius:4px; padding:9px;
        overflow-x:auto; font:11px/1.5 var(--mono); margin:8px 0 0; max-height:280px; }
  .logs { margin-top:8px; font:11px/1.6 var(--mono); max-height:300px; overflow-y:auto;
          background:#0c1113; border:1px solid var(--line); border-radius:4px; padding:8px; }
  .logs div { white-space:pre-wrap; word-break:break-word; }
  .lvl { display:inline-block; width:52px; color:var(--muted); }
  .INFO .lvl{color:var(--accent);} .WARNING .lvl{color:var(--warn);}
  .ERROR .lvl,.CRITICAL .lvl{color:var(--bad);}
  details summary { cursor:pointer; font:11px var(--mono); color:var(--muted); margin-top:8px; }
  .empty { color:var(--muted); font-style:italic; }
  .counts { font:11px var(--mono); color:var(--muted); }
</style>
</head>
<body>
<header>
  <h1>MOCK CRM — PIPELINE TEST CONSOLE</h1>
  <span class="counts" id="counts"></span>
  <span class="grow"></span>
  <button onclick="runAll()" class="primary">▶ Run pipeline on ALL</button>
  <button onclick="generate()">+ Generate 5</button>
  <button onclick="resetDb()" class="danger">Reset DB</button>
</header>

<main>
  <section>
    <h2>Fake meetings</h2>
    <div id="list"><p class="empty">loading…</p></div>

    <h2 style="margin-top:22px">New / edit meeting</h2>
    <div class="card">
      <input type="hidden" id="f_id">
      <label>Title</label><input id="f_title" placeholder="Client — kickoff">
      <label>Description</label><input id="f_description">
      <label>Organizer</label><input id="f_organizer" placeholder="ops@client.example">
      <label>Participants (comma separated)</label><input id="f_attendees" placeholder="a@x.com, b@y.com">
      <label>Location</label><input id="f_location" placeholder="Google Meet">
      <label>Status</label>
      <select id="f_status">
        <option>completed</option><option>scheduled</option><option>cancelled</option>
      </select>
      <label>Start / end (ISO, blank = auto past)</label>
      <div class="row">
        <input id="f_start" placeholder="auto" style="flex:1">
        <input id="f_end" placeholder="auto" style="flex:1">
      </div>
      <label>Project id (blank = let classification decide)</label><input id="f_project">
      <label>Language</label>
      <select id="f_language"><option value="es">es</option><option value="en">en</option></select>
      <label>Transcript (stands in for the Plaud recording)</label>
      <textarea id="f_transcript"></textarea>
      <div class="row">
        <button onclick="save()" class="primary">Save meeting</button>
        <button onclick="clearForm()">Clear</button>
      </div>
    </div>
  </section>

  <section>
    <h2>Pipeline traces</h2>
    <div id="traces"><p class="empty">No runs yet. Hit “Run pipeline” on a meeting.</p></div>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);
let meetings = [];

async function api(path, opts={}) {
  const res = await fetch('/mock/api' + path, {
    headers: {'Content-Type':'application/json'}, ...opts
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(res.status + ' ' + body);
  }
  return res.json();
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function load() {
  const data = await api('/meetings');
  meetings = data.meetings;
  $('counts').textContent = Object.entries(data.counts)
    .map(([k,v]) => k + '=' + v).join('  ');
  $('list').innerHTML = meetings.length ? meetings.map(m => `
    <div class="card">
      <h3>${esc(m.title) || '<em>untitled</em>'}</h3>
      <div class="meta">${esc(m.id)}</div>
      <div class="meta">${esc(m.language)} · ${esc(m.status)} · ${esc(m.location)} ·
        ${(m.attendee_ids||[]).length} participant(s) ·
        ${m.transcript ? m.transcript.length + ' chars' : '<span style="color:var(--warn)">empty transcript</span>'}</div>
      <div class="row">
        <button onclick="run('${m.id}')" class="primary">▶ Run pipeline</button>
        <button onclick="edit('${m.id}')">Edit</button>
        <button onclick="del('${m.id}')" class="danger">Delete</button>
      </div>
    </div>`).join('') : '<p class="empty">No meetings. Use “Generate 5” or add one below.</p>';
}

function formPayload() {
  return {
    id: $('f_id').value || null,
    title: $('f_title').value,
    description: $('f_description').value,
    organizer: $('f_organizer').value,
    attendee_ids: $('f_attendees').value.split(',').map(s=>s.trim()).filter(Boolean),
    location: $('f_location').value,
    status: $('f_status').value,
    start_at: $('f_start').value || null,
    end_at: $('f_end').value || null,
    project_id: $('f_project').value || null,
    language: $('f_language').value,
    transcript: $('f_transcript').value,
  };
}

async function save() {
  const p = formPayload();
  try {
    if (p.id && meetings.some(m => m.id === p.id)) {
      await api('/meetings/' + p.id, {method:'PUT', body: JSON.stringify(p)});
    } else {
      await api('/meetings', {method:'POST', body: JSON.stringify(p)});
    }
    clearForm(); load();
  } catch (e) { alert('Save failed: ' + e.message); }
}

function edit(id) {
  const m = meetings.find(x => x.id === id); if (!m) return;
  $('f_id').value = m.id; $('f_title').value = m.title || '';
  $('f_description').value = m.description || ''; $('f_organizer').value = m.organizer || '';
  $('f_attendees').value = (m.attendee_ids||[]).join(', ');
  $('f_location').value = m.location || ''; $('f_status').value = m.status || 'completed';
  $('f_start').value = m.start_at || ''; $('f_end').value = m.end_at || '';
  $('f_project').value = m.project_id || ''; $('f_language').value = m.language || 'es';
  $('f_transcript').value = m.transcript || '';
  window.scrollTo({top: document.body.scrollHeight, behavior:'smooth'});
}

function clearForm() {
  ['f_id','f_title','f_description','f_organizer','f_attendees','f_location',
   'f_start','f_end','f_project','f_transcript'].forEach(i => $(i).value = '');
}

async function del(id) {
  if (!confirm('Delete this meeting?')) return;
  await api('/meetings/' + id, {method:'DELETE'}); load();
}

async function generate() { await api('/generate?count=5', {method:'POST'}); load(); }

async function resetDb() {
  if (!confirm('Wipe the mock database and re-seed the defaults?')) return;
  await api('/reset', {method:'POST'});
  $('traces').innerHTML = '<p class="empty">Database reset.</p>';
  load();
}

function renderTrace(t) {
  const cls = t.ok ? 'ok' : 'bad';
  const logs = (t.logs||[]).map(l =>
    `<div class="${esc(l.level)}"><span class="lvl">${esc(l.level)}</span>` +
    `+${l.elapsed_ms}ms  ${esc(l.logger)}: ${esc(l.message)}</div>`).join('');
  const delta = [];
  for (const [k,v] of Object.entries(t.db_counts_after||{})) {
    const before = (t.db_counts_before||{})[k] || 0;
    if (v !== before) delta.push(`${k}: ${before} → ${v}`);
  }
  return `<div class="trace ${cls}">
    <span class="badge ${cls}">${t.ok ? 'PASS' : 'FAIL'}</span>
    <strong style="margin-left:8px">${esc(t.title)}</strong>
    <div class="meta">stage=${esc(t.stage)} · ${t.duration_ms}ms · ${esc(t.event_id)}</div>
    ${t.error ? `<pre style="color:var(--bad)">${esc(t.error)}</pre>` : ''}
    ${delta.length ? `<div class="meta" style="margin-top:6px">writes → ${esc(delta.join('   '))}</div>` : ''}
    <details><summary>result</summary><pre>${esc(JSON.stringify(t.result, null, 2))}</pre></details>
    ${t.traceback ? `<details><summary>traceback</summary><pre>${esc(t.traceback)}</pre></details>` : ''}
    <details open><summary>logs (${(t.logs||[]).length})</summary><div class="logs">${logs}</div></details>
  </div>`;
}

async function run(id) {
  $('traces').innerHTML = '<p class="empty">Running…</p>';
  try {
    const t = await api('/run/' + id, {method:'POST'});
    $('traces').innerHTML = renderTrace(t);
  } catch (e) {
    $('traces').innerHTML = `<pre style="color:var(--bad)">${esc(e.message)}</pre>`;
  }
  load();
}

async function runAll() {
  $('traces').innerHTML = '<p class="empty">Running all meetings…</p>';
  try {
    const r = await api('/run-all', {method:'POST'});
    $('traces').innerHTML =
      `<div class="meta" style="margin-bottom:10px">${r.passed} passed · ${r.failed} failed</div>` +
      r.traces.map(renderTrace).join('');
  } catch (e) {
    $('traces').innerHTML = `<pre style="color:var(--bad)">${esc(e.message)}</pre>`;
  }
  load();
}

load();
</script>
</body>
</html>
"""
