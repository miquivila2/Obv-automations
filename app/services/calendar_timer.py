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

BOUNDARY WITH PLAUD (see docs §9): once an event is due, ingestion needs the
meeting TRANSCRIPT and the attendee EMAILS.
  * Attendee emails: `public.events.attendee_ids`' real shape (email strings?
    team member uuids? something else?) hasn't been verified against
    production data. Decided (this session): proceed under a documented
    ASSUMPTION — they're email strings — rather than block on it (see
    _resolve_attendee_emails). If that assumption turns out wrong, swap that
    one function for a public.team_members(id -> email) lookup; nothing else
    downstream (classification, the build graph) needs to change.
  * Transcript: no longer blocked on Developer Platform approval (docs §9.7,
    revised this session) — app/services/plaud_client.py talks to Plaud's own
    MCP server instead. This module still has to resolve WHICH Plaud
    recording corresponds to this event (plaud_client.find_recording_id, by
    time-window overlap) before it can fetch the transcript, since a calendar
    event carries no Plaud recording id of its own.
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
        .select("id,start_at,end_at,project_id,attendee_ids")
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


def _resolve_attendee_emails(event: dict) -> list[str]:
    """ASSUMPTION (docs §9.10, not yet verified against production data):
    public.events.attendee_ids is a list of email strings. If that's wrong
    (e.g. team_member uuids instead), this is the only function to change —
    replace it with a public.team_members(id -> email) lookup."""
    return [str(a) for a in (event.get("attendee_ids") or [])]


def _parse_datetime(value) -> datetime:
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


async def process_due_event(event: dict) -> dict:
    """Resolve transcript + attendees for one due event and run Agent 1.

    Attendee emails are resolved now (see _resolve_attendee_emails and its
    documented assumption). The transcript comes from Plaud's MCP server
    (app/services/plaud_client.py) — first find_recording_id matches this
    event to exactly one Plaud recording by time-window overlap (ASSUMPTION:
    `public.events` has a `start_at` column, mirroring the already-used
    `end_at`), then fetch_transcript pulls its text.

    `language` has no real source for this automatic path yet (unlike the
    manual webhook, which takes it as an input); defaults to 'es' like the
    artifact-changed re-trigger does (app/main.py) until a per-project
    language source exists.
    """
    from app.services.ingestion import ingest_meeting
    from app.services.plaud_client import fetch_transcript, find_recording_id

    attendee_emails = _resolve_attendee_emails(event)
    plaud_note_id = await find_recording_id(
        event_id=event["id"],
        start_at=_parse_datetime(event["start_at"]),
        end_at=_parse_datetime(event["end_at"]),
    )
    transcript = await fetch_transcript(event_id=event["id"], plaud_note_id=plaud_note_id)

    return await ingest_meeting(
        event_id=event["id"],
        attendee_emails=attendee_emails,
        language="es",
        transcript_text=transcript,
        plaud_note_id=plaud_note_id,
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
