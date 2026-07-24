"""The Judge loop's decision logic lives in `_after_judge` (a graph edge), so it's
unit-testable without running the graph, Bedrock, or Supabase. This is the core of
the shared review loop: approve -> advance, reject under the cap -> revise, reject
at the cap -> human review.
"""
from langgraph.graph import END

from app.graph.build import _after_judge
from app.graph.nodes.judge import JudgeVerdict


def _state(artifact_type, verdict, judge_round):
    return {
        "current_artifact_type": artifact_type,
        "judge_verdict": verdict,
        "judge_round": judge_round,
    }


def test_approve_advances_to_next_artifact():
    assert _after_judge(_state("wireframe", "approve", 1)) == "plan"
    assert _after_judge(_state("plan", "approve", 1)) == "gantt"
    assert _after_judge(_state("gantt", "approve", 1)) == "budget"


def test_approve_on_budget_ends_the_chain():
    assert _after_judge(_state("budget", "approve", 1)) == END


def test_reject_under_cap_revises_same_builder():
    # Round 1 of a max-2 loop: go back to the same builder to revise.
    assert _after_judge(_state("gantt", "reject", 1)) == "gantt"


def test_reject_at_cap_goes_to_human_review():
    # Round 2 (== judge_max_rounds): never approved -> flag for a human.
    assert _after_judge(_state("gantt", "reject", 2)) == "human_review"


def test_stub_verdict_is_a_clean_approve():
    v = JudgeVerdict.stub()
    assert v.verdict == "approve"
