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


async def test_process_due_event_still_fails_at_the_plaud_boundary(fake):
    # The one remaining blocker (docs §9.7): transcript fetching. Attendee
    # resolution must NOT be what raises anymore.
    with pytest.raises(NotImplementedError, match="fetch_transcript"):
        await process_due_event({"id": "e1", "attendee_ids": ["a@b.com"]})


async def test_process_due_event_ingests_once_transcript_fetching_is_available(fake, monkeypatch):
    # Simulates Plaud access landing — process_due_event should need no changes.
    async def _fake_fetch_transcript(*, event_id, plaud_note_id):
        return "the real transcript"

    monkeypatch.setattr("app.services.plaud_client.fetch_transcript", _fake_fetch_transcript)

    result = await process_due_event({"id": "e1", "attendee_ids": ["a@b.com"]})
    assert result["event_id"] == "e1"
    assert result["transcript"] == "the real transcript"
