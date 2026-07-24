"""The budget arithmetic is the money math — it must be exact and is fully
testable in isolation (no model, no DB). See docs §5.2: costs are computed here,
never by the LLM.
"""
import pytest

from app.services.budget_math import price_line_items
from app.services.rates import resolve_currency


def test_amount_is_hours_times_rate():
    out = price_line_items([{"hours": 40, "tier": "standard"}], {"standard": 60.0})
    assert out["lines"][0]["unit_rate"] == 60.0
    assert out["lines"][0]["amount"] == 2400.0
    assert out["subtotal"] == 2400.0


def test_subtotal_sums_all_lines_no_iva_no_discount():
    out = price_line_items(
        [{"hours": 40, "tier": "standard"}, {"hours": 16, "tier": "standard"}],
        {"standard": 60.0},
    )
    # 40*60 + 16*60 = 3360 — no IVA, no discount, no contingency added.
    assert out["subtotal"] == 3360.0


def test_rounding_is_half_up_to_cents():
    out = price_line_items([{"hours": 1.005, "tier": "standard"}], {"standard": 100.0})
    # 1.005 * 100 = 100.50
    assert out["lines"][0]["amount"] == 100.50


def test_position_is_assigned_in_order():
    out = price_line_items(
        [{"hours": 1, "tier": "standard"}, {"hours": 2, "tier": "standard"}], {"standard": 10.0}
    )
    assert [li["position"] for li in out["lines"]] == [0, 1]


def test_missing_tier_rate_fails_loud():
    with pytest.raises(KeyError):
        price_line_items([{"hours": 10, "tier": "senior"}], {"standard": 60.0})


def test_currency_follows_language_with_project_override():
    assert resolve_currency("en") == "USD"
    assert resolve_currency("es") == "MXN"
    assert resolve_currency("es", project_preferred="USD") == "USD"
