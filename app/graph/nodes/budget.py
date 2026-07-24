"""Agent 5 — Budget.

Produces a priced, justified budget in the Axo Capital format (per-item hours x
rate, monthly grouping, market comparison, milestone payments) and persists it as
line items in the CRM plus a .docx in Storage.

CRITICAL (docs §5.2): the LLM generates the line items — category, description,
hours, tier, month, justification — but the ARITHMETIC (hours x rate, subtotal)
is computed in code (app.services.budget_math). An LLM getting a multiplication
wrong in a client-facing document is a real, avoidable risk.

Locked decisions (docs §5): currency by meeting language (USD/EN, MXN/ES, project
override allowed); rates one-or-two-tier from the CRM; NO automatic IVA or
discounts — a human adds those in the CRM.

Inputs: the project's Gantt tasks (public.gantt_tasks) + past-budget examples
(few-shot, pending the example-library decision). Output draft is priced here and
persisted by the graph's persist step (app.services.budget_persist), which upserts
agent-owned lines rather than duplicating them on a follow-up regeneration
(docs §5, gap #1/#2).

Modes: create | follow-up (load the current agent-authored lines first, revise
only what the notes ask — matches Wireframe/Planner's pattern).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.graph.state import BuildState


class BudgetLine(BaseModel):
    category: str = Field(..., description="Short grouping, e.g. 'Money-path fixes', 'Compliance'.")
    description: str = Field(..., description="What this line delivers, one or two sentences.")
    hours: float = Field(..., gt=0, description="Estimated hours for this line. NOT a cost.")
    tier: str = Field("standard", description="Rate tier name; 'standard' unless the project has tiers.")
    month: str | None = Field(None, description="Delivery month label, e.g. 'July', for monthly grouping.")
    gantt_task_id: str | None = Field(None, description="The gantt task this line prices, if it maps to one.")
    justification: str = Field(..., description="One-line justification of the hours (Axo style).")


class BudgetDraft(BaseModel):
    line_items: list[BudgetLine]

    @classmethod
    def stub(cls) -> "BudgetDraft":
        """Canned output for MODEL_PROVIDER=stub: two plausible lines so the pricing
        math and .docx render can be exercised end-to-end without a real model."""
        return cls(
            line_items=[
                BudgetLine(
                    category="Development",
                    description="[stub] Core feature implementation.",
                    hours=40,
                    tier="standard",
                    month="Month 1",
                    justification="[stub] estimate.",
                ),
                BudgetLine(
                    category="Testing",
                    description="[stub] Automated test coverage.",
                    hours=16,
                    tier="standard",
                    month="Month 1",
                    justification="[stub] estimate.",
                ),
            ]
        )


async def build_budget(state: BuildState) -> BuildState:
    """Generate priced budget line items and stash the draft in state. Persistence
    (line items + .docx) is the graph's persist step, not this node."""
    from app.config import model_id_for
    from app.services.bedrock import chat_model_for
    from app.services.budget_math import price_line_items
    from app.services.gantt_persist import load_latest_gantt_tasks
    from app.services.rates import resolve_currency, resolve_rates

    rate_by_tier, project_preferred = resolve_rates(state["project_id"])
    currency = resolve_currency(state["language"], project_preferred)

    gantt_tasks = load_latest_gantt_tasks(state["project_id"])
    gantt_ctx = "\n".join(
        f"- id={t['id']} phase={t['phase']!r} name={t['name']!r} duration_days={t['duration_days']}"
        for t in gantt_tasks
    ) or "(no Gantt tasks found)"

    prior = ""
    if state.get("mode") == "follow_up":
        from app.services.budget_persist import load_latest_budget_lines

        existing_lines = load_latest_budget_lines(state["project_id"])
        if existing_lines:
            prior = f"\n\nCurrent budget lines to revise (change ONLY what the notes ask):\n{existing_lines}"

    model = chat_model_for("budget").with_structured_output(BudgetDraft)
    system = (
        "You are Oblivion's budget agent. From the Gantt tasks, produce budget LINE ITEMS "
        "in the Axo Capital style: each line has a category, a description, an hours estimate, "
        "a rate tier, an optional delivery month, the gantt_task_id it prices, and a one-line "
        "justification.\n"
        "DO NOT compute any costs, rates, subtotals, taxes, or discounts — those are calculated "
        "in code. Only estimate hours and describe the work.\n"
        f"Available rate tiers: {list(rate_by_tier)}. Currency is {currency} (set in code)."
    )
    human = f"Gantt tasks:\n{gantt_ctx}{prior}"
    draft: BudgetDraft = await model.ainvoke([("system", system), ("human", human)])

    # Arithmetic in code — never trust the model for money (docs §5.2).
    priced = price_line_items([li.model_dump() for li in draft.line_items], rate_by_tier)

    return {
        **state,
        "current_artifact_type": "budget",
        "draft": {
            "currency": currency,
            "rate_by_tier": rate_by_tier,
            "lines": priced["lines"],
            "subtotal": priced["subtotal"],
            "model_id": model_id_for("budget"),
        },
    }
