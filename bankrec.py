"""Bank statement vs internal ledger reconciliation.

Deterministic: every number here comes from plain arithmetic on the uploaded rows. The AI never calculates and
never decides a category. Nothing is dropped: a row with no reference, a duplicated reference or a missing amount
each appear in the result under their own category with the amount that is really in the file.

Rules
- The Reference is the key. Credits are compared with credits and debits with debits.
- A blank amount is "missing", never zero. A genuine 0 stays 0.
- A reference that is repeated in either file is not silently paired up: every row of it is listed as
  "Duplicate reference" for a person to look at.
- Differences up to the tolerance count as equal. The tolerance is printed in the report.
"""
from __future__ import annotations

import re
from datetime import datetime

import numpy as np
import pandas as pd

MATCHED = "Matched"
CREDIT_MISMATCH = "Credit mismatch"
DEBIT_MISMATCH = "Debit mismatch"
BOTH_MISMATCH = "Credit and debit mismatch"
DIRECTION = "Credit/debit mismatch"
BANK_ONLY = "Only in bank"
LEDGER_ONLY = "Only in ledger"
MANUAL = "Manually matched"
DUPLICATE = "Duplicate reference"
MISSING_AMT = "Missing amount"
NO_REF = "Missing reference"

ORDER = [MATCHED, MANUAL, CREDIT_MISMATCH, DEBIT_MISMATCH, BOTH_MISMATCH, DIRECTION, BANK_ONLY, LEDGER_ONLY, DUPLICATE, MISSING_AMT, NO_REF]

RESOLUTION = {
    MATCHED: "No action needed.",
    CREDIT_MISMATCH: "Check the credited amount in both records against the source document. Correct whichever side is wrong, "
                     "or record the difference as a fee, charge or short payment.",
    DEBIT_MISMATCH: "Check the debited amount in both records against the source document. Correct whichever side is wrong, "
                    "or record the difference as a fee, charge or over-payment.",
    BOTH_MISMATCH: "Both the credit and the debit differ. Check this transaction in full against the source document.",
    DIRECTION: "One record shows money coming in and the other shows money going out. Check whether the entry was posted "
               "on the wrong side or is a reversal.",
    BANK_ONLY: "The bank shows this transaction but the ledger does not. Find the source document and post it to the ledger "
               "(or investigate if it is unexpected).",
    LEDGER_ONLY: "The ledger shows this transaction but the bank does not. Check whether it is still in transit, was "
                 "recorded in error, or failed at the bank.",
    MANUAL: "A person paired these two records by hand. Keep the reason on file; any remaining amount difference is still counted.",
    DUPLICATE: "This reference appears more than once. Check whether it was posted twice, or whether separate transactions "
               "share one reference, and fix the record that is wrong.",
    MISSING_AMT: "An amount is blank in one or both records. Get the real amount from the source document before matching.",
    NO_REF: "This row has no reference so it cannot be matched. Add the reference or match it by hand.",
}

_REF = re.compile(r"^(transaction\s*)?(ref(erence)?(\s*(no|number|id))?|rrn|stan|txn(\s*id)?|trx(\s*id)?|trans(action)?\s*id|session\s*id|id)$", re.I)
_REF_LOOSE = re.compile(r"ref|rrn|txn|trx|trans(action)?\s*(id|no)|session", re.I)
_DATE = re.compile(r"date|time|posted|value\s*day", re.I)
_DESC = re.compile(r"descr|narrat|particular|detail|remark|memo|note|payee|beneficiar", re.I)
_CREDIT = re.compile(r"credit|\bcr\b|deposit|money\s*in|inflow|received", re.I)
_DEBIT = re.compile(r"debit|\bdr\b|withdraw|money\s*out|outflow|paid\s*out|payment", re.I)
_AMOUNT = re.compile(r"amount|amt|value", re.I)
_TYPE = re.compile(r"credit\s*/\s*debit|dr\s*/\s*cr|cr\s*/\s*dr|type|direction|\bdc\b", re.I)
_BAL = re.compile(r"balance|bal\b", re.I)


