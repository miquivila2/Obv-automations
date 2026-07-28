"""Runs the REAL pipeline against mock data, and captures a full trace of it.

The whole value of this harness is that it runs the production code path, not a
reimplementation of it. So this module deliberately calls the same functions the
live triggers call — `ingest_meeting`, `orchestrator.route`, the compiled graph,
`run_final_qa_check` — and changes exactly one thing: where the transcript comes
from (the mock event's own `transcript` field, instead of Plaud's MCP server).

Plaud is the one integration that CANNOT be file-swapped the way the database
can: it's a subprocess + OAuth, not a data source behind get_supabase(). So the
mock event carries its transcript inline and we skip that call. Everything
downstream of the transcript is the real thing.

Log capture: a handler is attached for the duration of a run, so the UI can show
exactly what happened at every stage — including the tracebacks of failures,
which is the point of the exercise.
"""
from __future__ import annotations

import logging
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("mock.runner")


@dataclass
class LogLine:
    level: str
    logger: str
    message: str
    elapsed_ms: int


@dataclass
class RunTrace:
    """Everything one pipeline execution produced, for display in the UI."""

    event_id: str
    title: str
    ok: bool = False
    stage: str = "starting"
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    traceback: str | None = None
    logs: list[LogLine] = field(default_factory=list)
    duration_ms: int = 0
    db_counts_before: dict[str, int] = field(default_factory=dict)
    db_counts_after: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "ok": self.ok,
            "stage": self.stage,
            "result": self.result,
            "error": self.error,
            "traceback": self.traceback,
            "duration_ms": self.duration_ms,
            "db_counts_before": self.db_counts_before,
            "db_counts_after": self.db_counts_after,
            "logs": [
                {"level": l.level, "logger": l.logger, "message": l.message, "elapsed_ms": l.elapsed_ms}
                for l in self.logs
            ],
        }


class _TraceHandler(logging.Handler):
    def __init__(self, trace: RunTrace, started: float) -> None:
        super().__init__(level=logging.DEBUG)
        self._trace = trace
        self._started = started

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken log record must not kill the run
            message = "<unformattable log record>"
        self._trace.logs.append(
            LogLine(
                level=record.levelname,
                logger=record.name,
                message=message,
                elapsed_ms=int((time.monotonic() - self._started) * 1000),
            )
        )


@contextmanager
def _capture(trace: RunTrace, started: float):
    handler = _TraceHandler(trace, started)
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


async def run_pipeline_for_event(event_id: str) -> RunTrace:
    """Execute the full production pipeline for one mock meeting."""
    from app.mock.store import get_mock_store

    store = get_mock_store()
    event = next((e for e in store.rows("public", "events") if e.get("id") == event_id), None)

    trace = RunTrace(event_id=event_id, title=(event or {}).get("title", "<unknown>"))
    if event is None:
        trace.error = f"No mock event with id {event_id!r}"
        trace.stage = "lookup"
        return trace

    started = time.monotonic()
    trace.db_counts_before = store.counts()

    with _capture(trace, started):
        try:
            await _execute(event, trace)
            trace.ok = trace.error is None
        except Exception as exc:  # noqa: BLE001 - capturing is the whole point
            trace.error = f"{type(exc).__name__}: {exc}"
            trace.traceback = traceback.format_exc()
            logger.exception("pipeline failed at stage %s", trace.stage)

    trace.duration_ms = int((time.monotonic() - started) * 1000)
    trace.db_counts_after = store.counts()
    return trace


