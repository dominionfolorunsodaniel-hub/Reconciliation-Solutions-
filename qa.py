"""qa.py - answers to common questions straight from the verified facts, with NO AI.

Used when the AI service is unavailable (busy, timed out, rate limited, network failure or an unusable reply): RECONZA still answers
the question that was actually asked, from the same dataset as the full analysis, the PDF and the Excel file. A question that is not
one of the supported kinds is NOT answered with something unrelated and is NOT guessed: answer() returns None and the caller says so.
"""
from __future__ import annotations

import re

import pandas as pd

import facts

NAIRA = "₦"


def _m(v) -> str:
    return facts._m(v)


def _df(rows, cols) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=cols)


_ROW_COLS = ["Row", "Reference", "Date", "Type", "Amount", "Counterparty", "Status", "Description"]


def _detail_rows(rows) -> pd.DataFrame:
    return _df([[r.get(c, "") for c in _ROW_COLS] for r in rows], _ROW_COLS)


def _tx_rows(F, status=None) -> pd.DataFrame:
    """Analysed transactions of a status from the facts (file row numbers)."""
    out = []
    for g in F["status"].get("rows", []):
        if status is None or g["status"].lower() == status.lower():
            out += [{"Row": x["Row"], "Reference": x["Reference"], "Date": x["Date"], "Type": x["Type"], "Amount": x["Amount"], "Counterparty": x["Counterparty"],
                     "Status": g["status"], "Description": x["Narration"]} for x in g["transactions"]]
    return _detail_rows(out)


def _ledger_rows(F, mask_fn) -> pd.DataFrame:
    import ledger
    L = F.get("_ledger")
    if L is None:
        return _detail_rows([])
    return _detail_rows([{**ledger.row_detail(r), "Amount": ledger.row_detail(r)["Amount"]} for _, r in L[mask_fn(L)].iterrows()])


# --- what the question is about -------------------------------------------------------------------------------------
_W = lambda *words: re.compile(r"\b(" + "|".join(words) + r")\b", re.I)   # noqa: E731
RX = {
    "missing": _W(r"missing", r"blank", r"empty", r"incomplete", r"without a?n?\s*\w+", r"no (reference|description|counterparty|date|status|type)"),
    "duplicate": _W(r"duplicates?", r"duplicated", r"repeated", r"repeats?", r"twice", r"same reference"),
    "excluded": _W(r"excluded", r"exclude", r"left out", r"not counted", r"ignored", r"skipped", r"rejected", r"invalid", r"negative", r"zero", r"unsupported", r"anomal\w+", r"errors?", r"bad rows?"),
    "status": _W(r"pending", r"reversed", r"reversals?", r"failed", r"cancel\w+", r"completed", r"settled", r"status(es)?"),
    "daily": _W(r"daily", r"per day", r"each day", r"by day", r"busiest", r"busy day", r"day by day"),
    "unusual": _W(r"unusual", r"outliers?", r"suspicious", r"abnormal", r"too large", r"very large", r"unusually"),
    "quality": _W(r"data quality", r"quality", r"verified", r"verification", r"validate\w*", r"can i trust", r"reliable", r"cross-?check\w*"),
    "overview": _W(r"summary", r"summari[sz]e", r"overview", r"analy[sz]e", r"analysis", r"report", r"overall", r"how (did|is|are) we"),
    "count": _W(r"how many", r"count", r"number of"),
    "credit": _W(r"credits?", r"money in", r"inflows?", r"received", r"income", r"deposits?"),
    "debit": _W(r"debits?", r"money out", r"outflows?", r"spent", r"spending", r"expenses?", r"withdrawals?", r"payments? made"),
    "net": _W(r"net", r"net movement", r"balance change", r"difference"),
    "total": _W(r"total", r"sum", r"how much", r"amount"),
    "avg": _W(r"average", r"mean", r"typical"),
    "high": _W(r"highest", r"largest", r"biggest", r"maximum", r"max", r"top"),
    "low": _W(r"lowest", r"smallest", r"minimum", r"min"),
}
FIELDS = {"reference": "Reference", "description": "Description", "narration": "Description", "counterparty": "Counterparty", "date": "Date", "status": "Status",
          "type": "Type", "amount": "Amount"}


def examples() -> list[str]:
    return ["How many credits and debits are there, and what are the totals?", "What is the net movement?", "Which transactions are pending or reversed?",
            "Show the duplicate references.", "Which rows have a missing reference, description or counterparty?", "Which rows were excluded and why?",
            "What was the busiest day?", "What is the highest credit?", "Is this file verified?"]


