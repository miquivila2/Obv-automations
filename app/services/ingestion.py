"""Agent 1 — Meeting Notes (ingest + classify).

Trigger: a timer 30 minutes after a meeting ends, driven off the CRM's
Google-Calendar-synced `public.events` row (see app/main.py). Idempotency is
enforced by `agent.meeting_intake.event_id` being UNIQUE — if the timer fires
twice for the same event, the second insert is rejected at the database level
rather than silently double-processing.

Steps (per the original whiteboard spec):
  1. Export the Plaud note.
  2. Classify project + meeting class (app.services.classification).
  3. Write the transcript + classification into agent.meeting_intake.
  4. Trigger the Orchestrator (Agent 7).

CRM note: reads the event from `public.events` (owned by the CRM); writes only
to `agent.meeting_intake` (our schema). It never writes to the CRM here.

NOTE on step 1: the source doc says Plaud export is "manual now, Developer
Platform JSON later" — there is no Plaud API integration yet. `export_plaud_note`
is a stub that takes the transcript as a direct argument until that decision is
made; it does NOT call any external Plaud API today.
"""
from __future__ import annotations

from app.db.client import get_supabase
from app.services.classification import apply_classification, classify_meeting


async def export_plaud_note(event_id: str, transcript_text: str, plaud_note_id: str | None) -> str:
    """STUB: today this just returns the already-exported transcript (manual
    export, per the source doc). Swap this out once Plaud's Developer Platform
    JSON export is wired up — the return type (raw transcript text) should stay
    stable so nothing downstream needs to change."""
    return transcript_text


async def ingest_meeting(
    *,
    event_id: str,
    attendee_emails: list[str],
    language: str,
    transcript_text: str,
    plaud_note_id: str | None = None,
) -> dict:
    """Full Agent 1 flow. `event_id` is the CRM `public.events` row id. Returns
    the intake row (or the existing one, if this event was already processed —
    idempotent)."""
    supabase = get_supabase()
    intake_tbl = supabase.schema("agent").table("meeting_intake")

    existing = intake_tbl.select("*").eq("event_id", event_id).execute().data
    if existing:
        return existing[0]

    transcript = await export_plaud_note(event_id, transcript_text, plaud_note_id)

    intake = (
        intake_tbl.insert(
            {
                "event_id": event_id,
                "plaud_note_id": plaud_note_id,
                "transcript": transcript,
                "language": language,
                "status": "pending_review",
            }
        )
        .execute()
        .data[0]
    )

    # The meeting title isn't a separate field from Plaud; the classifier reads it
    # out of the transcript itself.
    result = await classify_meeting(attendee_emails=attendee_emails, title_and_notes=transcript)
    await apply_classification(intake["id"], result, attendee_emails=attendee_emails)

    return {**intake, "classification": result.model_dump()}