async def _execute(event: dict, trace: RunTrace) -> None:
    """The real pipeline, stage by stage. Mirrors app/main.py's calendar-timer
    endpoint — the production entry point for an automatic run."""
    from app.services.ingestion import ingest_meeting

    event_id = event["id"]

    # --- Stage 1: transcript ------------------------------------------------
    # The one substitution vs. production: the transcript comes from the mock
    # event rather than Plaud's MCP server (see module docstring).
    trace.stage = "transcript"
    transcript = event.get("transcript") or ""
    logger.info("transcript: %d chars from mock event %s", len(transcript), event_id)
    if not transcript.strip():
        logger.warning(
            "transcript is EMPTY — classification will have nothing to work with. "
            "This is a deliberate edge case if you seeded it."
        )

    attendees = [str(a) for a in (event.get("attendee_ids") or [])]
    language = event.get("language") or "es"
    logger.info("attendees=%s language=%s", attendees, language)

    # --- Stage 2: Agent 1, ingest + classify --------------------------------
    trace.stage = "agent1_ingest_classify"
    logger.info("Agent 1: ingesting and classifying")
    intake = await ingest_meeting(
        event_id=event_id,
        attendee_emails=attendees,
        language=language,
        transcript_text=transcript,
        plaud_note_id=f"mock-{event_id}",
    )
    classification = intake.get("classification")
    trace.result["intake_id"] = intake.get("id")
    trace.result["classification"] = classification
    logger.info("Agent 1 classification: %s", classification)

    if not classification:
        # An already-ingested event returns the existing row with no fresh
        # classification — that's the idempotency guarantee working, not a bug.
        trace.result["outcome"] = "already_ingested"
        logger.warning(
            "event %s was already ingested (agent.meeting_intake.event_id is UNIQUE); "
            "nothing re-ran. Reset the mock DB to process it again.",
            event_id,
        )
        return

    from app.config import get_settings

    threshold = get_settings().classification_confidence_threshold
    if classification["confidence"] < threshold:
        trace.result["outcome"] = "pending_review"
        trace.result["reason"] = "low_confidence"
        logger.warning(
            "confidence %.2f below threshold %.2f -> pending_review, chain NOT run",
            classification["confidence"],
            threshold,
        )
        return

    if not classification["project_id"]:
        # Mirrors app/main.py's calendar_timer guard (same production bug,
        # caught live via this harness): a confident CLASS with no matched
        # project must still go to review, not run — see docs §4.1. Without
        # this, a real run wrote budget_line_items with project_id=None.
        trace.result["outcome"] = "pending_review"
        trace.result["reason"] = "no_project_match"
        logger.warning(
            "project_id is null despite confidence %.2f -> pending_review, chain NOT run "
            "(docs §4.1: never auto-create a project)",
            classification["confidence"],
        )
        return

    # --- Stage 3: route ------------------------------------------------------
    meeting_class = classification["meeting_class"]

    if meeting_class == "final_qa":
        trace.stage = "agent8_final_qa"
        logger.info("Agent 8: final QA scope check (bypasses the build graph)")
        from app.services.qa_check import run_final_qa_check

        qa = await run_final_qa_check(
            project_id=classification["project_id"],
            intake_id=intake["id"],
            notes=transcript,
            trigger_source="mock",
        )
        trace.result["outcome"] = "final_qa_checked"
        trace.result["qa"] = qa
        logger.info("Agent 8 result: %s", qa)
        return

    # --- Stage 4: the build graph -------------------------------------------
    trace.stage = "build_graph"
    logger.info("Orchestrator + build chain: class=%s", meeting_class)

    from app.db.checkpointer import get_checkpointer
    from app.graph.build import build_graph

    initial_state = {
        "project_id": classification["project_id"],
        "intake_id": intake["id"],
        "meeting_class": meeting_class,
        "sub_type": classification.get("sub_type"),
        "language": language,
        "notes": transcript,
        "judge_round": 0,
        "trigger_source": "mock",
    }

    async with get_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)
        config = {"configurable": {"thread_id": initial_state["project_id"]}}
        final_state = await graph.ainvoke(initial_state, config=config)

    trace.stage = "done"
    trace.result["outcome"] = "processed"
    trace.result["needs_human_review"] = bool(final_state.get("needs_human_review"))
    trace.result["final_artifact"] = final_state.get("current_artifact_type")
    logger.info(
        "graph finished: last_artifact=%s needs_human_review=%s",
        final_state.get("current_artifact_type"),
        final_state.get("needs_human_review"),
    )

    trace.result["project_id"] = classification["project_id"]
    _attach_deliverables(trace, classification["project_id"])


def _attach_deliverables(trace: RunTrace, project_id: str) -> None:
    """Surface whatever got persisted as its own, UI-renderable fields — not
    buried in raw graph state. By the time the chain reaches Budget,
    state["draft"] holds only the BUDGET's draft; each earlier artifact only
    survives in its own table once persisted, so this reads them back from
    there rather than from final_state. Every artifact is optional: a
    follow-up run only touches one of them, and this must not fail just
    because the others were never (re)generated this run."""
    from app.services.budget_persist import load_assembled_budget_document
    from app.services.gantt_persist import load_latest_gantt_tasks
    from app.services.plan_persist import load_latest_plan
    from app.services.wireframe_persist import load_latest_wireframe

    wireframe = load_latest_wireframe(project_id)
    if wireframe:
        trace.result["wireframe_screens"] = wireframe.get("payload", {}).get("screens", [])

    plan = load_latest_plan(project_id)
    if plan:
        trace.result["plan"] = plan.get("payload", {})

    gantt_tasks = load_latest_gantt_tasks(project_id)
    if gantt_tasks:
        trace.result["gantt_tasks"] = gantt_tasks

    budget_document = load_assembled_budget_document(project_id)
    if budget_document:
        trace.result["budget_document"] = budget_document


async def run_pipeline_for_all(event_ids: list[str]) -> list[RunTrace]:
    """Batch runner — one event's failure never stops the others, mirroring
    run_calendar_timer_once's resilience."""
    traces = []
    for event_id in event_ids:
        traces.append(await run_pipeline_for_event(event_id))
    return traces
