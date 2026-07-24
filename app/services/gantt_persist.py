"""Materialize a Gantt draft into rows of the CRM's existing `public.gantt_tasks`.

Only inserts new agent-authored rows; never updates or deletes existing tasks.

Dependency simplification (documented, not hidden): each task depends on the one
immediately before it in the flattened milestone order — a single sequential
chain. This is a deliberate, simple default; a human refines real parallel/
independent tasks in the CRM. Task ids are generated client-side (uuid4) so
`depends_on` can reference sibling tasks created in the same batch.
"""
from __future__ import annotations

import uuid


def load_latest_gantt_tasks(project_id: str) -> list[dict]:
    """Read-only: the project's current Gantt tasks, ordered for display/pricing."""
    from app.db.client import get_supabase

    return (
        get_supabase()
        .table("gantt_tasks")
        .select("id,phase,name,duration_days,depends_on,position")
        .eq("project_id", project_id)
        .order("position")
        .execute()
        .data
    )


def build_gantt_rows(project_id: str, milestones: list[dict], source_draft_id: str | None) -> list[dict]:
    """Pure: flatten {milestone: [tasks]} into gantt_tasks rows with generated ids,
    sequential position, and a simple chained depends_on. No I/O — testable directly."""
    rows: list[dict] = []
    position = 0
    previous_id: str | None = None

    for milestone in milestones:
        phase = milestone["name"]
        for task in milestone["tasks"]:
            task_id = str(uuid.uuid4())
            rows.append(
                {
                    "id": task_id,
                    "project_id": project_id,
                    "phase": phase,
                    "name": task["name"],
                    "duration_days": task["duration_days"],
                    "depends_on": [previous_id] if previous_id else [],
                    "assignees": None,
                    "assignee_ids": [],
                    "progress": 0,
                    "anchor_date": None,  # date resolution not designed yet — see docs §9
                    "position": position,
                    "source_draft_id": source_draft_id,
                }
            )
            previous_id = task_id
            position += 1

    return rows


def persist_gantt(*, project_id: str, draft: dict) -> list[dict]:
    """Insert the Gantt's task rows into public.gantt_tasks. Returns the inserted
    rows (Budget reads them back by project_id, so no return-value dependency)."""
    from app.db.client import get_supabase

    rows = build_gantt_rows(project_id, draft["payload"]["milestones"], draft.get("source_draft_id"))
    if not rows:
        return []
    return get_supabase().table("gantt_tasks").insert(rows).execute().data
