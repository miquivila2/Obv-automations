"""The due-event filter is pure (no Supabase), so it's tested directly. This is
the core logic of Agent 1's real trigger: "meeting ended >= 30 min ago and
hasn't been ingested yet".
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.calendar_timer import (
    _events_due_for_processing,
    _resolve_attendee_emails,
    process_due_event,
)
from tests.fakes import FakeSupabase

_NOW = datetime(2026, 7, 24, 15, 0, tzinfo=timezone.utc)


def _event(id_, minutes_ago):
    return {"id": id_, "end_at": (_NOW - timedelta(minutes=minutes_ago)).isoformat()}


def test_event_ended_30_min_ago_is_due():
    events = [_event("e1", 30)]
    assert _events_due_for_processing(events, set(), _NOW) == events


def test_event_ended_10_min_ago_is_not_due_yet():
    events = [_event("e1", 10)]
    assert _events_due_for_processing(events, set(), _NOW) == []


def test_already_processed_event_is_skipped():
    events = [_event("e1", 60)]
    assert _events_due_for_processing(events, {"e1"}, _NOW) == []


def test_custom_delay_is_respected():
    events = [_event("e1", 45)]
    assert _events_due_for_processing(events, set(), _NOW, delay_minutes=60) == []
    assert _events_due_for_processing(events, set(), _NOW, delay_minutes=30) == events


def test_string_end_at_is_parsed():
    # Supabase returns ISO strings, not datetimes — confirm both work.
    events = [{"id": "e1", "end_at": "2026-07-24T14:00:00+00:00"}]  # 60 min before _NOW
    assert _events_due_for_processing(events, set(), _NOW) == events


def test_resolve_attendee_emails_under_the_documented_assumption():
    # ASSUMPTION (docs §9.10): attendee_ids are email strings. Swap
    # _resolve_attendee_emails alone if that's ever confirmed wrong.
    assert _resolve_attendee_emails({"attendee_ids": ["a@b.com", "c@d.com"]}) == ["a@b.com", "c@d.com"]


def test_resolve_attendee_emails_handles_missing_or_null():
    assert _resolve_attendee_emails({}) == []
    assert _resolve_attendee_emails({"attendee_ids": None}) == []


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeSupabase()
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake_client)
    monkeypatch.setattr("app.services.ingestion.get_supabase", lambda: fake_client)
    return fake_client


def _due_event(id_="e1", attendee_ids=None):
    return {
        "id": id_,
        "start_at": "2026-07-24T13:00:00+00:00",
        "end_at": "2026-07-24T14:00:00+00:00",
        "attendee_ids": attendee_ids or ["a@b.com"],
    }


@pytest.fixture
def fake_plaud(monkeypatch):
    """process_due_event now resolves a recording (find_recording_id) before
    fetching its transcript (docs §9.7, revised: Plaud's own MCP server, not
    the Developer Platform). Both are stubbed here at the module-attribute
    level, same pattern as every other lazy-imported dependency in this repo —
    real MCP wiring is covered separately in test_plaud_client.py."""

    async def _fake_find_recording_id(*, event_id, start_at, end_at):
        return "rec-1"

    async def _fake_fetch_transcript(*, event_id, plaud_note_id):
        assert plaud_note_id == "rec-1"
        return "the real transcript"

    monkeypatch.setattr("app.services.plaud_client.find_recording_id", _fake_find_recording_id)
    monkeypatch.setattr("app.services.plaud_client.fetch_transcript", _fake_fetch_transcript)


async def test_process_due_event_ingests_using_the_resolved_recording(fake, fake_plaud):
    result = await process_due_event(_due_event())
    assert result["event_id"] == "e1"
    assert result["transcript"] == "the real transcript"
    assert result["plaud_note_id"] == "rec-1"


async def test_process_due_event_propagates_an_ambiguous_match(fake, monkeypatch):
    # find_recording_id refuses to guess (see plaud_client.py) — that failure
    # must surface, not be swallowed here.
    async def _fake_find_recording_id(*, event_id, start_at, end_at):
        raise ValueError(f"2 Plaud recordings overlap event {event_id!r}'s window")

    monkeypatch.setattr("app.services.plaud_client.find_recording_id", _fake_find_recording_id)

    with pytest.raises(ValueError, match="ambiguous|overlap"):
        await process_due_event(_due_event())
