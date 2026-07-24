"""Agent 3 — Planner.

Reads the notes + the wireframe, produces the needs list (software / hardware /
cloud) and a build plan ordered toward the scope, grouped into logical phases.
Uses DeepSeek V3.2 — one of the two agents deliberately given a stronger model,
because a bad plan cascades into a bad Gantt and a bad budget.

Naming note: this plan's grouping is called "phases" (not "milestones") to avoid
colliding with the Gantt's milestones, which are calendar-month buckets the Gantt
agent derives FROM these phases — they are not the same concept.

Inputs: meeting notes + latest wireframe (once Agent 2 persists one).
Modes: create | follow-up (load latest plan draft first, from
public.project_plan_drafts — the CRM's own drafts table, reused as-is).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.graph.state import BuildState


class PlanNeeds(BaseModel):
    software: list[str] = Field(default_factory=list)
    hardware: list[str] = Field(default_factory=list)
    cloud: list[str] = Field(default_factory=list)


class PlanPhase(BaseModel):
    name: str = Field(..., description="A logical build phase, e.g. 'Core data model'.")
    items: list[str] = Field(..., description="Ordered build steps within this phase.")


class PlanDraft(BaseModel):
    needs: PlanNeeds
    phases: list[PlanPhase]

    @classmethod
    def stub(cls) -> "PlanDraft":
        """Canned output for MODEL_PROVIDER=stub, enough to exercise Gantt after it."""
        return cls(
            needs=PlanNeeds(software=["[stub] framework"], hardware=[], cloud=["[stub] hosting"]),
            phases=[
                PlanPhase(name="[stub] Foundation", items=["Set up project skeleton", "Core data model"]),
                PlanPhase(name="[stub] Features", items=["Build main flows"]),
            ],
        )


async def build_plan(state: BuildState) -> BuildState:
    from app.config import model_id_for
    from app.services.bedrock import chat_model_for
    from app.services.plan_persist import load_latest_plan

    from app.services.wireframe_persist import load_latest_wireframe

    wireframe = load_latest_wireframe(state["project_id"])
    wireframe_ctx = f"\n\nWireframe:\n{wireframe['payload']}" if wireframe else ""

    prior = ""
    if state.get("mode") == "follow_up":
        latest = load_latest_plan(state["project_id"])
        if latest:
            prior = f"\n\nCurrent plan to revise (change ONLY what the notes ask):\n{latest['payload']}"

    model = chat_model_for("planner").with_structured_output(PlanDraft)
    system = (
        "You are Oblivion's planning agent. From the notes and wireframe, produce:\n"
        "1) a needs list split into software, hardware, and cloud;\n"
        "2) a build plan ordered by how to construct toward the scope (dependencies first), "
        "grouped into logical phases, each with ordered items."
    )
    human = f"Meeting notes:\n{state['notes']}{wireframe_ctx}{prior}"

    draft: PlanDraft = await model.ainvoke([("system", system), ("human", human)])

    return {
        **state,
        "current_artifact_type": "plan",
        "draft": {"payload": draft.model_dump(), "brief": state["notes"], "model_id": model_id_for("planner")},
    }
