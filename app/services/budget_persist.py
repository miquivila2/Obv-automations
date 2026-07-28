"""Persist an approved budget draft into the CRM: line items + a PDF in Storage,
plus the Axo Capital-format document metadata (agent.budget_documents) —
monthly discounts, IVA, contingency, market comparison, milestones.

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


def _load_budget_document_row(supabase, project_id: str) -> dict | None:
    rows = (
        supabase.schema("agent")
        .table("budget_documents")
        .select("*")
        .eq("project_id", project_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def load_assembled_budget_document(project_id: str) -> dict | None:
    """Read-only: re-assemble the full Axo Capital-format document (months,
    discounts, IVA, market comparison, milestones) from whatever is currently
    persisted — agent.budget_documents for the human-entered fields plus
    public.budget_line_items for the priced lines. Reflects the LATEST human
    edits (e.g. via the mock console's edit form), not a stale snapshot from
    whenever Agent 5 last ran. Returns None if no budget exists yet for this
    project. Shared by the mock runner (trace display) and the mock console's
    JSON/PDF export endpoints — one assembly path, not two."""
    from app.db.client import get_supabase
    from app.services.budget_document import assemble_budget_document

    supabase = get_supabase()
    doc_row = _load_budget_document_row(supabase, project_id)
    if doc_row is None:
        return None

    lines = load_latest_budget_lines(project_id)
    if not lines:
        return None

    project_name = _fetch_project_name(supabase, project_id)
    return assemble_budget_document(
        project_name=project_name,
        currency=doc_row["currency"],
        priced_lines=[{**li, "hours": li["quantity"], "justification": li.get("details")} for li in lines],
        iva_rate=doc_row["iva_rate"],
        discount_pct_by_month=doc_row.get("discount_pct_by_month") or {},
        contingency_pct=doc_row.get("contingency_pct"),
        milestones=doc_row.get("milestones") or [],
    )


def persist_budget(*, project_id: str, draft: dict) -> dict:
    """Upsert the priced lines into public.budget_line_items (agent-owned rows
    only), assemble the full Axo Capital-format document (months, discounts,
    IVA, market comparison, milestones — see app.services.budget_document),
    upload it as a PDF, and upsert agent.budget_documents. Returns counts for
    observability.

    CRITICAL on regeneration: discount_pct_by_month, contingency_pct, and each
    milestone's part_pct are HUMAN-ENTERED fields. If a document already
    exists for this project, those human edits are carried forward as-is —
    only genuinely NEW months (never seen before) get a fresh 0% default.
    Regenerating the budget must never silently wipe out a discount or
    contingency percentage a human already set."""
    from app.config import get_settings
    from app.db.client import get_supabase
    from app.services.budget_document import assemble_budget_document, default_milestones
    from app.services.budget_pdf import render_budget_pdf

    supabase = get_supabase()
    currency = draft["currency"]
    lines = draft["lines"]
    project_name = _fetch_project_name(supabase, project_id)

    # Preserve human-entered fields across regenerations (see docstring above).
    existing_doc = _load_budget_document_row(supabase, project_id)
    months_in_draft = list(dict.fromkeys(li.get("month") or "Unscheduled" for li in lines))

    discount_pct_by_month = dict((existing_doc or {}).get("discount_pct_by_month") or {})
    for month in months_in_draft:
        discount_pct_by_month.setdefault(month, 0)

    contingency_pct = (existing_doc or {}).get("contingency_pct")
    iva_rate = (existing_doc or {}).get("iva_rate") or get_settings().budget_default_iva_rate
    milestones = (existing_doc or {}).get("milestones") or default_milestones(months_in_draft)

    document = assemble_budget_document(
        project_name=project_name,
        currency=currency,
        priced_lines=lines,
        iva_rate=iva_rate,
        discount_pct_by_month=discount_pct_by_month,
        contingency_pct=contingency_pct,
        milestones=milestones,
    )

    # 1. Render + upload the PDF (Storage). Path is per-project + timestamped.
    pdf_bytes = render_budget_pdf(project_name=project_name, document=document)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    file_path = f"{project_id}/budget_{stamp}.pdf"
    supabase.storage.from_(_BUCKET).upload(
        file_path, pdf_bytes, {"content-type": "application/pdf"}
    )

    # 2. Upsert agent.budget_documents — one row per project, human fields preserved.
    supabase.schema("agent").table("budget_documents").upsert(
        {
            "project_id": project_id,
            "currency": currency,
            "iva_rate": iva_rate,
            "discount_pct_by_month": discount_pct_by_month,
            "contingency_pct": contingency_pct,
            "milestones": milestones,
            "market_comparison": document["market_comparison"],
            "model_id": draft.get("model_id"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="project_id",
    ).execute()

    # 3. Upsert the line items — never touches a source='human' row.
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
