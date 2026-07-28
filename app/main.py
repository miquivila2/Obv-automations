"""FastAPI entrypoint — the HTTP surface the whole system triggers through.

Three inbound triggers, all push (no polling anywhere — see docs §7):

  POST /webhooks/calendar-timer   Google Calendar fires this 30 min after a
                                  meeting ends -> runs Agent 1 (ingestion),
                                  which classifies and then kicks the graph.

  POST /webhooks/artifact-changed Supabase Database Webhook fires this when an
                                  artifact row is edited. We act ONLY on
                                  source='human' rows (the re-trigger loop);
                                  source='agent' rows are ignored so agents
                                  never re-trigger themselves (docs §7).
                                  TODO(Session 6): wire to the real CRM artifact
                                  tables (project_plan_drafts / gantt_tasks /
                                  budget_line_items / agent.wireframe_drafts).

  POST /orchestrator/run          Direct kick of the build graph for an
                                  already-classified meeting (used by ingestion
                                  and available for manual re-runs).

  POST /internal/calendar-timer/tick   Meant for an external scheduler (cron /
                                  Task Scheduler / cloud scheduler), not a user.
                                  Polls public.events for meetings that ended
                                  30 min ago (Google Calendar sync already exists
                                  at the CRM level — this just watches it) and
                                  fires Agent 1. See app/services/calendar_timer.py.

Graph runs are keyed by thread_id = project_id, so the checkpointer can resume
the right project's state on a follow-up or human-review continuation.

AUTH: every trigger endpoint above requires the `X-Webhook-Secret` header to
match `settings.webhook_secret`. These endpoints write to the production CRM
(plan drafts, Gantt tasks, budget line items) and cost real model tokens, so an
unauthenticated public URL is not an option. /health is deliberately open, so a
load balancer or uptime check needs no secret.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.db.checkpointer import get_checkpointer
from app.db.client import get_supabase
from app.graph.build import build_graph
from app.graph.state import ArtifactType, BuildState
from app.services.ingestion import ingest_meeting
from app.services.qa_check import run_final_qa_check


def require_not_mock_mode() -> None:
    """Isolation guard (SAFETY REVIEW): the four PRODUCTION trigger endpoints
    below are shaped to run against the real CRM and, on the automatic path,
    the real Plaud MCP server. They are NOT how the mock harness (app/mock/)
    exercises the pipeline — that has its own routes, /mock/api/run and
    /mock/api/run-all, which read mock events directly and never touch Plaud
    or GitHub (see app/mock/runner.py).

    So when DATA_SOURCE=mock, calling one of these by habit or by a stray
    script must not silently "work" against mock data while still reaching
    out to real external services — it must fail loudly and say exactly what
    to call instead. This is what makes it structurally impossible to trigger
    production-shaped execution while testing, rather than merely discouraged."""
    if get_settings().data_source == "mock":
        raise HTTPException(
            status_code=409,
            detail=(
                "This endpoint is disabled while DATA_SOURCE=mock (safety isolation — "
                "see require_not_mock_mode in app/main.py). Use POST /mock/api/run/{event_id} "
                "or POST /mock/api/run-all instead."
            ),
        )


def require_webhook_secret(x_webhook_secret: str | None = Header(None)) -> None:
    """Shared-secret gate for every trigger endpoint. When no secret is
    configured the app is in local dev — allow, but that's the only case.

    `expected` must be checked for falsy, not `is None`: a `.env` file with
    `WEBHOOK_SECRET=` (present, empty — exactly what .env.example ships)
    parses to `""`, not `None`. Treating only `None` as "unconfigured" would
    make that documented, intentional local-dev setup 401 on every request."""
    import secrets

    expected = get_settings().webhook_secret
    if not expected:
        return
    if x_webhook_secret is None or not secrets.compare_digest(x_webhook_secret, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-Webhook-Secret")


def _log_data_source_mode() -> None:
    """SAFETY REVIEW: an unmissable, impossible-to-scroll-past declaration of
    which data source this process is running against, every single time it
    starts — so "which mode am I in" is never a question you have to go dig
    for in .env before trusting (or distrusting) what a run just did."""
    import logging

    logger = logging.getLogger("app.startup")
    settings = get_settings()

    if settings.data_source == "mock":
        logger.warning(
            "\n%s\n"
            "  RUNNING IN MOCK MODE — DATA_SOURCE=mock\n"
            "  * All reads/writes go to %s (a local JSON file), NOT Supabase.\n"
            "  * The 4 production trigger endpoints are disabled (409) — use /mock/api/* instead.\n"
            "  * Plaud and GitHub calls are blocked/faked — see app/services/plaud_client.py "
            "and app/services/github_progress.py.\n"
            "  * Test console: http://127.0.0.1:8000/mock\n"
            "%s",
            "=" * 74, settings.mock_data_path, "=" * 74,
        )
    else:
        logger.warning(
            "RUNNING IN PRODUCTION DATA MODE — DATA_SOURCE=supabase. "
            "This process reads/writes the REAL CRM at %s.",
            settings.supabase_url or "(SUPABASE_URL not set)",
        )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _log_data_source_mode()
    yield


app = FastAPI(title="Oblivion Multi-Agent Build Automation", lifespan=_lifespan)

# The mock-CRM test console (app/mock/) is mounted ONLY in mock mode. Its routes
# create/delete data and run the chain with no webhook secret — safe against a
# JSON file, unacceptable against the production CRM. Gating on data_source
# means there is no configuration in which these routes touch real data.
if get_settings().data_source == "mock":
    from app.mock.ui import router as mock_router

    app.include_router(mock_router)


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------
class CalendarTimerPayload(BaseModel):
    event_id: str  # public.events row id (the CRM's Google-Calendar-synced event)
    attendee_emails: list[str] = []
    language: str
    transcript_text: str  # manual Plaud export for now (see ingestion.py)
    plaud_note_id: str | None = None


class ArtifactChangedPayload(BaseModel):
    # Shape mirrors a Supabase Database Webhook `record` for an artifact table.
    project_id: str
    type: ArtifactType
    source: str  # 'agent' | 'human'


class OrchestratorRunPayload(BaseModel):
    project_id: str
    intake_id: str
    meeting_class: str
    sub_type: ArtifactType | None = None
    language: str
    notes: str


# --------------------------------------------------------------------------
# Graph runner
# --------------------------------------------------------------------------
async def _run_graph(initial_state: BuildState) -> dict:
    """Compile and invoke the build graph for one project, resumable via the
    checkpointer keyed on project_id."""
    async with get_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)
        config = {"configurable": {"thread_id": initial_state["project_id"]}}
        result = await graph.ainvoke(initial_state, config=config)
        return dict(result)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@app.post("/webhooks/calendar-timer", dependencies=[Depends(require_not_mock_mode), Depends(require_webhook_secret)])
async def calendar_timer(payload: CalendarTimerPayload) -> dict:
    """Agent 1 trigger. Ingest + classify; if confidently classified into an
    actionable class, kick the graph. Low-confidence meetings stop at the
    review queue and are not run."""
    intake = await ingest_meeting(**payload.model_dump())
    classification = intake.get("classification")

    if not classification or classification["confidence"] < 0.70 or not classification["project_id"]:
        # project_id is checked separately from confidence: a classifier can
        # be genuinely confident about the meeting CLASS while still finding
        # no matching project (docs §4.1 — "we never auto-create a new
        # project... if nothing matches, it goes to human review", regardless
        # of how sure the class itself is). Found live: a high-confidence
        # classification with project_id=None previously sailed straight into
        # the build graph and wrote real CRM rows (budget_line_items,
        # gantt_tasks, ...) with no project to attach them to.
        return {"status": "pending_review", "intake_id": intake["id"]}

    if classification["meeting_class"] == "final_qa":
        # Agent 8 (docs §9.2) — read-only scope-switch check, not the build graph.
        result = await run_final_qa_check(
            project_id=classification["project_id"],
            intake_id=intake["id"],
            notes=payload.transcript_text,
        )
        return {"status": "final_qa_checked", "intake_id": intake["id"], **result}

    result = await _run_graph(
        {
            "project_id": classification["project_id"],
            "intake_id": intake["id"],
            "meeting_class": classification["meeting_class"],
            "sub_type": classification.get("sub_type"),
            "language": payload.language,
            "notes": payload.transcript_text,
            "judge_round": 0,
            "trigger_source": "webhook",
        }
    )
    return {"status": "processed", "intake_id": intake["id"], "needs_human_review": result.get("needs_human_review", False)}


@app.post("/webhooks/artifact-changed", dependencies=[Depends(require_not_mock_mode), Depends(require_webhook_secret)])
async def artifact_changed(payload: ArtifactChangedPayload) -> dict:
    """The manual-edit re-trigger loop. Only human edits re-flow the chain;
    agent writes are ignored so the chain can't trigger itself (docs §7).

    Cascade is full and automatic (decided): a human wireframe edit re-flows
    plan -> Gantt -> budget with no intermediate confirmation."""
    if payload.source != "human":
        return {"status": "ignored", "reason": "agent write, not a human edit"}

    # The edited artifact's *successor* is where the chain must resume from.
    resume_from = {"wireframe": "plan", "gantt": "budget", "plan": "gantt", "budget": None}[payload.type]
    if resume_from is None:
        return {"status": "noop", "reason": "budget is terminal; nothing downstream"}

    # Reconstruct minimal state and re-run as a follow_up from the successor.
    # notes/language are read from the latest intake for this project.
    latest_intake = (
        get_supabase()
        .schema("agent")
        .table("meeting_intake")
        .select("*")
        .eq("project_id", payload.project_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    intake = latest_intake[0] if latest_intake else {}

    result = await _run_graph(
        {
            "project_id": payload.project_id,
            "intake_id": intake.get("id"),
            "meeting_class": "follow_up",
            "sub_type": resume_from,
            "language": intake.get("language", "es"),
            "notes": "",  # follow-up here is driven by the edited upstream artifact, not new notes
            "edited_artifact_type": payload.type,
            "judge_round": 0,
            "trigger_source": "webhook",
        }
    )
    return {"status": "re_triggered", "from": resume_from, "needs_human_review": result.get("needs_human_review", False)}


@app.post("/orchestrator/run", dependencies=[Depends(require_not_mock_mode), Depends(require_webhook_secret)])
async def orchestrator_run(payload: OrchestratorRunPayload) -> dict:
    """Direct graph kick for an already-classified meeting."""
    if payload.meeting_class == "final_qa":
        # Agent 8 (docs §9.2) — same read-only check as the calendar-timer path.
        result = await run_final_qa_check(
            project_id=payload.project_id, intake_id=payload.intake_id, notes=payload.notes,
        )
        return {"status": "final_qa_checked", **result}

    result = await _run_graph({**payload.model_dump(), "judge_round": 0, "trigger_source": "manual"})
    return {"status": "processed", "needs_human_review": result.get("needs_human_review", False)}


@app.post("/internal/calendar-timer/tick", dependencies=[Depends(require_not_mock_mode), Depends(require_webhook_secret)])
async def calendar_timer_tick() -> dict:
    """Called by an external scheduler, not a user. See module docstring above
    and app/services/calendar_timer.py — attendee resolution is wired (under a
    documented assumption, docs §9.10); transcript fetching is still blocked on
    Plaud Developer Platform access, so due events currently report as failed
    with a clear reason rather than silently doing nothing."""
    from app.services.calendar_timer import run_calendar_timer_once

    return await run_calendar_timer_once()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
