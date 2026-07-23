"""Agent 4 — Gantt.

Divides the plan into monthly milestones, creates specific tasks, builds the
Gantt. Uses Qwen3-Next-80B — this is structured transformation of an already-
reasoned plan, not reasoning from scratch, so it gets the cheap specialist.

Inputs: the plan (latest). In update mode, also state['progress_summary'] so
tasks/milestones reconcile with real code progress, not just the plan.
Modes: create | follow-up | update.
"""
from __future__ import annotations

from app.graph.state import BuildState
from app.services.artifacts import load_latest
from app.services.bedrock import chat_model_for


async def build_gantt(state: BuildState) -> BuildState:
    model = chat_model_for("gantt")

    plan = load_latest(state["project_id"], "plan")
    plan_ctx = f"Plan:\n{plan['content']}" if plan else "Plan: (none found)"

    update_ctx = ""
    if state.get("mode") == "update" and state.get("progress_summary"):
        update_ctx = (
            f"\n\nReal build progress vs. plan (reconcile tasks/milestones against THIS, "
            f"not just the plan):\n{state['progress_summary']}"
        )

    system = (
        "You are Oblivion's Gantt agent. From the plan, produce monthly milestones, specific "
        "tasks with short descriptions, and a Gantt structure. Every plan item must map to at "
        "least one task; do not invent scope beyond the plan. Return structured JSON with "
        "'milestones' (each with 'name', 'month', 'tasks')."
    )
    human = f"{plan_ctx}{update_ctx}"

    response = await model.ainvoke([("system", system), ("human", human)])
    return {**state, "current_artifact_type": "gantt", "draft": {"raw": response.content}}
