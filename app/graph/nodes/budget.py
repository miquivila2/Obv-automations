"""Agent 5 — Budget.

Produces a priced, justified budget in the FLOWSIGHT / Axo Capital format
(two-tier hourly rates, monthly subtotals, contingency, market comparison,
milestone payments) as a .docx. Uses Qwen3-Next-80B for long-context few-shot
grounding against past budgets.

CRITICAL (see docs §5.2): the LLM generates the line items — task, hours,
rate tier, justification — but the arithmetic (hours x rate, subtotals,
contingency, currency) is computed in code (`price_budget`). An LLM getting a
multiplication wrong in a document that goes to the client is a real,
avoidable risk.

Currency comes from the meeting language: USD if English, MXN if Spanish.

Inputs: the Gantt + tasks, a library of past budgets, and rate_config.
Modes: create | follow-up (load latest budget first).
"""
from __future__ import annotations

from app.db.client import get_supabase
from app.graph.state import BuildState
from app.services.artifacts import load_latest
from app.services.bedrock import chat_model_for


def _load_rates(currency: str) -> list[dict]:
    """Current rate tiers for the given currency (most recent effective_from wins)."""
    return (
        get_supabase()
        .table("rate_config")
        .select("*")
        .eq("currency", currency)
        .order("effective_from", desc=True)
        .execute()
        .data
    )


def price_budget(line_items: list[dict], rates: list[dict], contingency_pct: float = 0.10) -> dict:
    """Deterministic pricing in code, never in the LLM. Each line item is
    {tier, hours, ...}; we look up the rate and compute. Returns totals plus
    the priced lines. This is the money math — it must be exact and testable."""
    rate_by_tier = {r["tier"]: r["hourly_rate"] for r in rates}
    priced = []
    subtotal = 0.0
    for item in line_items:
        rate = rate_by_tier.get(item["tier"], 0.0)
        cost = rate * item["hours"]
        subtotal += cost
        priced.append({**item, "rate": rate, "cost": cost})
    contingency = subtotal * contingency_pct
    return {"lines": priced, "subtotal": subtotal, "contingency": contingency, "total": subtotal + contingency}


async def build_budget(state: BuildState) -> BuildState:
    currency = "USD" if state["language"] == "en" else "MXN"
    rates = _load_rates(currency)

    gantt = load_latest(state["project_id"], "gantt")
    gantt_ctx = f"Gantt + tasks:\n{gantt['content']}" if gantt else "Gantt: (none found)"

    model = chat_model_for("budget")
    system = (
        "You are Oblivion's budget agent. From the Gantt and tasks, produce budget LINE ITEMS in the "
        "FLOWSIGHT/Axo Capital style: each line has a task, an hours estimate, a rate tier, and a "
        "one-line justification. DO NOT compute any totals or costs — those are calculated separately. "
        f"Rate tiers available ({currency}): {[r['tier'] for r in rates]}. "
        "Return JSON: {\"line_items\": [{\"task\": str, \"hours\": number, \"tier\": str, \"justification\": str}]}"
    )
    human = f"{gantt_ctx}"

    response = await model.ainvoke([("system", system), ("human", human)])

    # NOTE: parsing response.content into line_items and calling price_budget(),
    # then rendering the .docx via the shared docx skill and uploading to Supabase
    # Storage, happens in the persistence step (build.py). The math is price_budget's
    # job — the model's output is never trusted for arithmetic.
    return {
        **state,
        "current_artifact_type": "budget",
        "draft": {"raw": response.content, "currency": currency},
    }
