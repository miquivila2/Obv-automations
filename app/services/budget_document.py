"""Assembles the full Axo Capital-format budget document (line items + monthly
discounts + IVA + market comparison + milestones) into one dict — the single
shape that both the JSON export and the PDF renderer (budget_pdf.py) consume.

Pure and dependency-free (no I/O, no DB) except for the market-comparison call,
which is itself pure — so this whole module is unit-testable without Supabase
or a model. All the actual arithmetic lives in budget_math.py; this module
only assembles its outputs plus the human-entered fields (discounts,
contingency, milestones) into one document.
"""
from __future__ import annotations


def assemble_budget_document(
    *,
    project_name: str,
    currency: str,
    priced_lines: list[dict],
    iva_rate: float,
    discount_pct_by_month: dict[str, float],
    contingency_pct: float | None,
    milestones: list[dict],
) -> dict:
    """Build the full document dict. `priced_lines` are already-priced lines
    from budget_math.price_line_items (each has hours/unit_rate/amount/month).
    `discount_pct_by_month`, `contingency_pct`, and each milestone's
    `part_pct` are HUMAN INPUT — this function only assembles them, never
    invents a value (see budget_math.py's module docstring for why)."""
    from app.services.budget_market_comparison import compute_market_comparison
    from app.services.budget_math import (
        apply_month_discounts,
        compute_iva_and_total,
        group_lines_by_month,
    )

    total_hours = sum(float(li["hours"]) for li in priced_lines)
    by_month = group_lines_by_month(priced_lines)
    discounted = apply_month_discounts(by_month, discount_pct_by_month)
    iva = compute_iva_and_total(discounted["subtotal_after_discounts"], iva_rate)

    return {
        "project_name": project_name,
        "currency": currency,
        "total_hours": total_hours,
        "months": discounted["months"],
        "subtotal_after_discounts": discounted["subtotal_after_discounts"],
        "iva_rate": iva_rate,
        "iva_amount": iva["iva_amount"],
        "total_all_included": iva["total_all_included"],
        "contingency_pct": contingency_pct,
        "market_comparison": compute_market_comparison(total_hours, currency),
        "milestones": milestones,
    }


def default_milestones(months: list[str]) -> list[dict]:
    """One placeholder payment row per month present in the budget, part_pct
    always 0 — "esto lo hacemos nosotros / debe estar al 0%". A human fills
    in the real split later; this just gives them the rows to fill in."""
    return [
        {"when": f"End of {month}", "description": f"{month} deliverables", "part_pct": 0, "amount": 0}
        for month in months
    ]