def answer(question: str, F: dict | None, df: pd.DataFrame | None = None) -> dict | None:
    """{"title", "table" (DataFrame), "text" (plain sentences, all calculated), "intent"} or None when this question needs the AI."""
    q = (question or "").strip()
    if not q or not F:
        return None
    t = F["totals"]
    ql = q.lower()

    # a specific reference that exists in the file
    L = F.get("_ledger")
    if L is not None:
        import ledger
        refs = {str(r).strip(): r for r in L["Reference"].unique() if len(str(r).strip()) >= 3 and str(r).strip().lower() not in ledger._MISSING_TEXT}
        hit = [r for r in sorted(refs, key=len, reverse=True) if re.search(r"(?<![A-Za-z0-9])" + re.escape(r.lower()) + r"(?![A-Za-z0-9])", ql)]
        if hit:
            rows = L[L["Reference"].isin(hit)]
            det = [{**ledger.row_detail(r), "Used in totals": "Yes" if r["Include"] else "No - " + r["Reasons"]} for _, r in rows.iterrows()]
            tab = _df([[d.get(c, "") for c in _ROW_COLS + ["Used in totals"]] for d in det], _ROW_COLS + ["Used in totals"])
            lines = [f"Reference {', '.join(hit)} appears on {len(rows):,} row(s) (rows {', '.join(str(int(x)) for x in rows['Row'].dropna())})."]
            for d in det:
                amt = _m(d["Amount"]) if d["Amount"] is not None else "(no amount)"
                lines.append(f"Row {d['Row']}: {d['Date']} | {d['Type']} {amt} | {d['Counterparty']} | status {d['Status']} | counted in totals: {d['Used in totals']}.")
            return {"title": f"Reference {', '.join(hit)}", "table": tab, "text": lines, "intent": "reference"}

    def has(k):
        return bool(RX[k].search(ql))

    # --- missing fields
    if has("missing"):
        wanted = [v for k, v in FIELDS.items() if re.search(r"\b" + k + r"s?\b", ql)]
        wanted = list(dict.fromkeys(wanted)) or list(F["missing"].keys())
        rows, lines = [], []
        for fld in wanted:
            m = F["missing"].get(fld)
            if m is None:
                continue
            if not m["column_found"]:
                lines.append(f"{fld}: the file has no {fld.lower()} column, so it could not be checked.")
                continue
            lines.append(f"{fld}: {m['count']:,} row(s) missing" + (" (rows " + ", ".join(str(r["Row"]) for r in m["rows"]) + ")" if m["count"] else "") + ".")
            rows += [{"Field": fld, **{c: r[c] for c in _ROW_COLS}, "Used in totals": r["Used in totals"]} for r in m["rows"]]
        return {"title": "Missing fields", "table": _df([[r[c] for c in ["Field"] + _ROW_COLS + ["Used in totals"]] for r in rows], ["Field"] + _ROW_COLS + ["Used in totals"]),
                "text": ["Whitespace-only cells count as missing. Counts are over every uploaded row."] + lines, "intent": "missing"}

    # --- duplicates
    if has("duplicate"):
        d = F["duplicates"]
        rows = [[g["reference"], g.get("assessment", ""), r["Row"], r["Date"], r["Type"], r["Amount"], r["Narration"], "Yes"] for g in d["groups"] for r in g["rows"]]
        rows += [[g["reference"], "Also used on a row left out of the totals", r["Row"], r["Date"], r["Type"], r["Amount"], r["Description"], r["Used in totals"]]
                 for g in d.get("with_excluded", []) for r in g["rows"]]
        lines = ([f"{len(d['groups']):,} reference(s) are repeated among counted rows: {d['groups_same']:,} with an identical amount and direction, {d['groups_different']:,} that differ."]
                 if d["groups"] else ["No reference is repeated among counted rows."])
        lines += [f"Reference {g['reference']}: {g['count']} occurrences in rows {', '.join(map(str, g['row_numbers']))} - {g['assessment']}." for g in d["groups"][:15]]
        if d.get("with_excluded"):
            lines.append(f"{len(d['with_excluded']):,} reference(s) are also used on rows left out of the totals.")
        lines.append("Duplicate-looking rows are never removed: counted ones stay in the totals.")
        return {"title": "Duplicate references", "table": _df(rows, ["Reference", "Assessment", "Row", "Date", "Type", "Amount", "Description", "In totals"]), "text": lines, "intent": "duplicates"}

    # --- excluded / invalid
    if has("excluded") and not (has("status") and re.search(r"pending|reversed", ql)):
        ex = F["excluded"]
        lines = [f"Rows: {F['rows_uploaded']:,} uploaded = {F['rows_analysed']:,} analysed + {F['rows_excluded']:,} excluded."]
        lines += [f"Row {r['Row']} (ref {r['Reference']}): {r['Reason']}." for r in ex[:30]]
        if not ex:
            lines.append("No row was excluded.")
        tab = _df([[r["Row"], r["Reference"], r["Date"], r["Type"], r["Amount"], r["Counterparty"], r["Status"], r["Reason"]] for r in ex],
                  ["Row", "Reference", "Date", "Type", "Amount as uploaded", "Counterparty", "Status", "Reason it is not in the totals"])
        return {"title": "Excluded rows", "table": tab, "text": lines, "intent": "excluded"}

    # --- status
    if has("status"):
        if not F["status"]["column_found"]:
            return {"title": "Transaction status", "table": _df([], ["Status"]), "text": ["The file has no status column, so status cannot be analysed."], "intent": "status"}
        want = [s for s in ("Pending", "Reversed", "Failed", "Cancelled", "Completed") if re.search(s[:-1].lower() if s != "Completed" else "complet|settled", ql)]
        rows = F["status"]["rows"]
        lines = [f"{r['status']}: {r['count']:,} transaction(s), {_m(r['credits'])} in ({r['credit_count']:,}) and {_m(r['debits'])} out ({r['debit_count']:,}). "
                 f"{'In' if r['in_gross_totals'] else 'Not in'} the gross totals; {'in' if r['in_settled_totals'] else 'not in'} the settled totals." for r in rows if not want or r["status"] in want]
        lines.append(f"Gross (every status): in {_m(t['money_in'])}, out {_m(t['money_out'])}. Settled (Completed only): in {_m(t['settled_money_in'])}, out {_m(t['settled_money_out'])}.")
        tab = _df([[r["status"], r["count"], r["credits"], r["debits"], r["net"]] for r in rows if not want or r["status"] in want], ["Status", "Count", "Credits", "Debits", "Net"])
        det = pd.concat([_tx_rows(F, s) for s in (want or [r["status"] for r in rows if r["status"] != "Completed"])], ignore_index=True) if (want or any(r["status"] != "Completed" for r in rows)) else _tx_rows(F, "none")
        if len(det):
            lines += [f"{x['Status']} - row {x['Row']}, ref {x['Reference']}, {x['Date'] or 'no date'}, {x['Type']} {_m(x['Amount'])}, {x['Counterparty'] or 'no counterparty'}." for _, x in det.head(40).iterrows()]
        return {"title": "Transaction status", "table": det if len(det) else tab, "text": lines, "intent": "status", "table2": tab}

    # --- daily
    if has("daily") and F["daily"]:
        best = max(F["daily"], key=lambda x: x["transactions"])
        big = max(F["daily"], key=lambda x: x["gross"])
        lines = [f"{len(F['daily']):,} active day(s). Busiest day by number of transactions: {best['date']} ({best['transactions']:,}). Largest total movement: {big['date']} ({_m(big['gross'])})."]
        und = F["rows_analysed"] - sum(x["transactions"] for x in F["daily"])
        if und:
            lines.append(f"{und:,} counted transaction(s) have no usable date, so they are in the totals but not in any day.")
        tab = _df([[x["date"], x["transactions"], x["credit_count"], x["debit_count"], x["credit_total"], x["debit_total"], x["net"]] for x in F["daily"]],
                  ["Date", "Transactions", "Credits", "Debits", "Credit total", "Debit total", "Net"])
        return {"title": "Daily activity", "table": tab, "text": [x for x in lines if x], "intent": "daily"}

    # --- unusual
    if has("unusual"):
        rows = [[r["Row"], r["Date"], r["Reference"], r["Type"], r["Amount"], r["why"]] for r in F["unusual"]]
        lines = ([f"{len(F['unusual']):,} transaction(s) are far above the normal range of their direction."] if rows else ["No transaction is far above the normal range of its direction (or there are too few to judge)."])
        lines += [f"Pattern: {p['title']} ({p['count']} row(s))." for p in F["patterns"][:8]]
        return {"title": "Unusual transactions", "table": _df(rows, ["Row", "Date", "Reference", "Type", "Amount", "Why it stands out"]), "text": lines, "intent": "unusual"}

    # --- data quality / verification
    if has("quality"):
        v = F["validation"]
        lines = [f"Verification status: {v['status']}. {v['passed']} of {v['total']} cross-checks passed.",
                 "Arithmetic checks prove the totals agree with each other; they do not prove the data is clean."]
        lines += [f"{q_['check']}: {q_['detail']}" for q_ in F["quality"] if q_["status"] == "warn"][:12]
        return {"title": "Data quality and verification", "table": _df([[c["check"], "PASS" if c["ok"] else "FAIL", c.get("kind", ""), c["detail"]] for c in v["checks"]], ["Check", "Result", "Type", "Detail"]),
                "text": lines, "intent": "quality"}

    # --- money questions
    want_cr, want_dr = has("credit"), has("debit")
    money = has("total") or has("net") or has("count") or has("avg") or has("high") or has("low") or want_cr or want_dr
    if money and not has("overview"):
        lines, rows = [], []
        both = want_cr == want_dr   # neither or both named: give both sides
        if both or want_cr:
            rows.append(["Credits (money in)", t["credit_count"], t["money_in"], t["average_in"], (t["highest_in"] or {}).get("Amount"), (t["lowest_in"] or {}).get("Amount")])
        if both or want_dr:
            rows.append(["Debits (money out)", t["debit_count"], t["money_out"], t["average_out"], (t["highest_out"] or {}).get("Amount"), (t["lowest_out"] or {}).get("Amount")])
        if both:
            rows.append(["Net movement", t["rows"], t["net_movement"], None, None, None])
        for name, key, hk, lk, ak, ck, tk in (("credits", "in", "highest_in", "lowest_in", "average_in", "credit_count", "money_in"), ("debits", "out", "highest_out", "lowest_out", "average_out", "debit_count", "money_out")):
            if (both or (want_cr if name == "credits" else want_dr)):
                lines.append(f"{name.capitalize()}: {t[ck]:,} transaction(s), total {_m(t[tk])}" + (f", average {_m(t[ak])}" if t[ak] is not None else "")
                             + (f", highest {_m(t[hk]['Amount'])} (row {t[hk]['Row']}, ref {t[hk]['Reference']})" if t[hk] else "")
                             + (f", lowest {_m(t[lk]['Amount'])} (row {t[lk]['Row']}, ref {t[lk]['Reference']})" if t[lk] else "") + ".")
        if both:
            lines.append(f"Net movement: {_m(t['net_movement'])} (credits {_m(t['money_in'])} minus debits {_m(t['money_out'])}).")
        lines.append(f"Population: the {F['rows_analysed']:,} counted transactions of {F['rows_uploaded']:,} uploaded rows; {F['rows_excluded']:,} excluded row(s) are not included. "
                     "Every status is included in these gross figures" + (f"; Completed only: in {_m(t['settled_money_in'])}, out {_m(t['settled_money_out'])}, net {_m(t['settled_net_movement'])}." if "settled_money_in" in t else "."))
        return {"title": "Totals", "table": _df(rows, ["", "Count", "Total", "Average", "Highest", "Lowest"]), "text": lines, "intent": "totals"}

    # --- overview of the whole file (only when the person actually asked for it)
    if has("overview"):
        return {"title": "Summary of the whole file", "table": _df([[s] for s in F["summary"]], ["Verified summary"]), "text": list(F["summary"]), "intent": "overview"}
    return None


