"""_tracked (app/graph/build.py) wraps every graph node so each execution lands
in agent.runs: a 'running' row on entry, 'success'/'failed' + finished_at on
exit (docs §11 — the table existed since 0001 but nothing wrote to it before).
"""
import pytest

from app.graph.build import _tracked
from tests.fakes import FakeSupabase


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeSupabase()
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake_client)
    return fake_client


async def test_successful_node_run_is_recorded(fake):
    async def _node(state):
        return {**state, "draft": {"model_id": "some-model"}}

    tracked = _tracked("wireframe", _node)
    await tracked({"project_id": "p1", "intake_id": "i1"})

    rows = fake.rows("agent", "runs")
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "wireframe"
    assert rows[0]["project_id"] == "p1"
    assert rows[0]["intake_id"] == "i1"
    assert rows[0]["status"] == "success"
    assert rows[0]["model_id"] == "some-model"
    assert rows[0]["finished_at"] is not None


async def test_sync_node_is_supported(fake):
    def _node(state):
        return {**state}

    tracked = _tracked("orchestrator", _node)
    await tracked({"project_id": "p1"})

    rows = fake.rows("agent", "runs")
    assert rows[0]["status"] == "success"
    assert rows[0]["model_id"] is None  # no draft on this node


async def test_failed_node_is_recorded_and_the_exception_still_propagates(fake):
    async def _node(_state):
        raise RuntimeError("boom")

    tracked = _tracked("budget", _node)
    with pytest.raises(RuntimeError, match="boom"):
        await tracked({"project_id": "p1"})

    rows = fake.rows("agent", "runs")
    assert rows[0]["status"] == "failed"
    assert "boom" in rows[0]["error"]


async def test_unknown_agent_name_is_rejected(fake):
    from app.services.run_tracking import start_run

    with pytest.raises(ValueError):
        start_run(project_id="p1", intake_id=None, agent_name="not-a-real-agent", trigger_source=None)
