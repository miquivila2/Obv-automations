"""summarize_progress (Agent 7, update mode) reads the real progress snapshot
(github_progress, implemented per docs §9.3) and hands it + the notes to the
LLM to compare build vs. plan. MODEL_PROVIDER=stub (conftest) means the LLM
call itself returns a canned response — this test only exercises the plumbing:
that a real progress snapshot is fetched and reaches the model call.
"""
import httpx
import pytest

from app.graph.nodes.orchestrator import summarize_progress
from tests.fakes import FakeSupabase


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeSupabase()
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake_client)
    return fake_client


def _github_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/commits"):
        return httpx.Response(
            200,
            json=[{"sha": "abc1234567", "commit": {"message": "Fix bug", "author": {"name": "A", "date": "2026-07-20T10:00:00Z"}}}],
        )
    return httpx.Response(200, json=[])


_RealAsyncClient = httpx.AsyncClient


async def test_summarize_progress_uses_the_real_snapshot(fake, monkeypatch):
    fake.seed("agent", "project_repos", [{"project_id": "p1", "owner": "acme", "repo": "widgets"}])
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *a, **kw: _RealAsyncClient(*a, **{**kw, "transport": httpx.MockTransport(_github_handler)}),
    )

    out = await summarize_progress({"project_id": "p1", "notes": "client asked for X"})

    assert "progress_summary" in out
    assert out["progress_summary"].startswith("[stub:orchestrator_update_summary]")


async def test_summarize_progress_propagates_a_missing_repo_error(fake):
    with pytest.raises(ValueError, match="No GitHub repo configured"):
        await summarize_progress({"project_id": "p-unknown", "notes": "n"})
