"""judge() writes one agent.artifact_feedback row per round, keyed by a stable
draft_ref_id that's fresh for a new artifact review and reused across a
reject-revise loop (see app/graph/nodes/judge.py). MODEL_PROVIDER=stub (set in
conftest) means the Judge always approves (JudgeVerdict.stub()) — the
approve/reject *decision* is covered by test_judge_loop.py; this file covers
the feedback write itself.
"""
import pytest

from app.graph.nodes.judge import judge
from tests.fakes import FakeSupabase


@pytest.fixture
def fake(monkeypatch):
    fake_client = FakeSupabase()
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake_client)
    return fake_client


async def test_first_round_writes_feedback_with_a_fresh_ref(fake):
    out = await judge(
        {"current_artifact_type": "wireframe", "notes": "n", "draft": {"payload": {}}, "judge_round": 0}
    )
    rows = fake.rows("agent", "artifact_feedback")
    assert len(rows) == 1
    assert rows[0]["round"] == 1
    assert rows[0]["artifact_type"] == "wireframe"
    assert rows[0]["verdict"] == "approve"
    assert rows[0]["artifact_ref"] == out["draft_ref_id"]
    assert rows[0]["judge_model_id"]


async def test_revise_round_reuses_the_same_ref(fake):
    first = await judge({"current_artifact_type": "gantt", "notes": "n", "draft": {"payload": {}}, "judge_round": 0})
    second = await judge({**first, "judge_round": first["judge_round"]})  # simulate a reject-then-revise call
    rows = fake.rows("agent", "artifact_feedback")
    assert [r["round"] for r in rows] == [1, 2]
    assert rows[0]["artifact_ref"] == rows[1]["artifact_ref"] == second["draft_ref_id"]


async def test_new_artifact_after_round_reset_gets_a_new_ref(fake):
    # persist() resets judge_round to 0 when the chain advances to the next
    # artifact type (see app/graph/build.py) — simulated here directly.
    wireframe_result = await judge(
        {"current_artifact_type": "wireframe", "notes": "n", "draft": {"payload": {}}, "judge_round": 0}
    )
    plan_result = await judge(
        {"current_artifact_type": "plan", "notes": "n", "draft": {"payload": {}}, "judge_round": 0}
    )
    assert plan_result["draft_ref_id"] != wireframe_result["draft_ref_id"]
    rows = fake.rows("agent", "artifact_feedback")
    assert [r["round"] for r in rows] == [1, 1]  # each is round 1 of its own artifact
