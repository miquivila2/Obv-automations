"""The due-event filter is pure (no Supabase), so it's tested directly. This is
the core logic of Agent 1's real trigger: "meeting ended >= 30 min ago and
hasn't been ingested yet".
"""
from datetime import datetime, timedelta, timezone

from app.services.calendar_timer import _events_due_for_processing

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
