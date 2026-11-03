"""
receipts.py - tidy PDFs for payments: a pro-forma invoice (before paying, with the bank details) and a receipt
(after the owner has confirmed a transfer). Plain reportlab, naira sign included.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import secrets
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import db
import report_pdf

SUPPORT = os.getenv("SUPPORT_EMAIL", "reconciliationsolutionsupport@gmail.com")


def bank_accounts() -> list[dict]:
    """The temporary receiving accounts (details can be changed with environment variables)."""
    return [
        {"name": os.getenv("MONIEPOINT_BANK_NAME") or "Moniepoint", "account_number": os.getenv("MONIEPOINT_ACCOUNT") or "8100481004",
         "account_name": os.getenv("MONIEPOINT_OWNER") or "Daniel Dominion Folorunso"},
        {"name": os.getenv("OPAY_BANK_NAME") or "OPay", "account_number": os.getenv("OPAY_ACCOUNT") or "8083878507",
         "account_name": os.getenv("OPAY_OWNER") or "Mary Olubunmi Folorunso"},
    ]


def _naira(v) -> str:
    return f"₦{int(v):,}"


def _day(ts: str | None) -> str:
    return (ts or "")[:10] or "-"


def _doc(title: str, number: str, meta: list[tuple[str, str]], rows: list[list[str]], total_label: str, total: str,
         paragraphs: list[str], branding: dict | None, banks: list[dict] | None = None) -> bytes:
    reg, bold = report_pdf._fonts()
    brand = colors.HexColor((branding or {}).get("color") or "#1769ff")
    h = ParagraphStyle("h", fontName=bold, fontSize=20, textColor=colors.white, leading=24)
    n = ParagraphStyle("n", fontName=reg, fontSize=10, leading=14)
    b = ParagraphStyle("b", fontName=bold, fontSize=10, leading=14)
    small = ParagraphStyle("s", fontName=reg, fontSize=8.5, leading=12, textColor=colors.HexColor("#55607a"))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm, title=title)
    band = Table([[Paragraph(escape(title), h), Paragraph(f"<para align='right'><font color='white'>Report Analyzer<br/>{escape(number)}</font></para>", n)]], colWidths=[10 * cm, 7 * cm])
    band.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), brand), ("PADDING", (0, 0), (-1, -1), 12), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story = [band, Spacer(1, 14)]
    mt = Table([[Paragraph(escape(k), b), Paragraph(escape(v), n)] for k, v in meta], colWidths=[4 * cm, 13 * cm])
    mt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [mt, Spacer(1, 14)]
    t = Table([[Paragraph(escape(c), ParagraphStyle("th", parent=b, textColor=colors.white)) for c in rows[0]]] +
              [[Paragraph(escape(c), n) for c in r] for r in rows[1:]] +
              [[Paragraph(escape(total_label), b), "", Paragraph(escape(total), b)]], colWidths=[8 * cm, 5 * cm, 4 * cm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#243b75")), ("GRID", (0, 0), (-1, -2), 0.4, colors.HexColor("#c9d3ea")),
                           ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#243b75")), ("PADDING", (0, 0), (-1, -1), 7), ("ALIGN", (2, 0), (2, -1), "RIGHT")]))
    story += [t, Spacer(1, 14)]
    if banks:
        story.append(Paragraph("How to pay", b))
        bt = Table([["Bank", "Account number", "Account name"]] + [[x["name"], x["account_number"], x["account_name"]] for x in banks], colWidths=[4 * cm, 5 * cm, 8 * cm])
        bt.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), reg), ("FONTNAME", (0, 0), (-1, 0), bold), ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9d3ea")), ("PADDING", (0, 0), (-1, -1), 6)]))
        story += [Spacer(1, 4), bt, Spacer(1, 10)]
    for p in paragraphs:
        story += [Paragraph(escape(p), small), Spacer(1, 4)]
    story.append(Paragraph(f"Questions? {escape(SUPPORT)}", small))
    doc.build(story)
    return buf.getvalue()


def proforma_pdf(company_name: str, plan_key: str, branding: dict | None = None, discount_ngn: int = 0) -> bytes:
    plan = db.PLANS[plan_key]
    today = dt.datetime.now(dt.timezone.utc)
    number = f"PF-{today:%Y%m%d}-{secrets.token_hex(2).upper()}"
    price = int(plan["price_ngn"])
    amount = max(0, price - max(0, int(discount_ngn)))
    term = db.duration_label(plan_key)
    rows = [["Plan", "Includes", "Amount"],
            [plan["name"], f"{term}; {plan['monthly_limit']:,} checks a month; {plan['team_limit']} team member(s)", _naira(price)]]
    if discount_ngn:
        rows.append(["Discount", "Coupon", "-" + _naira(discount_ngn)])
    return _doc("Pro-forma invoice", number,
                [("Billed to", company_name), ("Date", f"{today:%Y-%m-%d}"), ("Valid until", f"{(today + dt.timedelta(days=7)):%Y-%m-%d}")],
                rows, "Amount to pay", _naira(amount),
                ["Pay by bank transfer to one of the accounts above, then enter your transfer reference on the Plan & Billing page and attach a screenshot of the transfer.",
                 "Your plan starts after our team confirms the payment. This is a request for payment, not a receipt."], branding, bank_accounts())


def receipt_pdf(invoice: dict, company_name: str, pay: dict | None, sub: dict | None, branding: dict | None = None) -> bytes:
    plan_key = invoice["plan"]
    plan = db.PLANS.get(plan_key, {"name": plan_key.title(), "monthly_limit": 0, "team_limit": 0})
    meta = [("Received from", company_name), ("Receipt date", _day(invoice.get("paid_at") or invoice.get("issued_at"))), ("Status", "PAID")]
    if pay:
        meta.append(("Transfer reference", str(pay.get("transfer_reference") or "-")))
    if sub:
        meta.append(("Plan period", f"{_day(sub.get('starts_at'))} to {_day(sub.get('expires_at'))}"))
    rows = [["Plan", "Includes", "Amount"],
            [plan["name"], f"{plan.get('monthly_limit', 0):,} checks a month; {plan.get('team_limit', 0)} team member(s)", _naira(invoice["amount_ngn"])]]
    return _doc("Receipt", invoice["invoice_number"], meta, rows, "Total paid", _naira(invoice["amount_ngn"]),
                ["Payment was confirmed by a Report Analyzer administrator after a bank transfer.",
                 "This is a receipt for the subscription named above. It is not a tax invoice unless separately validated for local tax requirements."], branding)
