"""Agent 1 — Meeting Notes (ingest + classify).

Trigger: a timer 30 minutes after a meeting ends, driven off the Google
Calendar event (see app/main.py:/webhooks/calendar-timer). Idempotency is
enforced by `meetings.calendar_event_id` being UNIQUE — if the timer fires
twice for the same event, the second insert is rejected at the database
level rather than silently double-processing.

Steps (per the original whiteboard spec):
  1. Export the Plaud note.
  2. Classify project + meeting class (app.services.classification).
  3. Write the raw note + classification into Supabase.
  4. Trigger the Orchestrator (Agent 7).

NOTE on step 1: the source doc says Plaud export is "manual now, Developer
Platform JSON later" — there is no Plaud API integration yet. `export_plaud_
note` is a stub that takes the transcript as a direct argument until that
decision is made; it does NOT call any external Plaud API today.
"""
from __future__ import annotations

from app.db.client import get_supabase
from app.services.classification import apply_classification, classify_meeting


async def export_plaud_note(calendar_event_id: str, transcript_text: str, plaud_note_id: str | None) -> str:
    """STUB: today this just accepts an already-exported transcript (manual
    export, per the source doc). Swap this out once Plaud's Developer
    Platform JSON export is wired up — the return type (raw transcript text)
    should stay stable so nothing downstream needs to change.
    """
    return transcript_text


async def ingest_meeting(
    *,
    calendar_event_id: str,
    meeting_datetime: str,
    attendees: list[dict],
    language: str,
    transcript_text: str,
    plaud_note_id: str | None = None,
) -> dict:
    """Full Agent 1 flow. Returns the created meeting row (or the existing
    one, if this calendar_event_id was already processed — idempotent)."""
    supabase = get_supabase()

    existing = (
        supabase.table("meetings").select("*").eq("calendar_event_id", calendar_event_id).execute().data
    )
    if existing:
        return existing[0]

    transcript = await export_plaud_note(calendar_event_id, transcript_text, plaud_note_id)

    meeting = (
        supabase.table("meetings")
        .insert(
            {
                "calendar_event_id": calendar_event_id,
                "meeting_datetime": meeting_datetime,
                "attendees": attendees,
                "language": language,
                "status": "pending_review",
            }
        )
        .execute()
        .data[0]
    )

    supabase.table("raw_notes").insert(
        {"meeting_id": meeting["id"], "plaud_note_id": plaud_note_id, "transcript": transcript}
    ).execute()

    attendee_emails = [a["email"] for a in attendees if "email" in a]
    title_and_notes = transcript  # meeting title isn't a separate field from Plaud; the LLM reads it out of the transcript

    result = await classify_meeting(attendee_emails=attendee_emails, title_and_notes=title_and_notes)
    await apply_classification(meeting["id"], result)

    return {**meeting, "classification": result.model_dump()}
