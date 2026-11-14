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
              chart_png: bytes | None = None, branding: dict | None = None, total_rows: int | None = None,
              extra_tables: list | None = None) -> bytes:
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
    # Faint copy of the logo, drawn behind the content of every page (the logo's own alpha is lowered so text stays readable).
    wm = None
    if logo_bytes:
        try:
            from PIL import Image as PILImage
            from reportlab.lib.utils import ImageReader
            im = PILImage.open(io.BytesIO(logo_bytes)).convert("RGBA")
            im.putalpha(im.getchannel("A").point(lambda a: int(a * 0.10)))
            buf = io.BytesIO(); im.save(buf, "PNG"); buf.seek(0)
            wm = (ImageReader(buf), im.width / max(im.height, 1))
        except Exception:  # noqa: BLE001  (a bad logo must never stop the PDF)
            wm = None

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
        for _, row in table.iterrows():  # every row: nothing is left out of the PDF
            data.append([_cell(row[c])[:limit] for c in cols])
        t = Table(data, colWidths=[width] * len(cols), repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), brand), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, -1), f_reg), ("FONTNAME", (0, 0), (-1, 0), f_bold), ("FONTSIZE", (0, 0), (-1, -1), 7.5), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5d9e4")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [None if wm else colors.white, colors.Color(0.96, 0.965, 0.98, alpha=0.7 if wm else 1)]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(t)
        total = total_rows if total_rows is not None else len(table)
        if len(table.columns) > MAX_COLS:
            story.append(Paragraph(escape("Columns not shown in this table (they are in the Excel file): " + ", ".join(str(c) for c in list(table.columns)[MAX_COLS:])), s_sub))

    # extra detail tables (e.g. the transactions of a bank statement), with wrapping text so nothing is cut off
    _w = {"date": 1.1, "reference": 1.4, "narration": 2.3, "description": 2.3, "recommended resolution": 3.4, "status": 1.3, "note": 1.6, "item": 3.0, "value": 1.3, "category": 1.8, "methodology and assumptions": 8.0, "type": 0.8, "amount": 1.2, "balance": 1.2, "flag": 2.4, "account": 1.2}
    s_cell = ParagraphStyle("cell", parent=styles["Normal"], fontName=f_reg, fontSize=7, leading=8.5)
    for ttl, tdf, cap in (extra_tables or []):
        if tdf is None or not len(tdf):
            continue
        cols = list(tdf.columns)[:12]
        weights = [_w.get(str(c).lower(), 1.0) for c in cols]
        avail = A4[0] - 3.2 * cm
        widths = [avail * w / sum(weights) for w in weights]
        story += [Spacer(1, 12), Paragraph(escape(ttl), s_name)]
        data = [[Paragraph(escape(str(c)), ParagraphStyle("h", parent=s_cell, fontName=f_bold, textColor=colors.white)) for c in cols]]
        for _, row in tdf.iterrows():  # all rows, however many pages that takes
            data.append([Paragraph(escape(_cell(row[c])), s_cell) for c in cols])
        et = Table(data, colWidths=widths, repeatRows=1)
        et.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), brand), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5d9e4")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [None if wm else colors.white, colors.Color(0.96, 0.965, 0.98, alpha=0.7 if wm else 1)]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(et)
        story.append(Paragraph(escape(f"{len(tdf):,} row(s) listed."), s_sub))
        if len(tdf.columns) > 12:
            story.append(Paragraph(escape("Columns not shown here (they are in the Excel file): " + ", ".join(str(c) for c in list(tdf.columns)[12:])), s_sub))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(brand)
        canvas.rect(0, A4[1] - 0.35 * cm, A4[0], 0.35 * cm, stroke=0, fill=1)
        if wm:
            w = min(13 * cm, 13 * cm * wm[1]) if wm[1] >= 1 else 9 * cm * wm[1]
            h = w / wm[1]
            canvas.drawImage(wm[0], (A4[0] - w) / 2, (A4[1] - h) / 2, width=w, height=h, mask="auto")
        canvas.restoreState()
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


# =========================================================================== #
# Structured analytics report: every section reads from the verified facts (facts.py), so the PDF, the screen and the Excel
# file always show the same numbers.
# =========================================================================== #
def _money(v) -> str:
    v = float(v)
    return f"-₦{abs(v):,.2f}" if v < 0 else f"₦{v:,.2f}"


