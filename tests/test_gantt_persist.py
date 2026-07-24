"""plan_gantt_upsert is pure (no Supabase) — the matching/diffing logic that turns
a Gantt regeneration into update/insert/delete operations without duplicating or
orphaning rows in public.gantt_tasks. This is the core of gap #1/#2's fix.
"""
from app.services.gantt_persist import _split_crm_row, plan_gantt_upsert


def _task(name, duration_days=5, description="desc"):
    return {"name": name, "duration_days": duration_days, "description": description}


_MILESTONES_2_TASKS = [{"name": "Month 1", "tasks": [_task("A"), _task("B")]}]
_MILESTONES_3_TASKS = [{"name": "Month 1", "tasks": [_task("A2"), _task("B2"), _task("C")]}]
_MILESTONES_1_TASK = [{"name": "Month 1", "tasks": [_task("A3")]}]


def test_first_generation_is_all_inserts_no_existing_ids():
    plan = plan_gantt_upsert([], _MILESTONES_2_TASKS, "p", None)
    assert len(plan["to_insert"]) == 2
    assert plan["to_update"] == []
    assert plan["to_delete"] == []


def test_same_count_regeneration_is_all_updates_reusing_ids():
    old_ids = ["id-A", "id-B"]
    plan = plan_gantt_upsert(old_ids, _MILESTONES_2_TASKS, "p", None)
    assert [r["id"] for r in plan["to_update"]] == old_ids
    assert plan["to_insert"] == []
    assert plan["to_delete"] == []


def test_growing_regeneration_updates_existing_and_inserts_the_rest():
    old_ids = ["id-A", "id-B"]
    plan = plan_gantt_upsert(old_ids, _MILESTONES_3_TASKS, "p", None)
    assert [r["id"] for r in plan["to_update"]] == old_ids
    assert len(plan["to_insert"]) == 1
    assert plan["to_delete"] == []


def test_shrinking_regeneration_deletes_the_surplus():
    old_ids = ["id-A", "id-B"]
    plan = plan_gantt_upsert(old_ids, _MILESTONES_1_TASK, "p", None)
    assert [r["id"] for r in plan["to_update"]] == ["id-A"]
    assert plan["to_insert"] == []
    assert plan["to_delete"] == ["id-B"]


def test_depends_on_chain_uses_reused_ids_across_regeneration():
    old_ids = ["id-A", "id-B"]
    plan = plan_gantt_upsert(old_ids, _MILESTONES_2_TASKS, "p", None)
    by_id = {r["id"]: r for r in plan["to_update"]}
    assert by_id["id-A"]["depends_on"] == []
    assert by_id["id-B"]["depends_on"] == ["id-A"]


def test_never_touches_a_task_id_outside_the_ownership_list():
    # Only 1 id passed as "ours" -> only position 0 can ever be update/delete;
    # nothing outside existing_agent_task_ids appears in to_update or to_delete.
    plan = plan_gantt_upsert(["id-A"], _MILESTONES_3_TASKS, "p", None)
    touched_ids = {r["id"] for r in plan["to_update"]} | set(plan["to_delete"])
    assert touched_ids == {"id-A"}


def test_description_carried_through_the_row():
    plan = plan_gantt_upsert([], [{"name": "M1", "tasks": [_task("A", description="Builds X.")]}], "p", None)
    assert plan["to_insert"][0]["description"] == "Builds X."


def test_split_crm_row_removes_id_and_description():
    row = {"id": "t-1", "description": "hello", "name": "A", "position": 0}
    crm_row, description = _split_crm_row(row)
    assert "id" not in crm_row and "description" not in crm_row
    assert crm_row == {"name": "A", "position": 0}
    assert description == "hello"
