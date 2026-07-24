"""The budget arithmetic — computed in code, NEVER by the LLM (docs §5.2).

The Budget agent's model produces the *structure* of each line (category, hours,
tier, justification). This module turns that into money: unit_rate lookup, and
amount = hours x unit_rate, plus the subtotal.

Per the locked decisions (docs/ARCHITECTURE.md):
  * NO automatic IVA and NO automatic discounts — a human adds those in the CRM.
    This module returns line amounts and a subtotal, nothing else.
  * An LLM getting a multiplication wrong in a client-facing document is a real,
    avoidable risk, so every number here comes from Python, not the model.

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
