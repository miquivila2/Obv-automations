"""Render an assembled budget document into a PDF, matching the structure of
Oblivion's real Axo Capital budget (the reference the user provided).

Replaces app/services/budget_docx.py's .docx output. Two reasons: (1) the
user asked for a PDF deliverable specifically, and (2) this machine has no
LibreOffice, so a .docx -> .pdf conversion step isn't available — rendering
straight to PDF with `reportlab` (already a project dependency) sidesteps
that entirely.

Takes an ASSEMBLED document dict (see app/services/budget_document.py for how
it's built from priced lines + discounts + IVA + market comparison +
milestones) and lays it out. Kept deliberately thin and presentational, same
principle as the old budget_docx.py: every number in the input is already
computed (budget_math.py); this module only draws it.
"""
from __future__ import annotations

from io import BytesIO


def render_budget_pdf(*, project_name: str, document: dict) -> bytes:
    """Build the PDF and return its bytes, ready to upload to Supabase Storage.

    `document` is the dict produced by app.services.budget_document.assemble()."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    currency = document["currency"]
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("BudgetH1", parent=styles["Heading1"], textColor=colors.HexColor("#0B5563"))
    h2 = ParagraphStyle("BudgetH2", parent=styles["Heading2"], textColor=colors.HexColor("#0B5563"))
    body = styles["BodyText"]
    small = ParagraphStyle("Small", parent=body, fontSize=9, textColor=colors.grey)
    cell = ParagraphStyle("Cell", parent=body, fontSize=9, leading=12)
    cell_bold = ParagraphStyle("CellBold", parent=cell, fontName="Helvetica-Bold")

    story: list = []

    # --- Header ---------------------------------------------------------
    story.append(Paragraph(f"{project_name}", h1))
    story.append(Paragraph("Development Engagement · Budget Proposal", body))
    story.append(
        Paragraph(
            f"Currency: {currency}, taxes included · Prepared by Oblivion · Confidential",
            small,
        )
    )
    story.append(Spacer(1, 0.2 * inch))

    # --- The short version ------------------------------------------------
    story.append(Paragraph("The short version", h2))
    total_hours = document["total_hours"]
    short_rows = [
        ["Total hours", f"{total_hours:.0f}"],
        [f"Subtotal after discounts ({currency})", f"{document['subtotal_after_discounts']:,.2f}"],
        [f"IVA ({document['iva_rate'] * 100:.0f}%)", f"{document['iva_amount']:,.2f}"],
        [f"TOTAL, all included ({currency})", f"{document['total_all_included']:,.2f}"],
    ]
    if document.get("contingency_pct") is not None:
        short_rows.insert(
            2, [f"Contingency ({document['contingency_pct']:.0f}%)", "manually applied — see below"]
        )
    short_table = Table(short_rows, colWidths=[3.5 * inch, 2.5 * inch])
    short_table.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor("#DBE1E2")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#EAF3F4")),
            ]
        )
    )
    story.append(short_table)
    story.append(Spacer(1, 0.25 * inch))

    # --- Month by month -----------------------------------------------------
    story.append(Paragraph("What you are paying, month by month", h2))
    for month in document["months"]:
        story.append(Paragraph(month["month"], ParagraphStyle("MonthHead", parent=body, fontName="Helvetica-Bold", fontSize=11, spaceBefore=8)))
        rows = [["What we build", "Hours", "Rate", "Cost"]]
        for li in month["lines"]:
            desc = li.get("description", "")
            just = li.get("justification") or li.get("details") or ""
            rows.append(
                [
                    Paragraph(f"<b>{desc}</b><br/><font size=8 color='grey'>{just}</font>", cell),
                    f"{float(li['hours']):.0f}",
                    f"{float(li['unit_rate']):,.2f}",
                    f"{float(li['amount']):,.2f}",
                ]
            )
        rows.append(["Subtotal", f"{month['hours']:.0f}", "", f"{month['subtotal']:,.2f}"])
        rows.append([f"Discount ({month['discount_pct']:.0f}%)", "", "", f"-{month['discount_amount']:,.2f}"])
        rows.append(["Total", "", "", f"{month['total']:,.2f}"])

        table = Table(rows, colWidths=[3.2 * inch, 0.7 * inch, 0.9 * inch, 0.9 * inch])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B5563")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#DBE1E2")),
                    ("BACKGROUND", (0, -3), (-1, -1), colors.HexColor("#F4F7F7")),
                    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 0.15 * inch))

    story.append(Spacer(1, 0.1 * inch))
    total_row = Table(
        [
            [f"Subtotal, all periods, after discounts ({currency})", f"{document['subtotal_after_discounts']:,.2f}"],
            [f"IVA ({document['iva_rate'] * 100:.0f}%)", f"{document['iva_amount']:,.2f}"],
            [f"TOTAL (all included, {currency})", f"{document['total_all_included']:,.2f}"],
        ],
        colWidths=[4 * inch, 2 * inch],
    )
    total_row.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#0B5563")),
                ("TEXTCOLOR", (0, -1), (-1, -1), colors.white),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(total_row)
    story.append(Spacer(1, 0.3 * inch))

    # --- Market comparison (USD only — see budget_market_comparison.py) -----
    market = document.get("market_comparison")
    if market:
        story.append(Paragraph("What the same work costs elsewhere", h2))
        rows = [["If you bought this elsewhere...", "Published price range", "This budget"]]
        for band in market:
            rows.append(
                [
                    Paragraph(f"<b>{band['label']}</b><br/><font size=8 color='grey'>{band['rate_range']}</font>", cell),
                    f"US${band['price_low']:,.0f} to US${band['price_high']:,.0f}",
                    f"{document['total_all_included']:,.2f} {currency}",
                ]
            )
        mtable = Table(rows, colWidths=[3.2 * inch, 1.7 * inch, 1.8 * inch])
        mtable.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B5563")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#DBE1E2")),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(mtable)
        story.append(Spacer(1, 0.3 * inch))

    # --- Payment terms / milestones -----------------------------------------
    milestones = document.get("milestones") or []
    if milestones:
        story.append(Paragraph("Payment terms", h2))
        story.append(
            Paragraph(
                "Percentages below are 0% until filled in manually — this system never invents a payment split.",
                small,
            )
        )
        story.append(Spacer(1, 0.05 * inch))
        rows = [["When", "What has been delivered", "Part", "Amount"]]
        for m in milestones:
            rows.append(
                [
                    m.get("when", ""),
                    m.get("description", ""),
                    f"{m.get('part_pct', 0):.0f}%",
                    f"{m.get('amount', 0):,.2f}",
                ]
            )
        ptable = Table(rows, colWidths=[1.3 * inch, 3.2 * inch, 0.7 * inch, 1.3 * inch])
        ptable.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B5563")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#DBE1E2")),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(ptable)

    buffer = BytesIO()
    SimpleDocTemplate(
        buffer, pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
    ).build(story)
    return buffer.getvalue()
