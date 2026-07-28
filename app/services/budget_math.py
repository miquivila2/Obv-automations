"""The budget arithmetic — computed in code, NEVER by the LLM (docs §5.2).

The Budget agent's model produces the *structure* of each line (category, hours,
tier, justification). This module turns that into money: unit_rate lookup,
amount = hours x unit_rate, monthly subtotals, per-month discounts, and IVA.

REVISED (docs §5.2, §9.12 reopened): the Axo Capital reference format the
agent now reproduces DOES show IVA and per-month discounts prominently — the
earlier "no IVA, no discounts" lock was written before that format was
compared against a real example. What's still true, and non-negotiable:
  * Every number here comes from Python, not the model. An LLM getting a
    multiplication wrong in a client-facing document is a real, avoidable risk.
  * discount_pct_by_month is HUMAN INPUT, always defaulting to 0 — this module
    never invents a discount. Same for contingency_pct and milestone part_pct,
    handled by the callers that build the full document (see
    app/services/budget_persist.py) — this module only ever multiplies and
    adds the numbers it's given.

Pure and dependency-free, so it's exhaustively unit-testable.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def _money(value: Decimal) -> float:
    """Round to 2 decimals, half-up (accounting rounding), as a float for JSON/DB."""
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def price_line_items(line_items: list[dict], rate_by_tier: dict[str, float]) -> dict:
    """Price each line item and total them.

    Each `line_items` entry must have `hours` (number) and may have `tier`
    (defaults to 'standard'). Returns:

        {"lines": [ {<original fields>, unit_rate, amount, position}, ... ],
         "subtotal": float}

    Raises KeyError if a line's tier has no configured rate — fail loud rather
    than silently pricing work at zero.
    """
    priced: list[dict] = []
    subtotal = Decimal("0")

    for position, item in enumerate(line_items):
        tier = item.get("tier", "standard")
        if tier not in rate_by_tier:
            raise KeyError(f"No rate configured for tier {tier!r} (available: {list(rate_by_tier)})")

        rate = Decimal(str(rate_by_tier[tier]))
        hours = Decimal(str(item["hours"]))
        amount = rate * hours
        subtotal += amount

        priced.append(
            {
                **item,
                "tier": tier,
                "unit_rate": _money(rate),
                "amount": _money(amount),
                "position": position,
            }
        )

    return {"lines": priced, "subtotal": _money(subtotal)}


def group_lines_by_month(lines: list[dict]) -> dict[str, list[dict]]:
    """Group already-priced lines by their `month` label, preserving the
    order months first appear in (Python dicts preserve insertion order;
    `lines` already arrives in position order from price_line_items).
    A line with no month falls under 'Unscheduled' rather than being dropped."""
    groups: dict[str, list[dict]] = {}
    for line in lines:
        month = line.get("month") or "Unscheduled"
        groups.setdefault(month, []).append(line)
    return groups


def apply_month_discounts(
    lines_by_month: dict[str, list[dict]], discount_pct_by_month: dict[str, float]
) -> dict:
    """Per-month subtotal, discount amount, and total — the Axo format's
    "What you are paying, month by month" section (§3).

    `discount_pct_by_month` is HUMAN INPUT (a percentage, 0-100). A month
    absent from it is treated as 0% — this function never invents a discount;
    it only ever applies the percentage it's handed.
    """
    months: list[dict] = []
    subtotal_after_discounts = Decimal("0")

    for month, month_lines in lines_by_month.items():
        month_subtotal = sum((Decimal(str(li["amount"])) for li in month_lines), Decimal("0"))
        pct = Decimal(str(discount_pct_by_month.get(month, 0) or 0))
        discount_amount = month_subtotal * pct / Decimal("100")
        month_total = month_subtotal - discount_amount
        subtotal_after_discounts += month_total

        months.append(
            {
                "month": month,
                "lines": month_lines,
                "hours": sum(float(li["hours"]) for li in month_lines),
                "subtotal": _money(month_subtotal),
                "discount_pct": float(pct),
                "discount_amount": _money(discount_amount),
                "total": _money(month_total),
            }
        )

    return {"months": months, "subtotal_after_discounts": _money(subtotal_after_discounts)}


def compute_iva_and_total(subtotal_after_discounts: float, iva_rate: float) -> dict:
    """IVA amount and the final all-included total. `iva_rate` is a fraction
    (0.16 for 16%), matching what agent.budget_documents.iva_rate stores."""
    subtotal = Decimal(str(subtotal_after_discounts))
    rate = Decimal(str(iva_rate))
    iva_amount = subtotal * rate
    return {
        "iva_amount": _money(iva_amount),
        "total_all_included": _money(subtotal + iva_amount),
    }
