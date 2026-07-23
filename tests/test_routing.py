"""The Orchestrator's routing is deterministic code, so it's fully unit-testable
without Bedrock or Supabase — that's the whole point of keeping it out of an LLM.
"""
import pytest

from app.graph.nodes.orchestrator import route


def test_onboarding_starts_full_chain_at_wireframe():
    out = route({"meeting_class": "onboarding"})
    assert out["entry_agent"] == "wireframe"
    assert out["mode"] == "create"


def test_update_enters_at_gantt():
    out = route({"meeting_class": "update"})
    assert out["entry_agent"] == "gantt"
    assert out["mode"] == "update"


def test_follow_up_enters_at_its_sub_type():
    out = route({"meeting_class": "follow_up", "sub_type": "budget"})
    assert out["entry_agent"] == "budget"
    assert out["mode"] == "follow_up"


def test_follow_up_without_sub_type_is_an_error():
    with pytest.raises(ValueError):
        route({"meeting_class": "follow_up", "sub_type": None})


def test_final_qa_is_not_routable_yet():
    # Documented gap (§9.2): surfaced loudly, not silently mis-routed.
    with pytest.raises(NotImplementedError):
        route({"meeting_class": "final_qa"})
