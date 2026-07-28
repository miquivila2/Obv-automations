"""Agent 8 — Final QA (scope-switch detector), docs §9.2.

Deliberately narrow: this is NOT an acceptance/handover agent, and it is NOT
part of the build chain (2-6). It has exactly one job — when a meeting is
classified `final_qa`, compare what the client asks for in that meeting
against the ORIGINAL agreed scope, and if the two have drifted apart, leave a
finding for a human. Nothing more:

  * Read-only against the CRM. Never inserts, updates, or deletes any
    `public.*` row, never changes a status anywhere.
  * Triggers nothing downstream — no other agent reads `agent.qa_findings`.
  * Only writes when it finds a switch. A clean Final QA meeting produces no
    row at all — there is nothing to notify a human about.

Baseline for "original scope": the latest `public.project_plan_drafts` row
(Agent 3's plan — needs + phases). Chosen over the wireframe (UI shape, not
scope) or the raw onboarding transcript (unstructured) because it's already
the structured "what we agreed to build" the rest of the chain (Gantt,
Budget) is built from downstream of.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ScopeCheck(BaseModel):
    has_scope_switch: bool = Field(
        ...,
        description="True only if the notes ask for something materially different from or "
        "beyond the agreed plan — not for ordinary acceptance feedback, bug reports, or "
        "small clarifications.",
    )
    summary: str = Field(..., description="One or two sentences: what changed, or why nothing did.")

    @classmethod
    def stub(cls) -> "ScopeCheck":
        return cls(has_scope_switch=False, summary="[stub] no scope switch detected")


async def run_final_qa_check(
    *, project_id: str, intake_id: str | None, notes: str, trigger_source: str | None = None
) -> dict:
    """Agent 8's entire job. Returns a dict describing the outcome; only
    inserts into `agent.qa_findings` (never public.*) when a switch is found.

    Tracked in agent.runs same as every graph node (migration 0009 added 'qa'
    to that table's agent_name constraint) — Agent 8 runs outside the graph
    (docs §9.2), so it doesn't get app/graph/build.py's `_tracked()` wrapper
    for free; this is that same observability, wired by hand at the one call
    site instead. Before this, Agent 8 had zero rows in agent.runs, ever."""
    from app.services.run_tracking import finish_run, start_run

    run_id = start_run(
        project_id=project_id, intake_id=intake_id, agent_name="qa", trigger_source=trigger_source
    )
    try:
        result = await _execute(project_id=project_id, intake_id=intake_id, notes=notes)
    except Exception as exc:
        finish_run(run_id, status="failed", error=str(exc))
        raise

    from app.config import model_id_for

    finish_run(run_id, status="success", model_id=model_id_for("qa"))
    return result


async def _execute(*, project_id: str, intake_id: str | None, notes: str) -> dict:
    from app.services.plan_persist import load_latest_plan

    plan = load_latest_plan(project_id)
    if plan is None:
        # Nothing to compare against — not an error, just nothing to check yet.
        return {"has_scope_switch": False, "reason": "no plan on file to compare against"}

    from app.config import model_id_for
    from app.services.bedrock import chat_model_for

    model = chat_model_for("qa").with_structured_output(ScopeCheck)
    system = (
        "You are Oblivion's Final QA scope-check agent. Compare what the client asks for in "
        "a Final QA / acceptance meeting against the ORIGINAL agreed plan (needs + phases). "
        "Flag has_scope_switch=true only if the notes ask for something materially different "
        "from or beyond that plan — not for normal acceptance feedback, bug reports, or minor "
        "clarifications. Do not invent a switch that isn't there."
    )
    human = f"Agreed plan:\n{plan['payload']}\n\nFinal QA meeting notes:\n{notes}"
    result: ScopeCheck = await model.ainvoke([("system", system), ("human", human)])

    if not result.has_scope_switch:
        return {"has_scope_switch": False, "summary": result.summary}

    from app.db.client import get_supabase

    row = (
        get_supabase()
        .schema("agent")
        .table("qa_findings")
        .insert(
            {
                "project_id": project_id,
                "intake_id": intake_id,
                "has_scope_switch": True,
                "summary": result.summary,
                "requested_scope": notes,
                "model_id": model_id_for("qa"),
            }
        )
        .execute()
        .data[0]
    )
    return {"has_scope_switch": True, "summary": result.summary, "finding_id": row["id"]}
