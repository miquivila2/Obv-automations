"""Stable interface for Plaud's Developer Platform (docs §9.7), blocked on
`dev.plaud.ai` "Contact Us" approval for PLAUD_CLIENT_ID/PLAUD_API_KEY.

`ingestion.export_plaud_note` already covers the path used by the direct
webhook (a transcript handed to us manually, per meeting). This module covers
the OTHER path: the calendar timer, which starts with no transcript at all and
needs to fetch one by event/meeting reference once the API exists.

Swap only `fetch_transcript`'s body when Plaud access lands — its signature is
the stable contract app/services/calendar_timer.py already codes against, so
nothing downstream (classification, the build graph) needs to change.
"""
from __future__ import annotations


async def fetch_transcript(*, event_id: str, plaud_note_id: str | None) -> str:
    """Fetch the meeting transcript for one calendar event from Plaud's
    Developer Platform.

    NOT IMPLEMENTED YET — blocked on Plaud Developer Platform access
    (PLAUD_CLIENT_ID / PLAUD_API_KEY via portal.plaud.ai, see
    docs/ARCHITECTURE.md §9.7). Once granted, this is the one function that
    needs a real implementation.
    """
    raise NotImplementedError(
        f"plaud_client.fetch_transcript: no Plaud Developer Platform access yet "
        f"(event_id={event_id!r}, plaud_note_id={plaud_note_id!r}) — see docs/ARCHITECTURE.md §9.7."
    )
