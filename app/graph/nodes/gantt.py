"""Agent 4 — Gantt.

Divides the plan into monthly milestones, creates specific tasks. Uses
Qwen3-Next-80B — structured transformation of an already-reasoned plan, not
reasoning from scratch, so it gets the cheap specialist.

Inputs: the latest plan draft (public.project_plan_drafts). In update mode, also
state['progress_summary'] so tasks/milestones reconcile with real code progress,
not just the plan.
Modes: create | follow-up | update.

Persistence materializes real rows in public.gantt_tasks (app.services.
gantt_persist) — not a JSON blob — since that's how the CRM already models a
Gantt. `source_draft_id` links each task back to the plan draft it came from.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.graph.state import BuildState


class GanttTaskItem(BaseModel):
    name: str
    duration_days: int = Field(..., gt=0)


class GanttMilestone(BaseModel):
    name: str = Field(..., description="A month label or phase name, e.g. 'Month 1' or 'July'.")
    tasks: list[GanttTaskItem]


class GanttDraft(BaseModel):
    milestones: list[GanttMilestone]

    @classmethod
    def stub(cls) -> "GanttDraft":
        return cls(
            milestones=[
                GanttMilestone(
                    name="[stub] Month 1",
                    tasks=[GanttTaskItem(name="[stub] Set up project skeleton", duration_days=5)],
                )
            ]
        )


async def build_gantt(state: BuildState) -> BuildState:
    from app.services.bedrock import chat_model_for
    from app.services.plan_persist import load_latest_plan

    plan = load_latest_plan(state["project_id"])
    plan_ctx = f"Plan:\n{plan['payload']}" if plan else "Plan: (none found)"

    prior_ctx = ""
    if state.get("mode") == "follow_up":
        from app.services.gantt_persist import load_latest_gantt_tasks

        existing_tasks = load_latest_gantt_tasks(state["project_id"])
        if existing_tasks:
            prior_ctx = (
                f"\n\nCurrent Gantt tasks to revise (change ONLY what the notes ask):\n{existing_tasks}"
            )

    update_ctx = ""
    if state.get("mode") == "update" and state.get("progress_summary"):
        update_ctx = (
            f"\n\nReal build progress vs. plan (reconcile tasks/milestones against THIS, "
            f"not just the plan):\n{state['progress_summary']}"
        )

    model = chat_model_for("gantt").with_structured_output(GanttDraft)
    system = (
        "You are Oblivion's Gantt agent. From the plan, produce monthly milestones with "
        "specific tasks (name + duration in days). Every plan item must map to at least one "
        "task; do not invent scope beyond the plan."
    )
    human = f"{plan_ctx}{prior_ctx}{update_ctx}"

    draft: GanttDraft = await model.ainvoke([("system", system), ("human", human)])

    return {
        **state,
        "current_artifact_type": "gantt",
        "draft": {"payload": draft.model_dump(), "source_draft_id": plan.get("id") if plan else None},
    }