def headline_table(F: dict) -> pd.DataFrame:
    """A few verified headline figures, shown with an honest 'could not answer' message (not a substitute answer)."""
    t = F["totals"]
    return _df([["Credits (money in)", t["credit_count"], t["money_in"]], ["Debits (money out)", t["debit_count"], t["money_out"]], ["Net movement", t["rows"], t["net_movement"]],
                ["Rows uploaded / analysed / excluded", f"{F['rows_uploaded']:,} / {F['rows_analysed']:,} / {F['rows_excluded']:,}", ""]], ["Verified figure", "Count", "Amount"])


def classify_error(err) -> tuple[str, str]:
    """('timeout' | 'rate_limit' | 'network' | 'invalid_response' | 'unavailable', plain-English reason)"""
    msg = str(err).lower()
    if "rate limit" in msg or "429" in msg or "quota" in msg:
        return "rate_limit", "the AI service is receiving too many requests"
    if "time budget" in msg or "timed out" in msg or "timeout" in msg or "slow right now" in msg:
        return "timeout", "the AI service took too long to reply"
    if "could not connect" in msg or "connection" in msg or "network" in msg or "unreachable" in msg:
        return "network", "RECONZA could not reach the AI service"
    if "no answer" in msg or "empty answer" in msg or "invalid" in msg or "json" in msg or "parse" in msg:
        return "invalid_response", "the AI service sent a reply that could not be used"
    return "unavailable", "the AI service is not available"


def unavailable_message(kind_reason: str) -> list[str]:
    return [f"I could not answer this question just now because {kind_reason}, and it needs the AI to be understood. I have not guessed an answer.",
            "Nothing was charged for this question. Press Try again in a minute.",
            "Meanwhile RECONZA can answer these kinds of question without the AI: " + " | ".join(examples()[:6]) + "."]