def _money(s: pd.Series) -> pd.Series:
    """Text or numbers to floats. Blank / unreadable stays NaN (missing), it is never turned into 0."""
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce").astype(float)

    def one(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return np.nan
        t = str(v).strip()
        if t == "" or t.lower() in {"nan", "none", "null", "-", "n/a", "na"}:
            return np.nan
        neg = t.startswith("(") and t.endswith(")")
        t = re.sub(r"[^0-9.\-]", "", t)
        if t in {"", "-", ".", "-."}:
            return np.nan
        try:
            x = float(t)
        except ValueError:
            return np.nan
        return -x if neg else x
    return s.map(one).astype(float)


def _first(df: pd.DataFrame, rx, skip=()):
    for c in df.columns:
        if c not in skip and rx.search(str(c)):
            return c
    return None


def detect_columns(df: pd.DataFrame) -> dict:
    """Which column is the reference / date / description / credit / debit. Returns {} entries as None when absent."""
    cols = list(df.columns)
    ref = next((c for c in cols if _REF.match(str(c).strip())), None) or _first(df, _REF_LOOSE)
    used = {ref}
    credit = _first(df, _CREDIT, used)
    used.add(credit)
    debit = _first(df, _DEBIT, used)
    used.add(debit)
    typ = amount = None
    if not (credit and debit):
        typ = _first(df, _TYPE, used)
        used.add(typ)
        amount = _first(df, _AMOUNT, used | {c for c in cols if _BAL.search(str(c))})
        used.add(amount)
    date = _first(df, _DATE, used)
    used.add(date)
    desc = _first(df, _DESC, used)
    return {"reference": ref, "date": date, "description": desc, "credit": credit, "debit": debit, "type": typ, "amount": amount}


def usable(df: pd.DataFrame) -> bool:
    c = detect_columns(df)
    return bool(c["reference"] and ((c["credit"] and c["debit"]) or (c["amount"] and c["type"])))


def applicable(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    return usable(a) and usable(b)


def _clean_ref(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    t = re.sub(r"\s+", "", str(v)).upper()
    return "" if t in {"NAN", "NONE", "NULL"} else t


def _is_credit_word(t: str) -> bool | None:
    t = str(t).strip().lower()
    if re.match(r"^(cr|credit|c|deposit|in|inflow)\b", t):
        return True
    if re.match(r"^(dr|debit|d|withdrawal|out|outflow)\b", t):
        return False
    return None


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """One row per source row with: ref, date, desc, credit, debit (NaN = missing), src_row."""
    c = detect_columns(df)
    out = pd.DataFrame(index=df.index)
    out["ref_raw"] = df[c["reference"]]
    out["ref"] = df[c["reference"]].map(_clean_ref)
    out["date"] = df[c["date"]] if c["date"] else ""
    out["desc"] = df[c["description"]].fillna("").astype(str) if c["description"] else ""
    if c["credit"] and c["debit"]:
        out["credit"] = _money(df[c["credit"]])
        out["debit"] = _money(df[c["debit"]])
    else:
        amt = _money(df[c["amount"]])
        kind = df[c["type"]].map(_is_credit_word)
        credit = pd.Series(np.nan, index=df.index)
        debit = pd.Series(np.nan, index=df.index)
        for i in df.index:
            v, k = amt.loc[i], kind.loc[i]
            if np.isnan(v):
                continue
            if k is True or (k is None and v >= 0):
                credit.loc[i] = v
            else:
                debit.loc[i] = v
        out["credit"], out["debit"] = credit, debit
    out["credit"] = out["credit"].abs()   # magnitude: debits are sometimes stored as negatives
    out["debit"] = out["debit"].abs()
    out["src_row"] = np.arange(2, len(df) + 2)  # row number as seen in a spreadsheet (row 1 = headings)
    return out.reset_index(drop=True)


def _dir(r) -> set:
    d = set()
    if not np.isnan(r["credit"]) and r["credit"] != 0:
        d.add("credit")
    if not np.isnan(r["debit"]) and r["debit"] != 0:
        d.add("debit")
    return d


def _fmt_date(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)) or str(v).strip() in {"", "NaT", "nan"}:
        return ""
    try:
        return pd.Timestamp(v).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return str(v)


def _num(v):
    return None if (v is None or (isinstance(v, float) and np.isnan(v))) else round(float(v), 2)


def _primary(r):
    """The amount to show for a record: the credit if it has one, else the debit, else whatever is not blank."""
    for k in ("credit", "debit"):
        v = r[k]
        if not np.isnan(v) and v != 0:
            return float(v)
    for k in ("credit", "debit"):
        if not np.isnan(r[k]):
            return float(r[k])
    return np.nan


def reconcile_bank_ledger(bank: pd.DataFrame, ledger: pd.DataFrame, tolerance: float = 0.01,
                          bank_name: str = "Bank statement", ledger_name: str = "Ledger", manual_pairs: list | None = None) -> dict:
    tol = abs(float(tolerance))
    B, L = normalise(bank), normalise(ledger)
    rows: list[dict] = []

    def rec(ref, st, b=None, l=None, diff=None, bank_row=None, ledger_row=None, note=""):
        src = b if b is not None else l
        date_b = _fmt_date(b["date"]) if b is not None else ""
        date_l = _fmt_date(l["date"]) if l is not None else ""
        desc = (b["desc"] if b is not None and str(b["desc"]).strip() else (l["desc"] if l is not None else "")) or ""
        rows.append({
            "Reference": ref or "(blank)", "Date": date_b or date_l, "Description": desc,
            "Bank credit": _num(b["credit"]) if b is not None else None, "Bank debit": _num(b["debit"]) if b is not None else None,
            "Ledger credit": _num(l["credit"]) if l is not None else None, "Ledger debit": _num(l["debit"]) if l is not None else None,
            "Bank amount": _num(_primary(b)) if b is not None else None, "Ledger amount": _num(_primary(l)) if l is not None else None,
            "Difference (A-B)": diff, "Status": st, "Recommended resolution": RESOLUTION[st],
            "Note": note, "Bank row": bank_row, "Ledger row": ledger_row,
            "Bank date": date_b, "Ledger date": date_l,
        })
        _ = src

    dup_b = set(B.loc[(B["ref"] != "") & B["ref"].duplicated(keep=False), "ref"])
    dup_l = set(L.loc[(L["ref"] != "") & L["ref"].duplicated(keep=False), "ref"])
    dups = dup_b | dup_l

    for _, r in B[B["ref"] == ""].iterrows():
        rec("", NO_REF, b=r, bank_row=int(r["src_row"]), note="Bank row without a reference")
    for _, r in L[L["ref"] == ""].iterrows():
        rec("", NO_REF, l=r, ledger_row=int(r["src_row"]), note="Ledger row without a reference")

    bi = {k: g for k, g in B[B["ref"] != ""].groupby("ref", sort=False)}
    li = {k: g for k, g in L[L["ref"] != ""].groupby("ref", sort=False)}
    seen: set = set()
    for ref in list(bi) + [k for k in li if k not in bi]:
        if ref in seen:
            continue
        seen.add(ref)
        bg, lg = bi.get(ref), li.get(ref)
        if ref in dups:
            side = []
            if ref in dup_b:
                side.append(f"{len(bg)} times in the bank")
            if ref in dup_l:
                side.append(f"{len(lg)} times in the ledger")
            note = "Appears " + " and ".join(side)
            for _, r in (bg.iterrows() if bg is not None else []):
                rec(ref, DUPLICATE, b=r, bank_row=int(r["src_row"]), note=note)
            for _, r in (lg.iterrows() if lg is not None else []):
                rec(ref, DUPLICATE, l=r, ledger_row=int(r["src_row"]), note=note)
            continue
        b = bg.iloc[0] if bg is not None else None
        l = lg.iloc[0] if lg is not None else None
        if l is None:
            rec(ref, BANK_ONLY, b=b, diff=_num(_primary(b)), bank_row=int(b["src_row"]))
            continue
        if b is None:
            rec(ref, LEDGER_ONLY, l=l, diff=_num(-_primary(l)) if not np.isnan(_primary(l)) else None, ledger_row=int(l["src_row"]))
            continue
        db, dl = _dir(b), _dir(l)
        bn, ln = int(b["src_row"]), int(l["src_row"])
        note = ""
        if _fmt_date(b["date"]) and _fmt_date(l["date"]) and _fmt_date(b["date"]) != _fmt_date(l["date"]):
            note = f"Dates differ: bank {_fmt_date(b['date'])}, ledger {_fmt_date(l['date'])}."
        if not db or not dl:
            rec(ref, MISSING_AMT, b=b, l=l, diff=None, bank_row=bn, ledger_row=ln,
                note=("No amount in the bank row. " if not db else "") + ("No amount in the ledger row. " if not dl else "") + note)
            continue
        if db != dl:
            diff = _num((_primary(b) or 0) - (_primary(l) or 0))
            rec(ref, DIRECTION, b=b, l=l, diff=diff, bank_row=bn, ledger_row=ln,
                note=f"Bank shows a {'/'.join(sorted(db))}, ledger shows a {'/'.join(sorted(dl))}. " + note)
            continue
        bad, diff = [], 0.0
        for k, label in (("credit", "Credit"), ("debit", "Debit")):
            if k in db:
                d = float(b[k]) - float(l[k])
                if abs(d) > tol:
                    bad.append(label)
                    diff += d
        st = MATCHED if not bad else (BOTH_MISMATCH if len(bad) == 2 else CREDIT_MISMATCH if bad == ["Credit"] else DEBIT_MISMATCH)
        rec(ref, st, b=b, l=l, diff=round(diff, 2) if bad else 0.0, bank_row=bn, ledger_row=ln, note=note)

    detail = pd.DataFrame(rows)
    applied_manual = 0
    for mp in (manual_pairs or []):
        br, lr = _clean_ref(mp["bank_ref"]), _clean_ref(mp["ledger_ref"])
        bi_ = detail.index[(detail["Status"] == BANK_ONLY) & (detail["Reference"].map(_clean_ref) == br)]
        li_ = detail.index[(detail["Status"] == LEDGER_ONLY) & (detail["Reference"].map(_clean_ref) == lr)]
        if len(bi_) != 1 or len(li_) != 1:
            continue   # one of them is no longer unmatched (new files): ignore this pair
        brow, lrow = detail.loc[bi_[0]].copy(), detail.loc[li_[0]]
        amt_b, amt_l = brow["Bank amount"], lrow["Ledger amount"]
        brow["Reference"] = brow["Reference"] if br == lr else f"{brow['Reference']} <-> {lrow['Reference']}"
        for k in ("Ledger credit", "Ledger debit", "Ledger amount", "Ledger row", "Ledger date"):
            brow[k] = lrow[k]
        diff = round(float(amt_b) - float(amt_l), 2) if pd.notna(amt_b) and pd.notna(amt_l) else None
        brow["Difference (A-B)"] = diff
        brow["Status"] = MANUAL
        brow["Recommended resolution"] = RESOLUTION[MANUAL]
        brow["Note"] = f"Matched by hand by {mp.get('created_by_name') or 'a user'} on {str(mp.get('created_at', ''))[:10]}. Reason: {mp.get('reason') or 'none given'}." + (f" Amounts still differ by {abs(diff):,.2f}." if diff and abs(diff) > tol else "")
        detail.loc[bi_[0]] = brow
        detail = detail.drop(index=li_[0])
        applied_manual += 1
    detail = detail.reset_index(drop=True)
    detail["_o"] = detail["Status"].map({s: i for i, s in enumerate(ORDER)})
    detail = detail.sort_values(["_o", "Reference"], kind="stable").drop(columns="_o").reset_index(drop=True)
    return _finish(detail, B, L, tol, bank_name, ledger_name, dup_b, dup_l)


def _amount_of(r) -> float:
    for k in ("Bank amount", "Ledger amount"):
        v = r[k]
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            return abs(float(v))
    return 0.0


def _discrepancy_value(detail: pd.DataFrame) -> float:
    """Total absolute discrepancy: |difference| of every mismatch plus the full amount of every one-sided transaction."""
    total = 0.0
    for _, r in detail.iterrows():
        if r["Status"] in (CREDIT_MISMATCH, DEBIT_MISMATCH, BOTH_MISMATCH, DIRECTION, MANUAL):
            total += abs(float(r["Difference (A-B)"] or 0))
        elif r["Status"] in (BANK_ONLY, LEDGER_ONLY):
            total += _amount_of(r)
    return round(total, 2)


def _side_totals(N: pd.DataFrame) -> dict:
    c, d = N["credit"].dropna(), N["debit"].dropna()
    out = {"rows": int(len(N)), "credit_total": round(float(c.sum()), 2), "credit_count": int(len(c)),
           "debit_total": round(float(d.sum()), 2), "debit_count": int(len(d))}
    out["net_movement"] = round(out["credit_total"] - out["debit_total"], 2)
    out["credit_average"] = round(out["credit_total"] / out["credit_count"], 2) if out["credit_count"] else None
    out["debit_average"] = round(out["debit_total"] / out["debit_count"], 2) if out["debit_count"] else None

    def ext(col, fn):
        s = N[col].dropna()
        if s.empty:
            return None
        i = fn(s)
        return {"amount": round(float(s.loc[i]), 2), "reference": str(N.loc[i, "ref_raw"])}
    out["credit_highest"], out["credit_lowest"] = ext("credit", lambda s: s.idxmax()), ext("credit", lambda s: s.idxmin())
    out["debit_highest"], out["debit_lowest"] = ext("debit", lambda s: s.idxmax()), ext("debit", lambda s: s.idxmin())
    return out


def _finish(detail, B, L, tol, bank_name, ledger_name, dup_b, dup_l) -> dict:
    cnt = detail["Status"].value_counts()
    summary_rows = []
    for st in ORDER:
        part = detail[detail["Status"] == st]
        if not len(part) and st not in (MATCHED, CREDIT_MISMATCH, DEBIT_MISMATCH, BANK_ONLY, LEDGER_ONLY, DUPLICATE):
            continue
        ba = pd.to_numeric(part["Bank amount"], errors="coerce").sum(min_count=1)   # NaN = this side has no record (not zero)
        la = pd.to_numeric(part["Ledger amount"], errors="coerce").sum(min_count=1)
        summary_rows.append({"Status": st, "Count": int(len(part)), "Amount A": None if pd.isna(ba) else round(float(ba), 2),
                             "Amount B": None if pd.isna(la) else round(float(la), 2),
                             "Difference": round(float(0 if pd.isna(ba) else ba) - float(0 if pd.isna(la) else la), 2)})
    summary = pd.DataFrame(summary_rows)
    problems = detail[~detail["Status"].isin([MATCHED, MANUAL])]
    matched = detail[detail["Status"] == MATCHED]
    manual_rows = detail[detail["Status"] == MANUAL]
    bt, lt = _side_totals(B), _side_totals(L)
    totals = {
        "bank": bt, "ledger": lt,
        "matched_count": int(len(matched)), "manual_count": int(len(manual_rows)),
        "matched_value_bank": round(float(pd.to_numeric(matched["Bank amount"], errors="coerce").sum()), 2),
        "matched_value_ledger": round(float(pd.to_numeric(matched["Ledger amount"], errors="coerce").sum()), 2),
        "unmatched_count": int(len(problems)),
        "bank_only_value": round(float(pd.to_numeric(detail.loc[detail["Status"] == BANK_ONLY, "Bank amount"], errors="coerce").sum()), 2),
        "ledger_only_value": round(float(pd.to_numeric(detail.loc[detail["Status"] == LEDGER_ONLY, "Ledger amount"], errors="coerce").sum()), 2),
        "mismatch_value": round(float(sum(abs(float(x or 0)) for x in detail.loc[detail["Status"].isin([CREDIT_MISMATCH, DEBIT_MISMATCH, BOTH_MISMATCH, DIRECTION, MANUAL]), "Difference (A-B)"])), 2),
        "duplicate_refs": sorted(dup_b | dup_l), "duplicate_refs_bank": sorted(dup_b), "duplicate_refs_ledger": sorted(dup_l),
        "total_discrepancy": _discrepancy_value(detail),
        "tolerance": tol,
    }
    total_rows = max(len(detail), 1)
    score = round(100 * (len(matched) + len(manual_rows)) / total_rows)
    out = {
        "engine": "bank-ledger", "summary": summary, "detail": detail, "totals": totals,
        "plan": {"keys": [{"a": "Reference", "b": "Reference"}], "amount": None, "tolerance": tol, "engine": "bank-ledger"},
        "bank_name": bank_name, "ledger_name": ledger_name, "grouped_rows": 0,
        "how": method_text(tol),
        "exposure": {"mismatch_value": totals["mismatch_value"], "only_in_a_value": totals["bank_only_value"],
                     "only_in_b_value": totals["ledger_only_value"], "total_at_risk": totals["total_discrepancy"]},
        "quality": {"score": score, "grade": "Excellent" if score >= 90 else "Good" if score >= 75 else "Needs attention" if score >= 50 else "Poor",
                    "factors": [{"name": "Matching accuracy", "score": float(score), "max": 100,
                                 "detail": f"{len(matched)} matched exactly and {len(manual_rows)} matched by hand, of {len(detail)} records ({score}%)."}]},
        "warnings": [],
    }
    out["report_text"] = report_text(out)
    return out


def method_text(tol: float) -> str:
    return ("HOW I MATCHED THEM: each bank row is paired with the ledger row that has the same Reference. Credits are compared with "
            "credits and debits with debits. A reference that exists in both files is NOT counted as matched unless the amounts also "
            f"agree to within the tolerance of {tol:g}. A blank amount is treated as missing, not as zero. A reference that appears "
            "more than once in either file is listed for review instead of being paired automatically. Every figure below is "
            "calculated by the application, not by the AI.")


def _n(v) -> str:
    return "-" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{float(v):,.2f}"


def _side_text(name: str, s: dict) -> str:
    t = [f"{name}: {s['rows']} rows. Credits {s['credit_count']} totalling {s['credit_total']:,.2f}; debits {s['debit_count']} totalling {s['debit_total']:,.2f}; "
         f"net movement {s['net_movement']:,.2f} (credits minus debits)."]
    if s["credit_average"] is not None:
        t.append(f"  Average credit {s['credit_average']:,.2f} (over {s['credit_count']} credits); highest {s['credit_highest']['amount']:,.2f} "
                 f"({s['credit_highest']['reference']}), lowest {s['credit_lowest']['amount']:,.2f} ({s['credit_lowest']['reference']}).")
    if s["debit_average"] is not None:
        t.append(f"  Average debit {s['debit_average']:,.2f} (over {s['debit_count']} debits); highest {s['debit_highest']['amount']:,.2f} "
                 f"({s['debit_highest']['reference']}), lowest {s['debit_lowest']['amount']:,.2f} ({s['debit_lowest']['reference']}).")
    return "\n".join(t)


def report_text(out: dict) -> str:
    t, d = out["totals"], out["detail"]
    lines = [f"RECONCILIATION: {out['bank_name']} (bank) vs {out['ledger_name']} (ledger)",
             f"Reporting date: {datetime.now().strftime('%Y-%m-%d')}. Matching tolerance: {t['tolerance']:g}.", "",
             "RESULT", out["summary"].to_string(index=False, na_rep="-", float_format=lambda x: f"{x:,.2f}"), "",
             f"Exact matches: {t['matched_count']}. Matched by hand: {t['manual_count']}. Records needing attention: {t['unmatched_count']}. "
             f"Total absolute discrepancy: {t['total_discrepancy']:,.2f} "
             f"(amount differences {t['mismatch_value']:,.2f} + only in bank {t['bank_only_value']:,.2f} + only in ledger {t['ledger_only_value']:,.2f}).", "",
             "TOTALS (calculated by the application)", _side_text("Bank", t["bank"]), _side_text("Ledger", t["ledger"]), ""]
    if t["duplicate_refs"]:
        lines += [f"DUPLICATE REFERENCES: {', '.join(t['duplicate_refs'])}", ""]
    lines.append("EVERY RECORD THAT DID NOT MATCH")
    prob = d[~d["Status"].isin([MATCHED])]
    if prob.empty:
        lines.append("None. Every record matched.")
    for _, r in prob.iterrows():
        lines.append(f"- {r['Reference']} | {r['Status']} | date {r['Date'] or 'n/a'} | {r['Description'] or 'no description'} | bank {_n(r['Bank amount'])} | "
                     f"ledger {_n(r['Ledger amount'])} | difference {_n(r['Difference (A-B)'])} | {r['Recommended resolution']}"
                     + (f" ({r['Note']})" if r["Note"] else ""))
    lines += ["", "EXACT MATCHES (reference | amount)"]
    lines += [f"- {r['Reference']} | {_n(r['Bank amount'])}" for _, r in d[d["Status"] == MATCHED].iterrows()] or ["None."]
    return "\n".join(lines)


def sheets(out: dict) -> dict:
    """Excel sheets: every section the brief asks for, with every row."""
    d, t = out["detail"], out["totals"]
    cols = ["Reference", "Date", "Description", "Bank amount", "Ledger amount", "Difference (A-B)", "Status", "Recommended resolution", "Note", "Bank row", "Ledger row"]
    cut = lambda *st: d[d["Status"].isin(st)][cols].reset_index(drop=True)  # noqa: E731
    bt, lt = t["bank"], t["ledger"]
    totals = pd.DataFrame([
        ["Bank credits (count)", bt["credit_count"]], ["Bank credits (total)", bt["credit_total"]],
        ["Bank debits (count)", bt["debit_count"]], ["Bank debits (total)", bt["debit_total"]], ["Bank net movement", bt["net_movement"]],
        ["Ledger credits (count)", lt["credit_count"]], ["Ledger credits (total)", lt["credit_total"]],
        ["Ledger debits (count)", lt["debit_count"]], ["Ledger debits (total)", lt["debit_total"]], ["Ledger net movement", lt["net_movement"]],
        ["Matched (count)", t["matched_count"]], ["Matched value (bank)", t["matched_value_bank"]],
        ["Unmatched / needing attention (count)", t["unmatched_count"]],
        ["Amount differences (absolute)", t["mismatch_value"]], ["Only in bank (value)", t["bank_only_value"]],
        ["Only in ledger (value)", t["ledger_only_value"]], ["TOTAL ABSOLUTE DISCREPANCY", t["total_discrepancy"]],
    ], columns=["Item", "Value"], dtype=object)  # object: counts stay whole numbers, money keeps 2 decimals
    method = pd.DataFrame({"Methodology and assumptions": [out["how"], f"Matching tolerance: {t['tolerance']:g}",
                                                           f"Reporting date: {datetime.now().strftime('%Y-%m-%d')}",
                                                           f"Bank file: {out['bank_name']}", f"Ledger file: {out['ledger_name']}"]})
    actions = pd.DataFrame([[s, int(c), RESOLUTION[s]] for s, c in d["Status"].value_counts().items() if s != MATCHED],
                           columns=["Category", "Records", "Recommended corrective action"])
    return {"Executive summary": out["summary"], "Financial totals": totals, "All records": d[cols], "Exact matches": cut(MATCHED), "Manual matches": cut(MANUAL),
            "Amount mismatches": cut(CREDIT_MISMATCH, DEBIT_MISMATCH, BOTH_MISMATCH, DIRECTION), "Only in bank": cut(BANK_ONLY),
            "Only in ledger": cut(LEDGER_ONLY), "Duplicate references": cut(DUPLICATE),
            "Missing data": cut(MISSING_AMT, NO_REF), "Corrective actions": actions, "Methodology": method}
