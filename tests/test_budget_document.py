"""assemble_budget_document ties budget_math's arithmetic together with the
human-entered fields (discounts, contingency, milestones) into the one shape
both the JSON export and the PDF renderer consume. Pure, no I/O.
"""
from app.services.budget_document import assemble_budget_document, default_milestones

_LINES = [
    {"category": "Audit", "description": "Codebase audit", "hours": 40, "unit_rate": 60.0, "amount": 2400.0, "month": "June"},
    {"category": "Dev", "description": "Fix #54", "hours": 5, "unit_rate": 60.0, "amount": 300.0, "month": "July"},
]


def test_assembles_months_iva_and_totals():
    doc = assemble_budget_document(
        project_name="Axo Capital",
        currency="USD",
        priced_lines=_LINES,
        iva_rate=0.16,
        discount_pct_by_month={"June": 100, "July": 0},
        contingency_pct=None,
        milestones=[],
    )
    assert doc["total_hours"] == 45
    assert [m["month"] for m in doc["months"]] == ["June", "July"]
    assert doc["months"][0]["total"] == 0.0  # June, 100% discount
    assert doc["months"][1]["total"] == 300.0  # July, no discount
    assert doc["subtotal_after_discounts"] == 300.0
    assert doc["iva_amount"] == 48.0  # 300 * 0.16
    assert doc["total_all_included"] == 348.0


def test_missing_months_in_discount_map_default_to_zero():
    doc = assemble_budget_document(
        project_name="X", currency="USD", priced_lines=_LINES, iva_rate=0.16,
        discount_pct_by_month={}, contingency_pct=None, milestones=[],
    )
    assert all(m["discount_pct"] == 0.0 for m in doc["months"])


def test_contingency_is_passed_through_never_computed():
    doc = assemble_budget_document(
        project_name="X", currency="USD", priced_lines=_LINES, iva_rate=0.16,
        discount_pct_by_month={}, contingency_pct=None, milestones=[],
    )
    assert doc["contingency_pct"] is None

    doc2 = assemble_budget_document(
        project_name="X", currency="USD", priced_lines=_LINES, iva_rate=0.16,
        discount_pct_by_month={}, contingency_pct=12.5, milestones=[],
    )
    assert doc2["contingency_pct"] == 12.5  # exactly what was given, untouched


def test_market_comparison_present_for_usd_absent_for_mxn():
    usd_doc = assemble_budget_document(
        project_name="X", currency="USD", priced_lines=_LINES, iva_rate=0.16,
        discount_pct_by_month={}, contingency_pct=None, milestones=[],
    )
    assert usd_doc["market_comparison"] is not None

    mxn_doc = assemble_budget_document(
        project_name="X", currency="MXN", priced_lines=_LINES, iva_rate=0.16,
        discount_pct_by_month={}, contingency_pct=None, milestones=[],
    )
    assert mxn_doc["market_comparison"] is None


def test_default_milestones_are_zero_percent_placeholders():
    milestones = default_milestones(["June", "July"])
    assert len(milestones) == 2
    assert all(m["part_pct"] == 0 for m in milestones)
    assert milestones[0]["when"] == "End of June"
