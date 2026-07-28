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
    events = store.rows("public", "events")
    # Flag which meetings Agent 1 already ingested (event_id is UNIQUE), so the
    # console can warn BEFORE a click, not just explain an empty result after —
    # this is the single most confusing thing about the harness otherwise:
    # clicking "Run pipeline" again on an already-processed meeting looks like
    # nothing works, when idempotency is doing exactly its job.
    processed_ids = {i.get("event_id") for i in store.rows("agent", "meeting_intake")}
    for event in events:
        event["_already_processed"] = event.get("id") in processed_ids
    return {"meetings": events, "counts": store.counts()}


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
# Budget document — Axo Capital format (discounts/IVA/contingency/market/milestones)
# --------------------------------------------------------------------------
class BudgetExtrasPayload(BaseModel):
    """Human-entered fields only. discount_pct_by_month and milestones' part_pct
    always start at 0 from the agent (docs §5.2) — this is where a human sets
    the real numbers, mirroring what would eventually be an editable form in
    the CRM itself."""

    discount_pct_by_month: dict[str, float] = Field(default_factory=dict)
    contingency_pct: float | None = None
    milestones: list[dict] = Field(default_factory=list)


def _assembled_budget_document(project_id: str) -> dict:
    from app.services.budget_persist import load_assembled_budget_document

    document = load_assembled_budget_document(project_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"no budget document for project {project_id}")
    return document


@router.get("/api/budget/{project_id}/document")
async def get_budget_document(project_id: str) -> dict:
    return _assembled_budget_document(project_id)


@router.get("/api/budget/{project_id}/pdf")
async def get_budget_pdf(project_id: str):
    from fastapi.responses import Response

    from app.services.budget_pdf import render_budget_pdf

    document = _assembled_budget_document(project_id)
    pdf_bytes = render_budget_pdf(project_name=document["project_name"], document=document)
    filename = f"{document['project_name'].replace(' ', '_')}_budget.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.put("/api/budget/{project_id}/extras")
