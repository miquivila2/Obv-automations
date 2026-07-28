"""HTTP-level tests for the FastAPI surface. Endpoints that kick off the full
LangGraph build (calendar-timer trigger, /orchestrator/run) need a running
graph + checkpointer + Bedrock/Supabase — out of scope for a unit test. This
covers the request-shape and early-return paths that don't require any of that.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_ARTIFACT_CHANGED_BODY = {"project_id": "p1", "type": "wireframe", "source": "agent"}


@pytest.fixture
def secret(monkeypatch):
    """Force a configured webhook secret for this test (conftest leaves it
    unset, i.e. local-dev mode where the check is skipped)."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    return "s3cret"


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_artifact_changed_ignores_agent_writes():
    # Only source='human' re-triggers the chain (docs §7) — an agent write must
    # never trigger itself into a loop.
    resp = client.post(
        "/webhooks/artifact-changed", json={"project_id": "p1", "type": "wireframe", "source": "agent"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "agent write, not a human edit"}


def test_artifact_changed_budget_edit_is_a_noop():
    # Budget is terminal in the chain — nothing downstream to re-trigger.
    resp = client.post(
        "/webhooks/artifact-changed", json={"project_id": "p1", "type": "budget", "source": "human"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "noop"


def test_trigger_endpoint_rejects_a_missing_secret(secret):
    resp = client.post("/webhooks/artifact-changed", json=_ARTIFACT_CHANGED_BODY)
    assert resp.status_code == 401


def test_trigger_endpoint_rejects_a_wrong_secret(secret):
    resp = client.post(
        "/webhooks/artifact-changed", json=_ARTIFACT_CHANGED_BODY, headers={"X-Webhook-Secret": "nope"}
    )
    assert resp.status_code == 401


def test_trigger_endpoint_accepts_the_right_secret(secret):
    resp = client.post(
        "/webhooks/artifact-changed", json=_ARTIFACT_CHANGED_BODY, headers={"X-Webhook-Secret": secret}
    )
    assert resp.status_code == 200


def test_health_needs_no_secret(secret):
    # Deliberately open, so uptime checks / load balancers need no credential.
    assert client.get("/health").status_code == 200


def test_empty_string_secret_is_treated_as_unconfigured(monkeypatch):
    # Regression: a real .env with `WEBHOOK_SECRET=` (present, empty — exactly
    # what .env.example ships) parses to "", not None. require_webhook_secret
    # must treat that the same as unset, or every local-dev deployment that
    # copied .env.example verbatim 401s on its own webhooks.
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "webhook_secret", "")
    resp = client.post("/webhooks/artifact-changed", json=_ARTIFACT_CHANGED_BODY)
    assert resp.status_code == 200


def test_calendar_timer_holds_for_review_when_confident_but_no_project_matched(monkeypatch):
    # Real bug caught via the mock harness: a classification can be genuinely
    # confident about the meeting CLASS while finding no matching project
    # (project_id=None). docs §4.1 — "we never auto-create a new project ...
    # if nothing matches, it goes to human review" — regardless of how sure
    # the class is. Before this guard, this sailed straight into the build
    # graph and wrote real rows (budget_line_items etc.) with no project to
    # attach them to. _run_graph must never be reached in this case.
    async def _fake_ingest(**_kwargs):
        return {
            "id": "intake-1",
            "classification": {
                "project_id": None,
                "new_project_suggested_name": None,
                "meeting_class": "follow_up",
                "sub_type": "budget",
                "confidence": 0.95,
                "reasoning": "confident about class, no project match",
            },
        }

    async def _boom(_state):
        raise AssertionError("_run_graph must not be called when project_id is None")

    monkeypatch.setattr("app.main.ingest_meeting", _fake_ingest)
    monkeypatch.setattr("app.main._run_graph", _boom)

    resp = client.post(
        "/webhooks/calendar-timer",
        json={
            "event_id": "e1", "attendee_emails": [], "language": "en",
            "transcript_text": "n",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending_review", "intake_id": "intake-1"}


def test_orchestrator_run_routes_final_qa_to_agent_8(monkeypatch):
    # Agent 8 (docs §9.2) — a real, narrow check, not the build graph and not
    # a 500 from orchestrator.route()'s NotImplementedError. No plan on file
    # for this project, so it's a clean "nothing to compare against" — the
    # stub model is never even reached.
    from tests.fakes import FakeSupabase

    monkeypatch.setattr("app.db.client.get_supabase", lambda: FakeSupabase())

    resp = client.post(
        "/orchestrator/run",
        json={
            "project_id": "p1", "intake_id": "i1", "meeting_class": "final_qa",
            "sub_type": None, "language": "en", "notes": "n",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "final_qa_checked", "has_scope_switch": False,
        "reason": "no plan on file to compare against",
    }
