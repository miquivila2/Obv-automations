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


def test_orchestrator_run_rejects_final_qa_without_erroring():
    # Deliberately deferred (docs §9.2) — must be a clean no-op, not a 500 from
    # orchestrator.route()'s NotImplementedError.
    resp = client.post(
        "/orchestrator/run",
        json={
            "project_id": "p1", "intake_id": "i1", "meeting_class": "final_qa",
            "sub_type": None, "language": "en", "notes": "n",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "final_qa_unhandled"}
