"""Persist an approved budget draft into the CRM: line items + a .docx in Storage.

Handles regeneration (follow-up mode) the same way gantt_persist does (docs §5,
gap #1/#2): `budget_line_items` already has a `source` column, so unlike Gantt we
don't need a separate ownership table — we scope directly to `source='agent'`.
Matching across a regeneration is by `gantt_task_id` (the semantic anchor: which
task this line prices), falling back to position for lines with no task link.
A human-added or human-edited line (source='human') is never touched.

Safety: this is a budget step that writes to a CRM table. It runs only after the
Judge approves, and only against the environment configured in .env — point that
at a TEST Supabase until the pipeline is validated (docs LOCAL_DEPLOYMENT §4).
"""
from __future__ import annotations

from datetime import datetime, timezone

_BUCKET = "budgets"


def _fetch_project_name(supabase, project_id: str) -> str:
    rows = supabase.table("projects").select("name").eq("id", project_id).limit(1).execute().data
    return rows[0]["name"] if rows else project_id


def load_latest_budget_lines(project_id: str) -> list[dict]:
    """Read-only: the project's current agent-authored budget lines, for follow-up
    mode's 'revise only what changed' prompt context."""
    from app.db.client import get_supabase

    return (
        get_supabase()
        .table("budget_line_items")
        .select("category,description,quantity,unit_rate,amount,month,details,gantt_task_id")
        .eq("project_id", project_id)
        .eq("source", "agent")
        .order("position")
        .execute()
        .data
    )


def plan_budget_upsert(existing_agent_lines: list[dict], new_lines: list[dict]) -> dict:
    """Pure: decide update/insert/delete for a Budget regeneration.

    `existing_agent_lines` (ordered by position): [{"id", "gantt_task_id", "position"}]
    — source='agent' rows only, queried by the caller. `new_lines`: priced line
    dicts from budget_math.price_line_items (each has gantt_task_id, position, ...).

    Matches by gantt_task_id where present; lines with no gantt_task_id on either
    side are paired by position among themselves. Anything left over in
    `existing_agent_lines` after matching is surplus (delete). No I/O — testable.

    Returns {"to_update": [{"id":..., **line}], "to_insert": [line...], "to_delete": [ids...]}.
    """
    by_task = {l["gantt_task_id"]: l for l in existing_agent_lines if l.get("gantt_task_id")}
    unanchored = iter(l for l in existing_agent_lines if not l.get("gantt_task_id"))

    to_update: list[dict] = []
    to_insert: list[dict] = []
    matched_ids: set[str] = set()

    for line in new_lines:
        task_id = line.get("gantt_task_id")
        match = by_task.get(task_id) if task_id else next(unanchored, None)
        if match:
            to_update.append({"id": match["id"], **line})
            matched_ids.add(match["id"])
        else:
            to_insert.append(line)

    to_delete = [l["id"] for l in existing_agent_lines if l["id"] not in matched_ids]

    return {"to_update": to_update, "to_insert": to_insert, "to_delete": to_delete}


def _row(li: dict, *, project_id: str, currency: str) -> dict:
    return {
        "project_id": project_id,
        "category": li.get("category", ""),
        "description": li.get("description", ""),
        "quantity": float(li["hours"]),  # CRM `quantity` == hours here
        "unit_rate": float(li["unit_rate"]),
        "amount": float(li["amount"]),
        "currency": currency,
        "source": "agent",
        "gantt_task_id": li.get("gantt_task_id"),
        "position": li.get("position", 0),
        "month": li.get("month"),
        "details": li.get("justification"),
    }


def persist_budget(*, project_id: str, draft: dict) -> dict:
    """Upsert the priced lines into public.budget_line_items (agent-owned rows
    only) and upload the .docx. Returns counts for observability."""
    from app.db.client import get_supabase
    from app.services.budget_docx import render_budget_docx

    supabase = get_supabase()
    currency = draft["currency"]
    lines = draft["lines"]

    # 1. Render + upload the .docx (Storage). Path is per-project + timestamped.
    project_name = _fetch_project_name(supabase, project_id)
    docx_bytes = render_budget_docx(
        project_name=project_name, currency=currency, lines=lines, subtotal=draft["subtotal"]
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    file_path = f"{project_id}/budget_{stamp}.docx"
    supabase.storage.from_(_BUCKET).upload(
        file_path,
        docx_bytes,
        {"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    )

    # 2. Upsert the line items — never touches a source='human' row.
    existing_agent_lines = (
        supabase.table("budget_line_items")
        .select("id,gantt_task_id,position")
        .eq("project_id", project_id)
        .eq("source", "agent")
        .order("position")
        .execute()
        .data
    )
    plan = plan_budget_upsert(existing_agent_lines, lines)

    for item in plan["to_update"]:
        supabase.table("budget_line_items").update(_row(item, project_id=project_id, currency=currency)).eq(
            "id", item["id"]
        ).execute()

    if plan["to_insert"]:
        rows = [_row(li, project_id=project_id, currency=currency) for li in plan["to_insert"]]
        supabase.table("budget_line_items").insert(rows).execute()

    if plan["to_delete"]:
        supabase.table("budget_line_items").delete().in_("id", plan["to_delete"]).execute()

    return {
        "file_path": file_path,
        "updated": len(plan["to_update"]),
        "inserted": len(plan["to_insert"]),
        "deleted": len(plan["to_delete"]),
    }
