"""The Judge loop's decision logic lives in `_after_judge` and `_after_persist`
(graph edges), so it's unit-testable without running the graph, models, or
Supabase. Core of the shared review loop: approve -> persist -> advance; reject
under the cap -> revise; reject at the cap -> human review.
"""
from langgraph.graph import END

from app.graph.build import _after_judge, _after_persist
from app.graph.nodes.judge import JudgeVerdict


def _state(artifact_type, verdict, judge_round):
    return {
        "current_artifact_type": artifact_type,
        "judge_verdict": verdict,
        "judge_round": judge_round,
    }


def test_approve_routes_to_persist():
    # Approval always goes to the shared persist step first, regardless of type.
    assert _after_judge(_state("wireframe", "approve", 1)) == "persist"
    assert _after_judge(_state("budget", "approve", 1)) == "persist"


def test_reject_under_cap_revises_same_builder():
    # Round 1 of a max-2 loop: go back to the same builder to revise.
    assert _after_judge(_state("gantt", "reject", 1)) == "gantt"


def test_reject_at_cap_goes_to_human_review():
    # Round 2 (== judge_max_rounds): never approved -> flag for a human.
    assert _after_judge(_state("gantt", "reject", 2)) == "human_review"


def test_persist_advances_down_the_chain():
    assert _after_persist({"current_artifact_type": "wireframe"}) == "plan"
    assert _after_persist({"current_artifact_type": "plan"}) == "gantt"
    assert _after_persist({"current_artifact_type": "gantt"}) == "budget"


def test_persist_on_budget_ends_the_chain():
    assert _after_persist({"current_artifact_type": "budget"}) == END


def test_stub_verdict_is_a_clean_approve():
    assert JudgeVerdict.stub().verdict == "approve"