async def update_budget_extras(project_id: str, payload: BudgetExtrasPayload) -> dict:
    """The human-edit surface: set discount %, contingency %, milestone splits.
    Agent 5 (persist_budget) always carries these forward unchanged on
    regeneration — this is the only place they're meant to be set."""
    store = _store()
    rows = store.rows("agent", "budget_documents")
    existing = next((r for r in rows if r.get("project_id") == project_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no budget document for project {project_id}")

    updated = {
        **existing,
        "discount_pct_by_month": payload.discount_pct_by_month,
        "contingency_pct": payload.contingency_pct,
        "milestones": payload.milestones,
    }
    rows = [updated if r.get("project_id") == project_id else r for r in rows]
    store.replace_table("agent", "budget_documents", rows)
    return {"updated": True, "document": _assembled_budget_document(project_id)}


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

    # Dialogue-formatted ("Speaker: line") like the seed fixtures, not a single
    # narrative paragraph — this is what the console renders under each
    # meeting's "transcript" details, and what the classifier stub actually
    # scans for keywords.
    classes = [
        ("kickoff / onboarding", (
            "Client: We need a new system with several user roles and monthly reports.\n"
            "Miquel (Oblivion): Got it, let's scope that out."
        )),
        ("budget follow-up", (
            "Client: Can we revisit the budget? We'd like to reduce QA hours.\n"
            "Miquel (Oblivion): Sure, let's see what we can trim."
        )),
        ("progress update", (
            "Miquel (Oblivion): How's the sprint going?\n"
            "Client: Ingestion is done, charting is half finished, and the timeline needs "
            "re-planning."
        )),
        ("final QA", (
            "Miquel (Oblivion): Let's do the acceptance review.\n"
            "Client: Looks good — but we'd also like a mobile app that was never scoped."
        )),
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


# Raw string, deliberately: this HTML block embeds JS regex literals (\n, \s,
# etc.) that must reach the browser as literal backslash-sequences. A normal
# triple-quoted string lets PYTHON's own escape processing consume them first
# (e.g. \n silently becomes a real newline, breaking a `/\n+/` regex into an
# unterminated one) — found the hard way when the console's transcript
# rendering broke with no visible error. `r"""..."""` makes every backslash
# in here pass through untouched, so this class of bug can't recur.
_HTML = r"""<!doctype html>
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

  /* ---------- Wireframe screen mockups ---------- */
  .screens { display:flex; flex-wrap:wrap; gap:12px; margin-top:10px; }
  .screen-mock { width:168px; background:#0c1113; border:1px solid var(--line);
                 border-radius:6px; overflow:hidden; }
  .screen-mock .titlebar { background:var(--accent); color:#04191d; font:600 10px var(--mono);
                 padding:5px 8px; letter-spacing:.03em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .screen-mock .body { padding:8px; display:flex; flex-direction:column; gap:5px; min-height:110px; }
  .screen-mock .comp { background:var(--panel); border:1px solid var(--line); border-radius:3px;
                 padding:4px 6px; font:10px system-ui; color:var(--ink); }
  .screen-mock .purpose { font:10px/1.4 system-ui; color:var(--muted); margin:0 0 2px; }
  .screen-mock .roles { display:flex; flex-wrap:wrap; gap:3px; padding:0 8px 8px; }
  .screen-mock .role-chip { font:9px var(--mono); background:var(--accent-soft); color:var(--accent);
                 padding:1px 5px; border-radius:8px; }
  .screen-mock .nav { font:9px var(--mono); color:var(--muted); padding:0 8px 8px; }

  /* ---------- Dialogue-formatted transcript ---------- */
  .dialogue { font:12px/1.6 var(--mono); background:#0c1113; border:1px solid var(--line);
                 border-radius:4px; padding:8px 9px; max-height:160px; overflow-y:auto; }
  .dialogue .turn { margin-bottom:3px; }
  .dialogue .speaker { color:var(--accent); font-weight:600; }

  /* ---------- Plan / Gantt / Budget deliverable panels ---------- */
  .deliverable { margin-top:10px; }
  .deliverable > summary { font:600 11px var(--mono); letter-spacing:.05em; text-transform:uppercase;
                 color:var(--accent); cursor:pointer; padding:3px 0; }
  .phase { background:#0c1113; border:1px solid var(--line); border-radius:4px;
                 padding:8px 10px; margin-top:6px; }
  .phase .pname { font-weight:600; font-size:12.5px; margin-bottom:4px; }
  .phase ul { margin:0; padding-left:18px; font-size:12px; color:var(--muted); }
  .needs-row { display:flex; gap:16px; margin-top:6px; font:11px var(--mono); color:var(--muted); flex-wrap:wrap; }
  .needs-row b { color:var(--ink); }
  table.dtable { width:100%; border-collapse:collapse; margin-top:6px; font-size:12px; }
  table.dtable th { text-align:left; font:10px var(--mono); text-transform:uppercase;
                 letter-spacing:.04em; color:var(--muted); border-bottom:1px solid var(--line-strong, var(--line));
                 padding:4px 6px; }
  table.dtable td { padding:5px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
  table.dtable td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .budget-total { text-align:right; font:600 13px var(--mono); margin-top:6px; color:var(--ink); }
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
    <div id="form_mode_banner" style="display:none; background:var(--flag-soft, #3a2318); border:1px solid var(--warn);
         border-radius:4px; padding:8px 10px; margin-bottom:10px; font-size:12.5px;"></div>
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
      <label>Transcript (stands in for the Plaud recording) — dialogue format, one "Speaker: line" per line</label>
      <textarea id="f_transcript" placeholder="Client: We need a portal with role-based login.&#10;Miquel (Oblivion): Got it, tell me more about the roles."></textarea>
      <div class="row">
        <button onclick="save()" class="primary" id="save_btn">Save meeting</button>
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

function dialogueHtml(transcript) {
  if (!transcript) return '<p class="empty" style="margin:6px 0 0">empty transcript</p>';
  // Turns are "Speaker: text" lines (see app/mock/seed.py). Anything that
  // doesn't match a "Name: " prefix is shown as a plain continuation line
  // rather than dropped, so free-form transcripts still render.
  const turns = transcript.split(/\n+/).filter(Boolean).map(line => {
    const m = line.match(/^([^:]{1,32}):\s*(.*)$/);
    return m
      ? `<div class="turn"><span class="speaker">${esc(m[1])}:</span> ${esc(m[2])}</div>`
      : `<div class="turn">${esc(line)}</div>`;
  });
  return `<div class="dialogue">${turns.join('')}</div>`;
}

async function load() {
  const data = await api('/meetings');
  meetings = data.meetings;
  $('counts').textContent = Object.entries(data.counts)
    .map(([k,v]) => k + '=' + v).join('  ');
  $('list').innerHTML = meetings.length ? meetings.map(m => `
    <div class="card">
      <h3>${esc(m.title) || '<em>untitled</em>'}
        ${m._already_processed ? '<span class="badge ok" style="margin-left:6px; vertical-align:2px;">already run</span>' : ''}
      </h3>
      <div class="meta">${esc(m.id)}</div>
      <div class="meta">${esc(m.language)} · ${esc(m.status)} · ${esc(m.location)} ·
        ${(m.attendee_ids||[]).length} participant(s) ·
        ${m.transcript ? m.transcript.length + ' chars' : '<span style="color:var(--warn)">empty transcript</span>'}</div>
      ${m._already_processed ? '<div class="meta" style="color:var(--accent); margin-top:2px;">Re-running will report "already ingested" — idempotency, not a bug. Reset DB or generate new ones to see fresh output.</div>' : ''}
      <details style="margin-top:8px"><summary>transcript</summary>${dialogueHtml(m.transcript)}</details>
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
  showFormMode('edit', m.title);
  window.scrollTo({top: document.body.scrollHeight, behavior:'smooth'});
}

function showFormMode(mode, title) {
  const banner = $('form_mode_banner');
  const btn = $('save_btn');
  if (mode === 'edit') {
    banner.style.display = 'block';
    banner.innerHTML = `✎ <b>Editing "${esc(title||'')}"</b> — Save will UPDATE this same meeting ` +
      `(same event id, so re-running it will report "already ingested"). ` +
      `Want a NEW meeting instead? <button onclick="duplicateAsNew()" style="margin-left:4px">Duplicate as new</button>`;
    btn.textContent = 'Update meeting';
  } else {
    banner.style.display = 'none';
    btn.textContent = 'Save meeting';
  }
}

function duplicateAsNew() {
  // The one-click fix for the exact trap this banner exists to prevent:
  // editing an existing meeting as a template but forgetting its id is still
  // attached, so "Save" would silently overwrite the original instead of
  // creating a new one. This just drops the id so the next Save POSTs fresh.
  $('f_id').value = '';
  showFormMode('create');
  alert('This will now save as a brand-new meeting (fresh id) instead of updating the original.');
}

function clearForm() {
  ['f_id','f_title','f_description','f_organizer','f_attendees','f_location',
   'f_start','f_end','f_project','f_transcript'].forEach(i => $(i).value = '');
  showFormMode('create');
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

function screenMocks(screens) {
  if (!screens || !screens.length) return '';
  const cards = screens.map(s => `
    <div class="screen-mock">
      <div class="titlebar">${esc(s.name)}</div>
      <div class="body">
        <p class="purpose">${esc(s.purpose||'')}</p>
        ${(s.components||[]).map(c => `<div class="comp">${esc(c)}</div>`).join('')}
      </div>
      ${(s.visible_to_roles||[]).length ? `<div class="roles">${
        s.visible_to_roles.map(r => `<span class="role-chip">${esc(r)}</span>`).join('')}</div>` : ''}
      ${(s.navigates_to||[]).length ? `<div class="nav">→ ${esc(s.navigates_to.join(', '))}</div>` : ''}
    </div>`).join('');
  return `<details open style="margin-top:10px">
    <summary>wireframe — ${screens.length} screen(s)</summary>
    <div class="screens">${cards}</div>
  </details>`;
}

function planPanel(plan) {
  if (!plan || !plan.phases) return '';
  const needs = plan.needs || {};
  const phases = plan.phases.map(p => `
    <div class="phase">
      <div class="pname">${esc(p.name)}</div>
      <ul>${(p.items||[]).map(i => `<li>${esc(i)}</li>`).join('')}</ul>
    </div>`).join('');
  return `<details open class="deliverable"><summary>plan — ${plan.phases.length} phase(s)</summary>
    <div class="needs-row">
      <span><b>Software:</b> ${esc((needs.software||[]).join(', ') || '—')}</span>
      <span><b>Hardware:</b> ${esc((needs.hardware||[]).join(', ') || '—')}</span>
      <span><b>Cloud:</b> ${esc((needs.cloud||[]).join(', ') || '—')}</span>
    </div>
    ${phases}
  </details>`;
}

function ganttPanel(tasks) {
  if (!tasks || !tasks.length) return '';
  const rows = tasks.map(t => `
    <tr>
      <td>${esc(t.phase)}</td>
      <td>${esc(t.name)}</td>
      <td class="num">${esc(t.duration_days)}d</td>
      <td>${esc((t.depends_on||[]).length)} dep(s)</td>
    </tr>`).join('');
  return `<details open class="deliverable"><summary>gantt — ${tasks.length} task(s)</summary>
    <table class="dtable">
      <thead><tr><th>Phase</th><th>Task</th><th>Duration</th><th>Depends on</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </details>`;
}

function budgetPanel(doc, projectId) {
  if (!doc || !doc.months) return '';
  const currency = doc.currency;

  const monthBlocks = doc.months.map(m => {
    const rows = m.lines.map(l => `
      <tr>
        <td>${esc(l.category||'')}</td>
        <td>${esc(l.description||'')}<div class="meta">${esc(l.justification||l.details||'')}</div></td>
        <td class="num">${esc(l.hours)}h</td>
        <td class="num">${esc(l.unit_rate)}</td>
        <td class="num">${esc(l.amount)}</td>
      </tr>`).join('');
    return `<div class="phase" style="margin-top:10px">
      <div class="pname">${esc(m.month)}</div>
      <table class="dtable">
        <thead><tr><th>Category</th><th>Description</th><th>Hours</th><th>Rate</th><th>Cost</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="needs-row">
        <span><b>Subtotal:</b> ${esc(m.subtotal)} ${esc(currency)}</span>
        <span><b>Discount (${esc(m.discount_pct)}%):</b> -${esc(m.discount_amount)} ${esc(currency)}</span>
        <span><b>Total:</b> ${esc(m.total)} ${esc(currency)}</span>
      </div>
    </div>`;
  }).join('');

  const market = doc.market_comparison ? `
    <div class="phase" style="margin-top:10px">
      <div class="pname">What the same work costs elsewhere</div>
      <table class="dtable">
        <thead><tr><th>Elsewhere</th><th>Published range</th><th>This budget</th></tr></thead>
        <tbody>${doc.market_comparison.map(b => `
          <tr><td>${esc(b.label)}</td><td class="num">${esc(b.rate_range)}</td>
              <td class="num">US$${esc(b.price_low)} – US$${esc(b.price_high)}</td></tr>`).join('')}
      </tbody></table>
    </div>` : '';

  const milestones = (doc.milestones||[]).length ? `
    <div class="phase" style="margin-top:10px">
      <div class="pname">Payment terms (0% = not yet set manually)</div>
      <table class="dtable">
        <thead><tr><th>When</th><th>What's delivered</th><th>Part</th><th>Amount</th></tr></thead>
        <tbody>${doc.milestones.map(m => `
          <tr><td>${esc(m.when)}</td><td>${esc(m.description)}</td>
              <td class="num">${esc(m.part_pct)}%</td><td class="num">${esc(m.amount)}</td></tr>`).join('')}
      </tbody></table>
    </div>` : '';

  const pid = projectId ? esc(projectId) : '';
  const downloads = projectId ? `
    <div class="row" style="margin-top:10px">
      <button onclick="window.open('/mock/api/budget/${pid}/pdf', '_blank')">⬇ Download PDF</button>
      <button onclick="downloadBudgetJson('${pid}')">⬇ Download JSON</button>
      <button onclick="openBudgetEditor('${pid}')">✎ Edit discount / contingency / milestones</button>
    </div>` : '';

  return `<details open class="deliverable"><summary>budget — ${doc.months.length} month(s), Axo Capital format</summary>
    ${monthBlocks}
    <div class="needs-row" style="margin-top:10px; font-size:13px">
      <span><b>Subtotal after discounts:</b> ${esc(doc.subtotal_after_discounts)} ${esc(currency)}</span>
      <span><b>IVA (${esc((doc.iva_rate*100).toFixed(0))}%):</b> ${esc(doc.iva_amount)} ${esc(currency)}</span>
      <span><b>Contingency:</b> ${doc.contingency_pct != null ? esc(doc.contingency_pct)+'%' : 'not set'}</span>
    </div>
    <div class="budget-total">TOTAL: ${esc(doc.total_all_included)} ${esc(currency)}</div>
    ${market}
    ${milestones}
    ${downloads}
  </details>`;
}

async function downloadBudgetJson(projectId) {
  const doc = await api('/budget/' + projectId + '/document');
  const blob = new Blob([JSON.stringify(doc, null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = (doc.project_name || 'budget').replace(/\s+/g, '_') + '_budget.json';
  a.click();
}

async function openBudgetEditor(projectId) {
  const doc = await api('/budget/' + projectId + '/document');
  const discounts = doc.months.map(m => `${m.month}=${m.discount_pct}`).join(', ');
  const newDiscounts = prompt('Discount % per month (format "June=100, July=40"):', discounts);
  if (newDiscounts === null) return;
  const discount_pct_by_month = {};
  newDiscounts.split(',').forEach(pair => {
    const [k, v] = pair.split('=').map(s => s.trim());
    if (k) discount_pct_by_month[k] = parseFloat(v) || 0;
  });

  const contingencyStr = prompt('Contingency % (blank = not set):', doc.contingency_pct != null ? String(doc.contingency_pct) : '');
  const contingency_pct = contingencyStr === null || contingencyStr.trim() === '' ? null : parseFloat(contingencyStr);

  const milestonesStr = prompt(
    'Milestone parts, format "When|Description|Part%" one per line:',
    (doc.milestones||[]).map(m => `${m.when}|${m.description}|${m.part_pct}`).join('\n')
  );
  if (milestonesStr === null) return;
  const milestones = milestonesStr.split('\n').filter(Boolean).map(line => {
    const [when, description, part_pct] = line.split('|').map(s => (s||'').trim());
    return {when, description, part_pct: parseFloat(part_pct) || 0, amount: 0};
  });

  api('/budget/' + projectId + '/extras', {
    method: 'PUT',
    body: JSON.stringify({discount_pct_by_month, contingency_pct, milestones}),
  }).then(() => { alert('Saved. Re-run the pipeline (or reload) to see it reflected everywhere.'); load(); });
}

function outcomeNotice(t) {
  const outcome = t.result && t.result.outcome;
  const notices = {
    already_ingested: {
      why: 'This meeting was already run before (agent.meeting_intake.event_id is UNIQUE — that\'s Agent 1\'s real idempotency guarantee, working correctly). Nothing re-ran, so there\'s nothing new to show below.',
      fix: 'To see a fresh run: click "Reset DB" (wipes and re-seeds), or "+ Generate 5" for new meetings, or create one below with your own transcript.',
    },
    final_qa_checked: {
      why: 'This was a Final QA meeting — Agent 8 only checks for a scope switch against the existing plan; it never builds a wireframe/plan/Gantt/budget. That\'s by design (docs §9.2), not a failure.',
      fix: 'To see the 4 deliverables, run an "onboarding" meeting instead (see the Northwind kickoff example).',
    },
  };
  const pendingReviewNotices = {
    low_confidence: {
      why: 'Classification confidence came back below the threshold (0.70), so the build chain never ran — this mirrors production: low-confidence meetings stop at a human review queue instead of guessing.',
      fix: 'With the stub model this usually means no explicit class keyword was found AND the transcript was short/generic. A longer, substantive transcript (or an explicit signal like "follow-up on the budget") should clear the threshold.',
    },
    no_project_match: {
      why: 'The meeting class was classified confidently, but no project matched (project_id is null) — and per docs §4.1, this system never auto-creates a project, so it goes to human review regardless of how confident the class itself was. (This exact gap was live in the code until this session\'s fix — a confident classification with no project used to sail straight into the build chain and write orphaned rows.)',
      fix: 'Make attendee emails match an existing seeded project (see Northwind/Halcyon), or accept that a genuinely brand-new client always needs a human to create the project first — that\'s the intended behavior, not a bug to work around.',
    },
  };
  const n = outcome === 'pending_review'
    ? (pendingReviewNotices[(t.result && t.result.reason) || 'low_confidence'])
    : (outcome && notices[outcome]);
  if (!n) return '';
  return `<div style="background:var(--accent-soft); border:1px solid var(--accent); border-radius:4px;
              padding:8px 10px; margin-top:8px; font-size:12.5px;">
    <strong style="color:var(--accent)">Why nothing generated:</strong> ${esc(n.why)}<br>
    <span style="color:var(--muted)">${esc(n.fix)}</span>
  </div>`;
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
    ${outcomeNotice(t)}
    ${screenMocks(t.result && t.result.wireframe_screens)}
    ${planPanel(t.result && t.result.plan)}
    ${ganttPanel(t.result && t.result.gantt_tasks)}
    ${budgetPanel(t.result && t.result.budget_document, t.result && t.result.project_id)}
    <details><summary>raw result JSON</summary><pre>${esc(JSON.stringify(t.result, null, 2))}</pre></details>
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
