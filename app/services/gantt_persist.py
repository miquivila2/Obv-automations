"""Materialize a Gantt draft into rows of the CRM's existing `public.gantt_tasks`,
handling regeneration (follow-up/update mode) without leaving duplicate or orphaned
rows behind.

WHY THIS IS MORE THAN A PLAIN INSERT (docs §5, gap #1/#2 resolution): gantt_tasks
is a flat list of task rows with no version/generation column. A naive "insert the
new draft" on every regeneration would leave the previous generation's rows sitting
next to the new ones — doubling the Gantt (and, downstream, the budget) in the CRM.

Decided with the team: the agent may UPDATE or DELETE rows **it created itself**
(tracked in `agent.gantt_task_ownership`, since gantt_tasks has no source/authorship
column to check). It must NEVER touch a row with no ownership record — that is how
a human-created or human-edited row stays untouched. Matching across generations is
by POSITION (old task at position i <-> new task at position i); matched rows reuse
their existing id so `depends_on` chains stay valid across a regeneration.

Dependency simplification (documented, not hidden — unchanged from before): each
task depends on the one immediately before it in the flattened milestone order.
"""
from __future__ import annotations

import uuid


def load_latest_gantt_tasks(project_id: str) -> list[dict]:
    """Read-only: the project's current Gantt tasks, ordered for display/pricing.
    Used by Budget — not scoped to agent-owned rows, since pricing should reflect
    the whole Gantt as the CRM shows it, human edits included."""
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


def _load_agent_owned_task_ids(project_id: str) -> list[str]:
    """Ordered (by position) ids of gantt_tasks rows the agent itself created for
    this project, per our ownership record — never a human's row."""
    from app.db.client import get_supabase

    rows = (
        get_supabase()
        .schema("agent")
        .table("gantt_task_ownership")
        .select("gantt_task_id,position")
        .eq("project_id", project_id)
        .order("position")
        .execute()
        .data
    )
    return [r["gantt_task_id"] for r in rows]


def plan_gantt_upsert(
    existing_agent_task_ids: list[str], milestones: list[dict], project_id: str, source_draft_id: str | None
) -> dict:
    """Pure: decide update/insert/delete for a Gantt regeneration.

    `existing_agent_task_ids` must already be ordered by position (see
    _load_agent_owned_task_ids) — position i is matched to the new draft's task at
    position i. Matched positions REUSE the existing id (update); new positions
    beyond the old count get a fresh id (insert); old ids beyond the new count are
    surplus (delete). No I/O — fully testable.

    Returns {"to_update": [rows...], "to_insert": [rows...], "to_delete": [ids...]}.
    """
    flat_tasks = [
        {"phase": milestone["name"], "name": task["name"], "duration_days": task["duration_days"]}
        for milestone in milestones
        for task in milestone["tasks"]
    ]

    to_update: list[dict] = []
    to_insert: list[dict] = []
    previous_id: str | None = None

    for position, task in enumerate(flat_tasks):
        reused = position < len(existing_agent_task_ids)
        task_id = existing_agent_task_ids[position] if reused else str(uuid.uuid4())
        row = {
            "id": task_id,
            "project_id": project_id,
            "phase": task["phase"],
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
        (to_update if reused else to_insert).append(row)
        previous_id = task_id

    to_delete = existing_agent_task_ids[len(flat_tasks):]  # surplus from a shrinking regeneration

    return {"to_update": to_update, "to_insert": to_insert, "to_delete": to_delete}


def persist_gantt(*, project_id: str, draft: dict) -> dict:
    """Apply the upsert plan to public.gantt_tasks + keep agent.gantt_task_ownership
    in sync. Returns counts for observability."""
    from app.db.client import get_supabase

    supabase = get_supabase()
    existing_ids = _load_agent_owned_task_ids(project_id)
    plan = plan_gantt_upsert(existing_ids, draft["payload"]["milestones"], project_id, draft.get("source_draft_id"))

    for row in plan["to_update"]:
        supabase.table("gantt_tasks").update({k: v for k, v in row.items() if k != "id"}).eq(
            "id", row["id"]
        ).execute()

    if plan["to_insert"]:
        supabase.table("gantt_tasks").insert(plan["to_insert"]).execute()

    if plan["to_delete"]:
        # Delete order matters: drop the CRM row first, then our ownership record —
        # if this fails partway, we're left with an orphaned ownership record (safe,
        # just stale bookkeeping) rather than an ownership-less CRM row (unsafe: a
        # future regeneration could no longer tell it was ours).
        supabase.table("gantt_tasks").delete().in_("id", plan["to_delete"]).execute()
        supabase.schema("agent").table("gantt_task_ownership").delete().in_(
            "gantt_task_id", plan["to_delete"]
        ).execute()

    ownership_rows = [
        {"gantt_task_id": r["id"], "project_id": project_id, "position": r["position"]}
        for r in plan["to_update"] + plan["to_insert"]
    ]
    if ownership_rows:
        supabase.schema("agent").table("gantt_task_ownership").upsert(
            ownership_rows, on_conflict="gantt_task_id"
        ).execute()

    return {"updated": len(plan["to_update"]), "inserted": len(plan["to_insert"]), "deleted": len(plan["to_delete"])}
