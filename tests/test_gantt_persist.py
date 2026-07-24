"""build_gantt_rows is pure (no Supabase) — the flattening + dependency-chaining
logic that turns a Gantt draft into public.gantt_tasks rows. Fully testable
without a database.
"""
from app.services.gantt_persist import build_gantt_rows


def test_flattens_milestones_into_rows_with_phase():
    milestones = [{"name": "Month 1", "tasks": [{"name": "A", "duration_days": 5}]}]
    rows = build_gantt_rows("proj-1", milestones, source_draft_id="draft-1")
    assert len(rows) == 1
    assert rows[0]["phase"] == "Month 1"
    assert rows[0]["name"] == "A"
    assert rows[0]["project_id"] == "proj-1"
    assert rows[0]["source_draft_id"] == "draft-1"


def test_first_task_has_no_dependency():
    milestones = [{"name": "M1", "tasks": [{"name": "A", "duration_days": 1}]}]
    rows = build_gantt_rows("p", milestones, None)
    assert rows[0]["depends_on"] == []


def test_tasks_chain_sequentially_across_milestones():
    milestones = [
        {"name": "M1", "tasks": [{"name": "A", "duration_days": 1}, {"name": "B", "duration_days": 2}]},
        {"name": "M2", "tasks": [{"name": "C", "duration_days": 3}]},
    ]
    rows = build_gantt_rows("p", milestones, None)
    ids = {r["name"]: r["id"] for r in rows}
    by_name = {r["name"]: r for r in rows}

    assert by_name["A"]["depends_on"] == []
    assert by_name["B"]["depends_on"] == [ids["A"]]
    assert by_name["C"]["depends_on"] == [ids["B"]]


def test_position_is_sequential_across_all_tasks():
    milestones = [
        {"name": "M1", "tasks": [{"name": "A", "duration_days": 1}, {"name": "B", "duration_days": 1}]},
    ]
    rows = build_gantt_rows("p", milestones, None)
    assert [r["position"] for r in rows] == [0, 1]


def test_required_not_null_columns_have_safe_defaults():
    rows = build_gantt_rows("p", [{"name": "M1", "tasks": [{"name": "A", "duration_days": 1}]}], None)
    row = rows[0]
    assert row["assignee_ids"] == []
    assert row["progress"] == 0
    assert row["depends_on"] == []  # NOT NULL column, never None
