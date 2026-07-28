"""SAFETY REVIEW: the mock harness must be structurally incapable of touching
production — the real CRM, real Plaud, or real GitHub — no matter what gets
called. These tests exercise the guards directly, not just "does the demo work".
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.mock.store import MockDatabaseError, MockStore


@pytest.fixture
def mock_settings(monkeypatch, tmp_path):
    """Force DATA_SOURCE=mock for the duration of a test, pointed at a scratch
    file so tests never touch a developer's real mock_data/crm.json.

    Clears BOTH memoization layers: app.mock.store's own `_STORE` singleton,
    AND app.db.client.get_supabase's `@lru_cache` — they are two separate
    caches, and a test that only cleared one (as an earlier version of this
    fixture did) leaves get_supabase() returning a STALE store from whatever
    test ran before it, silently reading/writing the wrong instance."""
    from app.config import get_settings
    from app.db.client import get_supabase

    settings = get_settings()
    monkeypatch.setattr(settings, "data_source", "mock")
    monkeypatch.setattr(settings, "mock_data_path", str(tmp_path / "crm.json"))
    import app.mock.store as store_module

    monkeypatch.setattr(store_module, "_STORE", None)
    get_supabase.cache_clear()
    yield settings
    get_supabase.cache_clear()


# ---------------------------------------------------------------------------
# get_supabase() must never construct a real client in mock mode
# ---------------------------------------------------------------------------
def test_get_supabase_returns_the_mock_store_in_mock_mode(mock_settings):
    from app.db.client import get_supabase

    get_supabase.cache_clear()
    client = get_supabase()
    assert isinstance(client, MockStore)


def test_get_supabase_never_imports_the_real_supabase_client_in_mock_mode(mock_settings, monkeypatch):
    """If `create_client` were ever called in mock mode, that's a real network
    client aimed at whatever SUPABASE_URL happens to be — even a placeholder
    value is the wrong failure mode (a hang/DNS error) vs. never calling it."""
    import supabase as supabase_pkg

    def _boom(*_a, **_kw):
        raise AssertionError("create_client was called while DATA_SOURCE=mock")

    monkeypatch.setattr(supabase_pkg, "create_client", _boom)

    from app.db.client import get_supabase

    get_supabase.cache_clear()
    get_supabase()  # must not raise


# ---------------------------------------------------------------------------
# The checkpointer must never open a real Postgres connection in mock mode
# ---------------------------------------------------------------------------
async def test_checkpointer_is_in_memory_in_mock_mode(mock_settings):
    from langgraph.checkpoint.memory import MemorySaver

    from app.db.checkpointer import get_checkpointer

    async with get_checkpointer() as saver:
        assert isinstance(saver, MemorySaver)


# ---------------------------------------------------------------------------
# Plaud must refuse to run in mock mode, from its one real choke point
# ---------------------------------------------------------------------------
async def test_plaud_call_tool_refuses_in_mock_mode(mock_settings):
    from app.services.plaud_client import _call_tool

    with pytest.raises(RuntimeError, match="DATA_SOURCE=mock"):
        await _call_tool("list_files", {})


async def test_plaud_fetch_transcript_refuses_in_mock_mode(mock_settings):
    from app.services.plaud_client import fetch_transcript

    with pytest.raises(RuntimeError, match="DATA_SOURCE=mock"):
        await fetch_transcript(event_id="e1", plaud_note_id="n1")


# ---------------------------------------------------------------------------
# GitHub must never make a real HTTP call in mock mode
# ---------------------------------------------------------------------------
async def test_github_progress_never_calls_httpx_in_mock_mode(mock_settings, monkeypatch):
    from app.mock.store import get_mock_store

    store = get_mock_store()
    store.replace_table(
        "agent", "project_repos", [{"id": "r1", "project_id": "p1", "owner": "acme", "repo": "widgets"}]
    )

    def _boom(*_a, **_kw):
        raise AssertionError("httpx.AsyncClient was constructed while DATA_SOURCE=mock")

    monkeypatch.setattr("httpx.AsyncClient", _boom)

    from app.services.github_progress import fetch_code_progress_snapshot

    summary = await fetch_code_progress_snapshot("p1")
    assert "acme/widgets" in summary
    assert "commit" in summary.lower()


async def test_github_progress_still_fails_loud_on_a_missing_repo_in_mock_mode(mock_settings):
    from app.services.github_progress import fetch_code_progress_snapshot

    with pytest.raises(ValueError, match="No GitHub repo configured"):
        await fetch_code_progress_snapshot("no-such-project")


# ---------------------------------------------------------------------------
# The 4 production trigger endpoints must be structurally disabled in mock mode
# ---------------------------------------------------------------------------
_PRODUCTION_ENDPOINTS = [
    ("/webhooks/calendar-timer", {
        "event_id": "e1", "attendee_emails": [], "language": "en", "transcript_text": "x",
    }),
    ("/webhooks/artifact-changed", {"project_id": "p1", "type": "wireframe", "source": "human"}),
    ("/orchestrator/run", {
        "project_id": "p1", "intake_id": "i1", "meeting_class": "onboarding", "language": "en", "notes": "n",
    }),
    ("/internal/calendar-timer/tick", {}),
]


@pytest.mark.parametrize("path,body", _PRODUCTION_ENDPOINTS)
def test_production_endpoints_are_disabled_in_mock_mode(mock_settings, path, body):
    from app.main import app

    client = TestClient(app)
    resp = client.post(path, json=body)
    assert resp.status_code == 409
    assert "/mock/api" in resp.json()["detail"]


def test_require_not_mock_mode_is_a_noop_outside_mock_mode(monkeypatch):
    """The guard itself, isolated from what runs after it. Exercising the full
    endpoints here would mean actually reaching the graph/checkpointer — real
    infrastructure this test suite has no business touching just to prove one
    dependency function doesn't raise when data_source != 'mock'."""
    from app.config import get_settings
    from app.main import require_not_mock_mode

    monkeypatch.setattr(get_settings(), "data_source", "supabase")
    require_not_mock_mode()  # must not raise

    monkeypatch.setattr(get_settings(), "data_source", "mock")
    with pytest.raises(HTTPException) as exc_info:
        require_not_mock_mode()
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# The mock console itself must not be reachable when NOT in mock mode
# ---------------------------------------------------------------------------
def test_mock_console_is_not_mounted_outside_mock_mode(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_source", "supabase")

    # Re-import app fresh so the conditional router-mount re-evaluates against
    # the patched setting (the module-level `if` runs once, at import time).
    import importlib

    import app.main as main_module

    importlib.reload(main_module)
    client = TestClient(main_module.app)
    resp = client.get("/mock")
    assert resp.status_code == 404

    # Restore for any test that runs after this one in the same process.
    monkeypatch.setattr(get_settings(), "data_source", "mock")
    importlib.reload(main_module)


# ---------------------------------------------------------------------------
# The store itself: unknown tables and constraint violations must be loud
# ---------------------------------------------------------------------------
def test_querying_an_unknown_table_raises(tmp_path):
    store = MockStore(tmp_path / "crm.json")
    with pytest.raises(MockDatabaseError, match="does not exist"):
        store.table("not_a_real_table")


def test_unique_constraint_is_enforced_on_insert(tmp_path):
    store = MockStore(tmp_path / "crm.json")
    store.schema("agent").table("meeting_intake").insert({"event_id": "e1"}).execute()
    with pytest.raises(MockDatabaseError, match="unique constraint"):
        store.schema("agent").table("meeting_intake").insert({"event_id": "e1"}).execute()


# ---------------------------------------------------------------------------
# The runner must never build for a confident classification with no project
# ---------------------------------------------------------------------------
async def test_runner_holds_for_review_when_confident_but_no_project_matched(mock_settings):
    # Same production bug as test_main.py's calendar_timer test, exercised
    # through the actual mock pipeline runner instead of a stubbed endpoint:
    # attendees that match no seeded project, but a transcript with an
    # explicit class keyword (so the stub IS confident about the class).
    from app.mock.runner import run_pipeline_for_event
    from app.mock.store import get_mock_store

    store = get_mock_store()
    store.replace_table(
        "public",
        "events",
        [
            {
                "id": "e-unmatched",
                "title": "Unmatched attendee follow-up",
                "attendee_ids": ["nobody@unknown-client.example"],
                "language": "en",
                "transcript": "Quick follow-up on the budget for this project.",
            }
        ],
    )

    trace = await run_pipeline_for_event("e-unmatched")

    assert trace.ok is True
    assert trace.result["outcome"] == "pending_review"
    assert trace.result["reason"] == "no_project_match"
    assert trace.result["classification"]["confidence"] >= 0.70
    assert trace.result["classification"]["project_id"] is None
    # The actual regression: no orphaned budget rows with project_id=None.
    assert store.rows("public", "budget_line_items") == []
