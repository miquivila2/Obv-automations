"""plan_budget_upsert is pure (no Supabase) — the matching/diffing logic that turns
a Budget regeneration into update/insert/delete operations, scoped to source='agent'
rows only (docs §5, gap #1/#2's Budget half).
"""
from app.services.budget_persist import plan_budget_upsert


def _existing(id_, gantt_task_id=None, position=0):
    return {"id": id_, "gantt_task_id": gantt_task_id, "position": position}


def _new_line(gantt_task_id=None, hours=10):
    return {"category": "Dev", "hours": hours, "gantt_task_id": gantt_task_id, "position": 0}


def test_matches_by_gantt_task_id_and_updates():
    existing = [_existing("line-1", gantt_task_id="task-A")]
    new = [_new_line(gantt_task_id="task-A", hours=20)]
    plan = plan_budget_upsert(existing, new)
    assert plan["to_update"] == [{"id": "line-1", **new[0]}]
    assert plan["to_insert"] == []
    assert plan["to_delete"] == []


def test_new_task_with_no_existing_match_is_inserted():
    existing = [_existing("line-1", gantt_task_id="task-A")]
    new = [_new_line(gantt_task_id="task-B")]
    plan = plan_budget_upsert(existing, new)
    assert plan["to_update"] == []
    assert plan["to_insert"] == new
    assert plan["to_delete"] == ["line-1"]  # old line for task-A has no match -> surplus


def test_unanchored_lines_pair_by_position():
    existing = [_existing("line-1", gantt_task_id=None, position=0)]
    new = [_new_line(gantt_task_id=None, hours=5)]
    plan = plan_budget_upsert(existing, new)
    assert plan["to_update"] == [{"id": "line-1", **new[0]}]
    assert plan["to_delete"] == []


def test_unmatched_existing_lines_are_deleted():
    existing = [_existing("line-1", gantt_task_id="task-A"), _existing("line-2", gantt_task_id="task-B")]
    new = [_new_line(gantt_task_id="task-A")]
    plan = plan_budget_upsert(existing, new)
    assert plan["to_delete"] == ["line-2"]


def test_human_lines_never_appear_because_caller_filters_by_source():
    # plan_budget_upsert never sees source='human' rows — the caller (persist_budget)
    # queries with .eq("source", "agent"), so this documents the contract via a
    # trivial case: an empty existing list (as if all rows were human-owned and
    # filtered out) only ever produces inserts, never touches anything.
    plan = plan_budget_upsert([], [_new_line(gantt_task_id="task-A")])
    assert plan["to_update"] == []
    assert plan["to_delete"] == []
