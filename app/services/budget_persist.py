"""Persist an approved budget draft into the CRM: line items + a .docx in Storage.

Writes to the CRM's existing `public.budget_line_items` table (the CRM already
models budgets this way) with source='agent', so the Lovable UI can show and let a
human review/edit them. It NEVER modifies or deletes existing rows — it only
inserts new agent-authored lines. The .docx goes to Supabase Storage.

Safety: this is the one budget step that writes to a CRM table. It runs only after
the Judge approves, and only against the environment configured in .env — point
that at a TEST Supabase until the pipeline is validated (docs LOCAL_DEPLOYMENT §4).
"""
from __future__ import annotations

from datetime import datetime, timezone

_BUCKET = "budgets"


def _fetch_project_name(supabase, project_id: str) -> str:
    rows = supabase.table("projects").select("name").eq("id", project_id).limit(1).execute().data
    return rows[0]["name"] if rows else project_id


def persist_budget(*, project_id: str, draft: dict) -> dict:
    """Insert the priced lines into public.budget_line_items and upload the .docx.
    Returns {"line_count", "file_path"}."""
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

    # 2. Insert the line items (agent-authored). No IVA/discount rows — humans add
    #    those in the CRM. We never update/delete existing rows.
    rows = [
        {
            "project_id": project_id,
            "category": li.get("category", ""),
            "description": li.get("description", ""),
            "quantity": float(li["hours"]),      # CRM `quantity` == hours here
            "unit_rate": float(li["unit_rate"]),
            "amount": float(li["amount"]),
            "currency": currency,
            "source": "agent",
            "gantt_task_id": li.get("gantt_task_id"),
            "position": li.get("position", 0),
            "month": li.get("month"),
            "details": li.get("justification"),
        }
        for li in lines
    ]
    supabase.table("budget_line_items").insert(rows).execute()

    return {"line_count": len(rows), "file_path": file_path}
