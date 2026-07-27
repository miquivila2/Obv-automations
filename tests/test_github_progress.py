"""fetch_code_progress_snapshot (docs §9.3, decided: GitHub commits/PRs) against
a mocked GitHub API (httpx.MockTransport — no real network) and a fake Supabase
for the project_repos lookup + code_progress write.
"""
import httpx
import pytest

from app.services.github_progress import fetch_code_progress_snapshot
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
            json=[
                {
                    "sha": "abc1234567",
                    "commit": {
                        "message": "Fix login bug\n\nmore detail in the body",
                        "author": {"name": "Alice", "date": "2026-07-20T10:00:00Z"},
                    },
                }
            ],
        )
    if request.url.path.endswith("/pulls"):
        return httpx.Response(
            200,
            json=[
                {"number": 12, "state": "closed", "merged_at": "2026-07-21T00:00:00Z", "title": "Add feature"},
                {"number": 13, "state": "open", "merged_at": None, "title": "WIP thing"},
            ],
        )
    return httpx.Response(404)


_RealAsyncClient = httpx.AsyncClient


def _mock_github(monkeypatch) -> None:
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_github_handler)
        return _RealAsyncClient(*args, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", factory)


async def test_no_repo_configured_raises_a_clear_error(fake):
    with pytest.raises(ValueError, match="No GitHub repo configured"):
        await fetch_code_progress_snapshot("p-unknown")


async def test_fetches_and_summarizes_commits_and_pull_requests(fake, monkeypatch):
    fake.seed("agent", "project_repos", [{"project_id": "p1", "owner": "acme", "repo": "widgets"}])
    _mock_github(monkeypatch)

    summary = await fetch_code_progress_snapshot("p1")

    assert "abc1234" in summary
    assert "Fix login bug" in summary
    assert "#12" in summary and "merged" in summary
    assert "#13" in summary and "open" in summary


async def test_snapshot_is_persisted_to_code_progress(fake, monkeypatch):
    fake.seed("agent", "project_repos", [{"project_id": "p1", "owner": "acme", "repo": "widgets"}])
    _mock_github(monkeypatch)

    await fetch_code_progress_snapshot("p1")

    rows = fake.rows("agent", "code_progress")
    assert len(rows) == 1
    assert rows[0]["project_id"] == "p1"
    assert rows[0]["source_ref"] == "abc1234567"
