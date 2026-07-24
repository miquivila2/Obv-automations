"""Agent 1's real trigger: detect meetings that ended ~30 minutes ago.

Google Calendar sync is NOT something we build — it already exists at the CRM
level (public.google_credentials + public.events, kept in sync by the Lovable
app). What's missing is the piece that watches `public.events` for a meeting
that just ended and fires Agent 1. That's this module.

Design:
  * `_events_due_for_processing` is pure (no I/O) — given a list of events, the
    set of event ids already ingested, and "now", it returns which events should
    fire. Fully unit-testable without Supabase.
  * `find_due_events` / `run_calendar_timer_once` do the I/O: read
    public.events (read-only) and agent.meeting_intake (read-only, for
    dedup — ingestion's own UNIQUE constraint on event_id is the real
    idempotency guarantee; this is just to avoid reprocessing noise).
  * Polling, not a push, because Google Calendar doesn't push to us. Meant to
    be called on a schedule (cron / Task Scheduler / cloud scheduler) via the
    `POST /internal/calendar-timer/tick` endpoint — see docs/LOCAL_DEPLOYMENT.md.

BOUNDARY WITH PLAUD (see docs §9, task pending real API docs): once an event is
due, ingestion needs the meeting TRANSCRIPT and the attendee EMAILS. Neither is
resolved here yet:
  * Transcript: `export_plaud_note` (app/services/ingestion.py) is still a stub
    that only echoes text handed to it — there's no fetch-by-meeting call yet.
  * Attendee emails: `public.events.attendee_ids` shape (email strings? team
    member uuids? something else?) hasn't been verified against real data — do
    NOT guess at its meaning here. Resolve it once confirmed.
`process_due_event` raises NotImplementedError rather than silently guessing
at either, so the boundary is loud, not silently wrong.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

DEFAULT_DELAY_MINUTES = 30
# How far back to look for unprocessed events. Bounds the query and stops an
# outage from causing a flood of very old events to fire at once when the
# poller comes back up.
DEFAULT_LOOKBACK_HOURS = 24


def _events_due_for_processing(
    events: list[dict],
    already_processed_event_ids: set[str],
    now: datetime,
    delay_minutes: int = DEFAULT_DELAY_MINUTES,
) -> list[dict]:
    """Pure filter: events whose end_at is at least `delay_minutes` in the past,
    and that don't already have an agent.meeting_intake row. `events` items need
    only `id` and `end_at` (an ISO datetime string or datetime)."""
    cutoff = now - timedelta(minutes=delay_minutes)
    due = []
    for event in events:
        if event["id"] in already_processed_event_ids:
            continue
        end_at = event["end_at"]
        if isinstance(end_at, str):
            end_at = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
        if end_at <= cutoff:
            due.append(event)
    return due


def find_due_events(
    delay_minutes: int = DEFAULT_DELAY_MINUTES, lookback_hours: int = DEFAULT_LOOKBACK_HOURS
) -> list[dict]:
    """Query public.events (read-only) for meetings ended `delay_minutes`+ ago,
    within the lookback window, that have no agent.meeting_intake row yet."""
    from app.db.client import get_supabase

    supabase = get_supabase()
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(hours=lookback_hours + 1)).isoformat()

    candidates = (
        supabase.table("events")
        .select("id,end_at,project_id")
        .gte("end_at", window_start)
        .lte("end_at", now.isoformat())
        .execute()
        .data
    )
    if not candidates:
        return []

    processed = (
        supabase.schema("agent")
        .table("meeting_intake")
        .select("event_id")
        .in_("event_id", [e["id"] for e in candidates])
        .execute()
        .data
    )
    processed_ids = {row["event_id"] for row in processed}

    return _events_due_for_processing(candidates, processed_ids, now, delay_minutes)


async def process_due_event(event: dict) -> dict:
    """Resolve transcript + attendees for one due event and run Agent 1.

    NOT IMPLEMENTED YET — deliberately. See module docstring "Boundary with
    Plaud". Wire this once (a) Plaud's Developer Platform integration replaces
    the export_plaud_note stub, and (b) events.attendee_ids' real shape is
    confirmed against production data.
    """
    raise NotImplementedError(
        f"process_due_event: event {event['id']} is due, but transcript fetching "
        "(Plaud) and attendee resolution (events.attendee_ids shape) are not wired "
        "yet — see docs/ARCHITECTURE.md §9 and app/services/calendar_timer.py."
    )


async def run_calendar_timer_once() -> dict:
    """Entry point for the scheduled tick (see POST /internal/calendar-timer/tick).
    Resilient: one event's failure doesn't stop the others."""
    due = find_due_events()
    processed, failed = [], []
    for event in due:
        try:
            await process_due_event(event)
            processed.append(event["id"])
        except Exception as e:  # noqa: BLE001 - collect and continue, don't crash the tick
            failed.append({"event_id": event["id"], "error": str(e)})
    return {"due": len(due), "processed": processed, "failed": failed}
