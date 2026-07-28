"""The budget arithmetic is the money math — it must be exact and is fully
testable in isolation (no model, no DB). See docs §5.2: costs are computed here,
never by the LLM.
"""
import pytest

from app.services.budget_math import (
    apply_month_discounts,
    compute_iva_and_total,
    group_lines_by_month,
    price_line_items,
)
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


# --- Monthly discounts + IVA (Axo Capital format, docs §5.2/§9.12 reopened) ---

def test_group_by_month_preserves_first_seen_order():
    lines = [{"month": "July", "amount": 1}, {"month": "June", "amount": 2}, {"month": "July", "amount": 3}]
    grouped = group_lines_by_month(lines)
    assert list(grouped.keys()) == ["July", "June"]
    assert len(grouped["July"]) == 2


def test_group_by_month_falls_back_to_unscheduled():
    grouped = group_lines_by_month([{"amount": 1}])
    assert list(grouped.keys()) == ["Unscheduled"]


def test_missing_month_in_discount_map_defaults_to_zero_never_invented():
    # "esto lo hacemos nosotros" — a month absent from discount_pct_by_month
    # must be treated as a plain 0% discount, not skipped or guessed.
    lines_by_month = {"June": [{"amount": 4800.0, "hours": 80}]}
    out = apply_month_discounts(lines_by_month, discount_pct_by_month={})
    month = out["months"][0]
    assert month["discount_pct"] == 0.0
    assert month["discount_amount"] == 0.0
    assert month["total"] == 4800.0


def test_month_discount_matches_the_axo_reference_exactly():
    # June: 80h at $60/h = $4,800, 100% discount -> $0 (the real Axo document).
    lines_by_month = {"June": [{"amount": 4800.0, "hours": 80}]}
    out = apply_month_discounts(lines_by_month, discount_pct_by_month={"June": 100})
    month = out["months"][0]
    assert month["subtotal"] == 4800.0
    assert month["discount_amount"] == 4800.0
    assert month["total"] == 0.0
    assert out["subtotal_after_discounts"] == 0.0


def test_partial_discount_matches_the_axo_reference_july():
    # July in the real document: $7,140 subtotal, 40% discount -> $4,284 total.
    lines_by_month = {"July": [{"amount": 7140.0, "hours": 119}]}
    out = apply_month_discounts(lines_by_month, discount_pct_by_month={"July": 40})
    month = out["months"][0]
    assert month["discount_amount"] == 2856.0
    assert month["total"] == 4284.0


def test_subtotal_after_discounts_sums_across_months():
    lines_by_month = {
        "June": [{"amount": 4800.0, "hours": 80}],
        "July": [{"amount": 7140.0, "hours": 119}],
    }
    out = apply_month_discounts(lines_by_month, {"June": 100, "July": 40})
    # 0 (June) + 4284 (July) — matches the real document's running total shape.
    assert out["subtotal_after_discounts"] == 4284.0


def test_iva_matches_the_axo_reference_exactly():
    # The real document: $10,080 subtotal, 16% IVA -> $1,612.80, total $11,692.80.
    out = compute_iva_and_total(10080.0, iva_rate=0.16)
    assert out["iva_amount"] == 1612.80
    assert out["total_all_included"] == 11692.80


def test_zero_iva_rate_is_a_pass_through():
    out = compute_iva_and_total(1000.0, iva_rate=0.0)
    assert out["iva_amount"] == 0.0
    assert out["total_all_included"] == 1000.0
