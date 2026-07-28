"""render_budget_pdf produces a real PDF — verified via pdfplumber that the
structure (project name, months, discount, IVA, total) reflects the assembled
document. No Supabase involved; this is pure rendering, same principle as the
old test_budget_docx.py (now replaced — see budget_pdf.py's module docstring
for why PDF instead of .docx).
"""
from io import BytesIO

import pdfplumber

from app.services.budget_document import assemble_budget_document
from app.services.budget_pdf import render_budget_pdf

_LINES = [
    {"category": "Audit", "description": "Codebase audit", "justification": "why", "hours": 40, "unit_rate": 60.0, "amount": 2400.0, "month": "June"},
    {"category": "Dev", "description": "Fix #54", "justification": "money bug", "hours": 5, "unit_rate": 60.0, "amount": 300.0, "month": "July"},
]


def _document(**overrides):
    base = dict(
        project_name="Axo Capital",
        currency="USD",
        priced_lines=_LINES,
        iva_rate=0.16,
        discount_pct_by_month={"June": 100, "July": 0},
        contingency_pct=None,
        milestones=[],
    )
    base.update(overrides)
    return assemble_budget_document(**base)


def _pdf_text(data: bytes) -> str:
    with pdfplumber.open(BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def test_returns_nonempty_valid_pdf_bytes():
    data = render_budget_pdf(project_name="Axo Capital", document=_document())
    assert isinstance(data, bytes) and len(data) > 0
    assert data[:4] == b"%PDF"  # PDF magic bytes — a real, structurally valid PDF


def test_project_name_and_currency_appear():
    text = _pdf_text(render_budget_pdf(project_name="Axo Capital", document=_document()))
    assert "Axo Capital" in text
    assert "USD" in text


def test_months_and_discounts_are_rendered():
    text = _pdf_text(render_budget_pdf(project_name="Axo Capital", document=_document()))
    assert "June" in text and "July" in text
    assert "100%" in text  # June's discount


def test_iva_and_total_match_the_computed_document():
    doc = _document()
    text = _pdf_text(render_budget_pdf(project_name="Axo Capital", document=doc))
    assert f"{doc['iva_amount']:,.2f}" in text
    assert f"{doc['total_all_included']:,.2f}" in text


def test_market_comparison_renders_for_usd():
    text = _pdf_text(render_budget_pdf(project_name="Axo Capital", document=_document(currency="USD")))
    assert "same work costs elsewhere" in text


def test_market_comparison_absent_for_mxn():
    doc = _document(currency="MXN")
    assert doc["market_comparison"] is None
    text = _pdf_text(render_budget_pdf(project_name="Axo Capital", document=doc))
    assert "same work costs elsewhere" not in text


def test_milestones_render_as_zero_percent_placeholders():
    milestones = [{"when": "End of July", "description": "Build month 1", "part_pct": 0, "amount": 0}]
    text = _pdf_text(render_budget_pdf(project_name="Axo Capital", document=_document(milestones=milestones)))
    assert "Payment terms" in text
    assert "End of July" in text
    assert "never invents a payment split" in text.replace("\n", " ")
