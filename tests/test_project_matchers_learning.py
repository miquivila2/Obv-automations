"""agent.project_matchers starts empty (docs §4.1) — apply_classification is
its only writer, learning an email matcher from each confidently-classified
meeting's attendees so the next meeting from the same people resolves
deterministically (step 1) instead of paying for the LLM every time.
"""
import pytest

from app.services.classification import ClassificationResult, apply_classification
from tests.fakes import FakeSupabase


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeSupabase()
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake_client)
    return fake_client


def _confident_result(project_id="p1"):
    return ClassificationResult(
        project_id=project_id,
        new_project_suggested_name=None,
        meeting_class="update",
        sub_type=None,
        confidence=0.95,
        reasoning="clear match",
    )


async def test_confident_classification_learns_new_email_matchers(fake):
    fake.seed("agent", "meeting_intake", [{"id": "intake-1", "event_id": "e1"}])
    await apply_classification("intake-1", _confident_result("p1"), attendee_emails=["Ops@Dasp.mx"])

    matchers = fake.rows("agent", "project_matchers")
    assert len(matchers) == 1
    assert matchers[0]["project_id"] == "p1"
    assert matchers[0]["kind"] == "email"
    assert matchers[0]["value"] == "ops@dasp.mx"


async def test_low_confidence_classification_learns_nothing(fake):
    fake.seed("agent", "meeting_intake", [{"id": "intake-1", "event_id": "e1"}])
    low_confidence = ClassificationResult(
        project_id="p1", new_project_suggested_name=None, meeting_class="onboarding",
        sub_type=None, confidence=0.2, reasoning="unsure",
    )
    await apply_classification("intake-1", low_confidence, attendee_emails=["ops@dasp.mx"])
    assert fake.rows("agent", "project_matchers") == []


async def test_email_already_known_to_a_project_is_never_reassigned(fake):
    fake.seed("agent", "meeting_intake", [{"id": "intake-1", "event_id": "e1"}])
    fake.seed("agent", "project_matchers", [{"project_id": "p-other", "kind": "email", "value": "shared@team.com"}])

    await apply_classification("intake-1", _confident_result("p1"), attendee_emails=["shared@team.com"])

    matchers = fake.rows("agent", "project_matchers")
    assert len(matchers) == 1
    assert matchers[0]["project_id"] == "p-other"  # untouched, not reassigned to p1


async def test_email_already_known_to_this_project_is_not_duplicated(fake):
    fake.seed("agent", "meeting_intake", [{"id": "intake-1", "event_id": "e1"}])
    fake.seed("agent", "project_matchers", [{"project_id": "p1", "kind": "email", "value": "ops@dasp.mx"}])

    await apply_classification("intake-1", _confident_result("p1"), attendee_emails=["ops@dasp.mx"])

    assert len(fake.rows("agent", "project_matchers")) == 1


async def test_no_project_id_learns_nothing(fake):
    fake.seed("agent", "meeting_intake", [{"id": "intake-1", "event_id": "e1"}])
    no_project = ClassificationResult(
        project_id=None, new_project_suggested_name="New Co", meeting_class="onboarding",
        sub_type=None, confidence=0.9, reasoning="brand new",
    )
    await apply_classification("intake-1", no_project, attendee_emails=["a@b.com"])
    assert fake.rows("agent", "project_matchers") == []
