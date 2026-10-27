"""
report_pdf.py - a clean, branded PDF of a result that a company can send on to its own clients.
Branding (display name, colour, logo) is set per company by an admin in the dashboard.
"""
import datetime as dt
import io
import numbers
import os
from xml.sax.saxutils import escape

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

MAX_ROWS, MAX_COLS = 40, 8
_FONTS = None


def _fonts() -> tuple[str, str]:
    """(regular, bold) font names. The built-in PDF fonts cannot draw the naira sign, so use DejaVu Sans
    (shipped inside matplotlib, which is already installed) and fall back to Helvetica if it is missing."""
    global _FONTS
    if _FONTS is None:
        try:
            import matplotlib
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont

            d = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")
            pdfmetrics.registerFont(TTFont("DejaVuSans", os.path.join(d, "DejaVuSans.ttf")))
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", os.path.join(d, "DejaVuSans-Bold.ttf")))
            _FONTS = ("DejaVuSans", "DejaVuSans-Bold")
        except Exception:  # noqa: BLE001
            _FONTS = ("Helvetica", "Helvetica-Bold")
    return _FONTS


def _cell(v) -> str:
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, numbers.Integral):
        return f"{int(v):,}" if abs(int(v)) >= 10000 else str(int(v))
    if isinstance(v, numbers.Real):
        return f"{float(v):,.2f}"
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)


def build_pdf(company_name: str, title: str, question: str, insight: str | None, table: pd.DataFrame,
              chart_png: bytes | None = None, branding: dict | None = None, total_rows: int | None = None) -> bytes:
    branding = branding or {}
    try:
        brand = colors.HexColor(branding.get("color") or "#2b59ff")
    except ValueError:
        brand = colors.HexColor("#2b59ff")
    name = branding.get("display_name") or company_name
    today = dt.date.today().strftime("%d %b %Y")

    styles = getSampleStyleSheet()
    f_reg, f_bold = _fonts()
    s_name = ParagraphStyle("name", parent=styles["Normal"], fontName=f_bold, textColor=brand, fontSize=11, spaceAfter=2)
    s_title = ParagraphStyle("title", parent=styles["Title"], fontName=f_bold, alignment=0, fontSize=20, leading=24, textColor=colors.HexColor("#1c2230"))
    s_sub = ParagraphStyle("sub", parent=styles["Normal"], fontName=f_reg, textColor=colors.HexColor("#6b7385"), fontSize=9)
    s_body = ParagraphStyle("body", parent=styles["Normal"], fontName=f_reg, fontSize=10, leading=14)

    story = []
    logo = branding.get("logo_path")
    logo_bytes = None
    if logo:
        import db  # local import: logos may live in the database (PostgreSQL mode)
        logo_bytes = db.read_file(logo)
    if logo_bytes:
        img = Image(io.BytesIO(logo_bytes))
        ratio = img.imageWidth / img.imageHeight
        img.drawHeight, img.drawWidth, img.hAlign = 1.3 * cm, 1.3 * cm * ratio, "LEFT"
        story += [img, Spacer(1, 4)]
    story += [Paragraph(escape(name), s_name), Paragraph(escape(title), s_title),
              Paragraph(escape(f"{today}  |  {question}"), s_sub), Spacer(1, 10)]

    if insight:
        for line in insight.splitlines():
            line = line.strip()
            if line:
                story.append(Paragraph(escape("• " + line[2:] if line.startswith("- ") else line), s_body))
        story.append(Spacer(1, 10))

    if chart_png:
        story += [Image(io.BytesIO(chart_png), width=16 * cm, height=8.9 * cm), Spacer(1, 10)]

    if table is not None and len(table.columns):
        cols = list(table.columns)[:MAX_COLS]
        width = (A4[0] - 3.2 * cm) / len(cols)
        limit = max(int(width / 4.2), 6)  # characters that fit at 7.5pt
        data = [[str(c)[:limit] for c in cols]]
        for _, row in table.head(MAX_ROWS).iterrows():
            data.append([_cell(row[c])[:limit] for c in cols])
        t = Table(data, colWidths=[width] * len(cols), repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), brand), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, -1), f_reg), ("FONTNAME", (0, 0), (-1, 0), f_bold), ("FONTSIZE", (0, 0), (-1, -1), 7.5), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5d9e4")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f6fa")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(t)
        total = total_rows if total_rows is not None else len(table)
        if total > MAX_ROWS or len(table.columns) > MAX_COLS:
            story.append(Paragraph(escape(f"Showing the first {min(MAX_ROWS, total)} of {total:,} rows - full detail is in the Excel file."), s_sub))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(f_reg, 8)
        canvas.setFillColor(colors.HexColor("#6b7385"))
        canvas.drawString(1.6 * cm, 1.0 * cm, f"{name}  |  {today}  |  Figures computed from the uploaded report")
        canvas.drawRightString(A4[0] - 1.6 * cm, 1.0 * cm, f"Page {doc.page}")
        canvas.restoreState()

    out = io.BytesIO()
    SimpleDocTemplate(out, pagesize=A4, leftMargin=1.6 * cm, rightMargin=1.6 * cm, topMargin=1.6 * cm,
                      bottomMargin=1.8 * cm, title=title).build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()