def build_analytics_pdf(company_name: str, title: str, question: str, answer: str | None, F: dict, table: pd.DataFrame | None = None,
                        chart_png: bytes | None = None, branding: dict | None = None, extra_tables: list | None = None,
                        findings_text: str | None = None) -> bytes:
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import CondPageBreak, KeepTogether, PageBreak

    branding = branding or {}
    try:
        brand = colors.HexColor(branding.get("color") or "#2b59ff")
    except ValueError:
        brand = colors.HexColor("#2b59ff")
    name = branding.get("display_name") or company_name
    today = dt.date.today().strftime("%d %b %Y")
    f_reg, f_bold = _fonts()
    ink, mute, line = colors.HexColor("#1c2230"), colors.HexColor("#6b7385"), colors.HexColor("#d5d9e4")
    zebra = colors.HexColor("#f5f6fa")
    ss = getSampleStyleSheet()
    s_name = ParagraphStyle("an_name", parent=ss["Normal"], fontName=f_bold, textColor=brand, fontSize=11, spaceAfter=2)
    s_title = ParagraphStyle("an_title", parent=ss["Title"], fontName=f_bold, alignment=0, fontSize=21, leading=25, textColor=ink, spaceAfter=2)
    s_sub = ParagraphStyle("an_sub", parent=ss["Normal"], fontName=f_reg, textColor=mute, fontSize=9, leading=12)
    s_h = ParagraphStyle("an_h", parent=ss["Normal"], fontName=f_bold, fontSize=13.5, leading=17, textColor=brand, spaceBefore=14, spaceAfter=5, keepWithNext=1)
    s_body = ParagraphStyle("an_body", parent=ss["Normal"], fontName=f_reg, fontSize=9.6, leading=13.5, textColor=ink)
    s_bul = ParagraphStyle("an_bul", parent=s_body, leftIndent=12, bulletIndent=2, spaceAfter=2)
    s_cell = ParagraphStyle("an_cell", parent=ss["Normal"], fontName=f_reg, fontSize=7.4, leading=9.2, textColor=ink)
    s_cellr = ParagraphStyle("an_cellr", parent=s_cell, alignment=2)
    s_head = ParagraphStyle("an_head", parent=s_cell, fontName=f_bold, textColor=colors.white)
    s_headr = ParagraphStyle("an_headr", parent=s_head, alignment=2)
    avail = A4[0] - 3.2 * cm

    def P(t, st=s_body):
        return Paragraph(escape(str(t)), st)

    def tbl(header, rows, weights, right=(), repeat=1, hl_rows=()):
        data = [[Paragraph(escape(str(h)), s_headr if i in right else s_head) for i, h in enumerate(header)]]
        for r in rows:
            data.append([Paragraph(escape(str(c)), s_cellr if i in right else s_cell) for i, c in enumerate(r)])
        t = Table(data, colWidths=[avail * w / sum(weights) for w in weights], repeatRows=repeat)
        st = [("BACKGROUND", (0, 0), (-1, 0), brand), ("GRID", (0, 0), (-1, -1), 0.25, line), ("VALIGN", (0, 0), (-1, -1), "TOP"),
              ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, zebra]), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        for i in hl_rows:
            st.append(("BACKGROUND", (0, i + 1), (-1, i + 1), colors.HexColor("#fff4e0")))
        t.setStyle(TableStyle(st))
        return t

    v, t = F["validation"], F["totals"]
    story = []
    logo_bytes = None
    if branding.get("logo_path"):
        try:
            import db
            logo_bytes = db.read_file(branding["logo_path"])
        except Exception:  # noqa: BLE001
            logo_bytes = None
    if logo_bytes:
        try:
            img = Image(io.BytesIO(logo_bytes)); ratio = img.imageWidth / img.imageHeight
            img.drawHeight, img.drawWidth, img.hAlign = 1.3 * cm, 1.3 * cm * ratio, "LEFT"
            story += [img, Spacer(1, 4)]
        except Exception:  # noqa: BLE001
            pass
    story += [P(name, s_name), P(title, s_title), P(f"Prepared {today}   |   {question}", s_sub), Spacer(1, 6)]

    # verification banner
    n_issues = len(v.get("issues") or [])
    ok_col = colors.HexColor("#fdecec") if not v["ok"] else (colors.HexColor("#fff4e0") if n_issues else colors.HexColor("#e7f6ec"))
    if not v["ok"]:
        banner_txt = (f"NOT FULLY VERIFIED: {v['passed']} of {v['total']} cross-checks passed. At least one cross-check failed. "
                      "Do not rely on the figures until the cause is found (see section 8).")
    elif n_issues:
        banner_txt = (f"CALCULATIONS VERIFIED, DATA-QUALITY ISSUES REMAIN: {v['passed']} of {v['total']} arithmetic and row cross-checks passed. "
                      f"This does not mean the data is clean: {n_issues} unresolved data-quality issue(s) are listed in sections 5 and 6.")
    else:
        banner_txt = (f"VERIFIED: {v['passed']} of {v['total']} cross-checks passed, and no data-quality issue was found. "
                      "Every figure in this report comes from the same verified dataset, and the totals were recounted a second way.")
    banner = Table([[Paragraph(escape(banner_txt), s_body)]], colWidths=[avail])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ok_col), ("BOX", (0, 0), (-1, -1), 0.6, line), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [banner]

    # 1. executive summary
    story += [P("1. Executive summary", s_h)]
    for s in F["summary"]:
        story.append(Paragraph(escape(s), s_bul, bulletText="•"))

    # 2. financial breakdown
    story += [P("2. Financial breakdown", s_h)]
    rows = [["Credits (money in)", f"{t['credit_count']:,}", _money(t["money_in"]), _money(t["average_in"]) if t["average_in"] is not None else "", _money(t["highest_in"]["Amount"]) if t["highest_in"] else "", _money(t["lowest_in"]["Amount"]) if t["lowest_in"] else ""],
            ["Debits (money out)", f"{t['debit_count']:,}", _money(t["money_out"]), _money(t["average_out"]) if t["average_out"] is not None else "", _money(t["highest_out"]["Amount"]) if t["highest_out"] else "", _money(t["lowest_out"]["Amount"]) if t["lowest_out"] else ""],
            ["Net movement", f"{t['rows']:,}", _money(t["net_movement"]), "", "", ""]]
    story += [tbl(["", "Count", "Total", "Average", "Highest", "Lowest"], rows, [2.2, 1.0, 1.9, 1.7, 1.7, 1.7], right=(1, 2, 3, 4, 5))]
    for key, nm in (("highest_in", "Highest credit"), ("highest_out", "Highest debit")):
        r = t.get(key)
        if r:
            story.append(P(f"{nm}: {_money(r['Amount'])} on {r['Date']} (reference {r['Reference']}, row {r.get('Row', r['No.'])}).", s_sub))
    story.append(P("Averages are over each direction's own transactions only. Highest and lowest are among counted transactions.", s_sub))
    if chart_png:
        story += [Spacer(1, 8), Image(io.BytesIO(chart_png), width=16 * cm, height=8.9 * cm)]

    # 3. transaction status (kept apart from credit/debit direction)
    stt = F.get("status") or {}
    story += [CondPageBreak(5 * cm), P("3. Transaction status", s_h)]
    story += [P(f"Rows: {F['rows_uploaded']:,} uploaded = {F['rows_analysed']:,} analysed + {F['rows_excluded']:,} excluded (every excluded row is listed in section 5.3).", s_sub), Spacer(1, 3)]
    if stt.get("column_found"):
        story += [tbl(["Status", "Count", "Credits", "Debits", "Net", "In gross totals", "In settled totals"],
                      [[r["status"], f"{r['count']:,}", f"{_money(r['credits'])} ({r['credit_count']:,})", f"{_money(r['debits'])} ({r['debit_count']:,})", _money(r["net"]), "Yes", "Yes" if r["in_settled_totals"] else "No"] for r in stt["rows"]],
                      [1.3, 0.7, 1.9, 1.9, 1.5, 1.0, 1.1], right=(1, 2, 3, 4)),
                  P(f"Gross totals include every status: in {_money(t['money_in'])}, out {_money(t['money_out'])}, net {_money(t['net_movement'])}. Settled (Completed only): in {_money(t['settled_money_in'])}, "
                    f"out {_money(t['settled_money_out'])}, net {_money(t['settled_net_movement'])}. Excluded rows are not in any total.", s_sub)]
        for r_ in stt["rows"]:
            if r_.get("note"):
                story.append(P(r_["note"], s_sub))
        odd = [(r_["status"], x) for r_ in stt["rows"] if r_["status"] != "Completed" for x in r_["transactions"]]
        if odd:
            story += [Spacer(1, 4), tbl(["Status", "Row", "Reference", "Date", "Type", "Amount", "Counterparty"],
                                        [[sname, x["Row"], x["Reference"], x["Date"] or "no date", x["Type"], _money(x["Amount"]), x["Counterparty"] or "no counterparty"] for sname, x in odd],
                                        [1.0, 0.6, 1.3, 1.1, 0.8, 1.4, 1.7], right=(5,))]
    else:
        story += [P("No status column was found in the file, so transaction status could not be analysed.", s_body)]

    # 4. daily activity
    story += [CondPageBreak(5 * cm), P("4. Daily activity", s_h)]
    if F["daily"]:
        hl = [i for i, x in enumerate(F["daily"]) if x["unusual"]]
        story += [tbl(["Date", "Txns", "Credits", "Debits", "Credit total", "Debit total", "Net", "Note"],
                      [[x["date"], f"{x['transactions']:,}", f"{x['credit_count']:,}", f"{x['debit_count']:,}", _money(x["credit_total"]), _money(x["debit_total"]), _money(x["net"]),
                        "Unusual" if x["unusual"] else ""] for x in F["daily"]],
                      [1.3, 0.8, 1.0, 1.0, 1.7, 1.7, 1.7, 1.0], right=(1, 2, 3, 4, 5, 6), hl_rows=hl),
                  P(f"{len(F['daily']):,} active day(s) listed. Shaded rows are unusual days.", s_sub)]
        for u in F["unusual_days"]:
            story += [Spacer(1, 4), P(f"Unusual day {u['date']}: " + " ".join(u["reasons"]), s_body)]
            story += [tbl(["Row", "Reference", "Type", "Amount", "Description"], [[r.get("Row", r["No."]), r["Reference"], r["Type"], _money(r["Amount"]), r["Narration"] or "no description"] for r in u["largest"]],
                          [0.7, 1.6, 0.9, 1.4, 3.4], right=(3,))]
    else:
        story += [P("No usable date column was found, so a daily summary could not be produced.", s_body)]

    # 4. findings
    story += [CondPageBreak(5 * cm), P("5. Transaction-level findings", s_h)]
    d = F["duplicates"]
    story += [P("5.1 Duplicate references", ParagraphStyle("h2", parent=s_body, fontName=f_bold, spaceBefore=4, spaceAfter=3, keepWithNext=1))]
    if not F["fields"]["reference"]:
        story += [P("No reference column was found in the file, so duplicate references could not be checked.", s_body)]
    elif not d["groups"]:
        story += [P(f"No reference is repeated among the {d['references_checked']:,} transaction(s) that carry one.", s_body)]
    else:
        story += [P(f"{len(d['groups']):,} reference(s) appear more than once: {d['groups_same']:,} with an identical amount and direction (most likely posted twice) and {d['groups_different']:,} with different amounts or directions (a reversal, part-payment, or a reused reference).", s_body)]
        rows = [[g["reference"], "Identical" if g["kind"].startswith("same") else "Differs", r.get("Row", r["No."]), r["Date"], r["Type"], _money(r["Amount"]), r["Narration"] or "no description"]
                for g in d["groups"] for r in g["rows"]]
        story += [tbl(["Reference", "Kind", "Row", "Date", "Type", "Amount", "Description"], rows, [1.5, 0.8, 0.7, 1.1, 0.8, 1.4, 2.4], right=(5,))]
    # 5.2 missing fields, 5.3 excluded rows (listed in full, from the same row ledger as the Excel file)
    h2 = ParagraphStyle("h2m", parent=s_body, fontName=f_bold, spaceBefore=4, spaceAfter=3, keepWithNext=1)
    if d.get("with_excluded"):
        story += [Spacer(1, 4), P("References also used on rows left out of the totals", h2),
                  tbl(["Reference", "Row", "Date", "Type", "Amount", "Used in totals"],
                      [[g["reference"], r["Row"], r["Date"], r["Type"], _money(r["Amount"]) if r["Amount"] is not None else "(none)", r["Used in totals"]] for g in d["with_excluded"] for r in g["rows"]],
                      [1.6, 0.7, 1.1, 0.9, 1.4, 1.4], right=(4,))]
    story += [Spacer(1, 6), P("5.2 Missing fields (a cell holding only spaces counts as missing)", h2)]
    mf = F.get("missing") or {}
    story += [tbl(["Field", "Missing", "Note"], [[fld, f"{m['count']:,}" if m["column_found"] else "-", "" if m["column_found"] else "no such column in the file"] for fld, m in mf.items()], [1.5, 0.8, 3.0])]
    mrows = [[fld, r["Row"], r["Reference"], r["Date"], r["Type"], _money(r["Amount"]) if r["Amount"] is not None else "(none)", r["Counterparty"], r["Used in totals"]] for fld, m in mf.items() for r in m["rows"]]
    if mrows:
        story += [Spacer(1, 4), tbl(["Field", "Row", "Reference", "Date", "Type", "Amount", "Counterparty", "In totals"], mrows, [1.1, 0.6, 1.2, 1.0, 0.8, 1.2, 1.4, 1.0], right=(5,))]
    story += [Spacer(1, 6), P(f"5.3 Excluded rows ({len(F.get('excluded') or []):,}) and rows counted with a warning ({len(F.get('warned') or []):,})", h2)]
    if F.get("excluded"):
        story += [tbl(["Row", "Reference", "Date", "Type", "Amount as uploaded", "Reason it is not in the totals"],
                      [[r["Row"], r["Reference"], r["Date"], r["Type"], _money(r["Amount"]) if r["Amount"] is not None else "(none)", r["Reason"]] for r in F["excluded"]], [0.6, 1.2, 1.0, 1.0, 1.3, 3.4], right=(4,))]
    else:
        story += [P("No row was excluded: every uploaded row is in the totals.", s_body)]
    if F.get("warned"):
        story += [Spacer(1, 4), tbl(["Row", "Reference", "Type", "Amount", "Warning (the row IS in the totals)"],
                                    [[r["Row"], r["Reference"], r["Type"], _money(r["Amount"]), r["Warnings"]] for r in F["warned"]], [0.6, 1.2, 0.9, 1.3, 3.5], right=(3,))]
    story += [Spacer(1, 6), P("5.4 Unusually large transactions", ParagraphStyle("h2b", parent=s_body, fontName=f_bold, spaceBefore=4, spaceAfter=3, keepWithNext=1))]
    if F["unusual"]:
        story += [tbl(["Row", "Date", "Reference", "Type", "Amount", "Why it stands out"], [[r.get("Row", r["No."]), r["Date"], r["Reference"], r["Type"], _money(r["Amount"]), r["why"]] for r in F["unusual"]],
                      [0.7, 1.0, 1.3, 0.8, 1.3, 3.8], right=(4,))]
    else:
        story += [P("No transaction is far above the normal range of its direction (or there are too few transactions to judge).", s_body)]
    if F["patterns"]:
        story += [Spacer(1, 6), P("5.5 Other patterns", ParagraphStyle("h2c", parent=s_body, fontName=f_bold, spaceBefore=4, spaceAfter=3, keepWithNext=1))]
        for p in F["patterns"]:
            story += [KeepTogether([P(f"[{p['severity'].upper()}] {p['title']} - {p['count']} row(s), total {_money(p['value'])}", ParagraphStyle("pt", parent=s_body, fontName=f_bold)), P(p["why"], s_sub), P("How it was found: " + p["method"], s_sub)])]
            story += [tbl(["Row", "Date", "Reference", "Type", "Amount", "Description"], [[r.get("Row", r["No."]), r["Date"], r["Reference"], r["Type"], _money(r["Amount"]), r["Narration"] or "no description"] for r in p["rows"]],
                          [0.7, 1.0, 1.4, 0.8, 1.3, 3.0], right=(4,)), Spacer(1, 5)]

    # 5. data quality
    story += [CondPageBreak(4 * cm), P("6. Data quality", s_h),
              tbl(["Check", "Status", "Detail"], [[q["check"], {"ok": "OK", "warn": "Attention", "info": "Note"}[q["status"]], q["detail"]] for q in F["quality"]] + [["Note", "Note", n] for n in F["notes"]], [1.6, 0.8, 5.6],
                  hl_rows=[i for i, q in enumerate(F["quality"]) if q["status"] == "warn"])]

    story += [Spacer(1, 6), P("How rows are treated", ParagraphStyle("h2t", parent=s_body, fontName=f_bold, spaceBefore=4, spaceAfter=3, keepWithNext=1))]
    for x_ in F.get("treatment", []):
        story.append(Paragraph(escape(x_), s_bul, bulletText="•"))

    # 6. recommendations
    story += [CondPageBreak(4 * cm), P("7. Recommendations", s_h)]
    for i, r in enumerate(F["recommendations"], 1):
        story.append(Paragraph(escape(r), s_bul, bulletText=f"{i}."))

    # 7. verification
    story += [CondPageBreak(4 * cm), P("8. Verification (cross-checks run before this report was produced)", s_h),
              tbl(["Check", "Result", "Detail"], [[c["check"], "PASS" if c["ok"] else "FAIL", c["detail"]] for c in v["checks"]], [3.2, 0.7, 4.1], hl_rows=[i for i, c in enumerate(v["checks"]) if not c["ok"]])]

    # 8. the question that was asked
    if answer or (table is not None and len(table.columns)):
        story += [PageBreak(), P("9. Your question and the answer", s_h), P(question, s_sub), Spacer(1, 4)]
        for line_ in (answer or "").splitlines():
            line_ = line_.strip()
            if line_:
                story.append(Paragraph(escape(line_[2:] if line_.startswith("- ") else line_), s_bul if line_.startswith("- ") else s_body, bulletText="•" if line_.startswith("- ") else None))
        if table is not None and len(table.columns):
            cols = list(table.columns)[:8]
            w = [1.0] * len(cols)
            story += [Spacer(1, 6), tbl([str(c) for c in cols], [[_cell(row[c]) for c in cols] for _, row in table.iterrows()], w, right=tuple(i for i, c in enumerate(cols) if pd.api.types.is_numeric_dtype(table[c]))),
                      P(f"{len(table):,} row(s) listed." + (" Columns not shown here are in the Excel file." if len(table.columns) > 8 else ""), s_sub)]

    # 9. detail tables (e.g. every transaction of a statement)
    for ttl, tdf, _cap in (extra_tables or []):
        if tdf is None or not len(tdf):
            continue
        cols = list(tdf.columns)[:12]
        weights = [{"date": 1.1, "reference": 1.4, "narration": 2.3, "description": 2.3, "what was found": 2.6, "group": 1.8}.get(str(c).lower(), 1.0) for c in cols]
        story += [PageBreak(), P(ttl, s_h), tbl([str(c) for c in cols], [[_cell(row[c]) for c in cols] for _, row in tdf.iterrows()], weights,
                                                right=tuple(i for i, c in enumerate(cols) if pd.api.types.is_numeric_dtype(tdf[c]))), P(f"{len(tdf):,} row(s) listed.", s_sub)]

    story += [Spacer(1, 10), P("Method and limits: all figures are plain arithmetic on the rows of the uploaded file (money is summed in whole kobo). Unusual amounts use the stated range rule "
                              "(above the upper quartile plus three times the middle-half spread). This report describes one file only: it cannot show that a transaction is missing, or that records agree with another source, "
                              "and totals alone do not prove that a business is healthy.", s_sub)]

    class Numbered(rl_canvas.Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for st in self._saved:
                self.__dict__.update(st)
                self.saveState()
                self.setFillColor(brand)
                self.rect(0, A4[1] - 0.35 * cm, A4[0], 0.35 * cm, stroke=0, fill=1)
                self.setFont(f_reg, 8)
                self.setFillColor(mute)
                self.drawString(1.6 * cm, 1.0 * cm, f"{name}  |  {today}  |  {(str(v['passed']) + '/' + str(v['total']) + ' checks passed' + (', data-quality issues remain' if n_issues else ', no data-quality issues')) if v['ok'] else 'NOT FULLY VERIFIED'}")
                self.drawRightString(A4[0] - 1.6 * cm, 1.0 * cm, f"Page {self._pageNumber} of {total}")
                self.restoreState()
                super().showPage()
            super().save()

    out = io.BytesIO()
    SimpleDocTemplate(out, pagesize=A4, leftMargin=1.6 * cm, rightMargin=1.6 * cm, topMargin=1.7 * cm, bottomMargin=1.9 * cm, title=title, author=name).build(story, canvasmaker=Numbered)
    return out.getvalue()
