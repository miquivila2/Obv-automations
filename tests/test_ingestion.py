"""Agent 1's ingest flow against a fake Supabase + the stub model provider
(conftest sets MODEL_PROVIDER=stub). export_plaud_note is still a stub (docs
§9.7) — it must simply echo the transcript unchanged; ingest_meeting must be
idempotent on event_id (agent.meeting_intake.event_id is UNIQUE at the DB
level, but the app also short-circuits to avoid a doomed insert).
"""
import pytest

from app.services.ingestion import export_plaud_note, ingest_meeting
from tests.fakes import FakeSupabase


async def test_export_plaud_note_echoes_the_transcript():
    assert await export_plaud_note("event-1", "hello world", None) == "hello world"


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeSupabase()
    # ingestion.py imports get_supabase at module load time (not lazily, unlike
    # the persist_* modules), so the name already bound in its namespace must be
    # patched directly; classification.py imports it lazily inside its
    # functions, so patching app.db.client covers that side.
    monkeypatch.setattr("app.services.ingestion.get_supabase", lambda: fake_client)
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake_client)
    return fake_client


async def test_ingest_meeting_is_idempotent_on_event_id(fake):
    fake.seed("agent", "meeting_intake", [{"id": "existing", "event_id": "e1", "status": "processed"}])
    result = await ingest_meeting(event_id="e1", attendee_emails=[], language="en", transcript_text="t")
    assert result == {"id": "existing", "event_id": "e1", "status": "processed"}
    assert len(fake.rows("agent", "meeting_intake")) == 1  # no duplicate insert attempted


async def test_ingest_meeting_creates_and_classifies_a_new_event(fake):
    result = await ingest_meeting(event_id="e2", attendee_emails=["a@b.com"], language="en", transcript_text="t")
    assert result["event_id"] == "e2"
    assert result["transcript"] == "t"
    assert result["classification"]["meeting_class"]  # stub classifier ran
    assert len(fake.rows("agent", "meeting_intake")) == 1


async def test_low_confidence_classification_leaves_status_pending_review(fake):
    # The stub classifier is deliberately low-confidence (see
    # ClassificationResult.stub) so a stubbed run lands in the review queue.
    await ingest_meeting(event_id="e3", attendee_emails=[], language="en", transcript_text="t")
    row = fake.rows("agent", "meeting_intake")[0]
    assert row["status"] == "pending_review"
