"""ledger.py - one record for EVERY uploaded row, with its original values and a clear decision.

facts.py asks this module to classify each row of an uploaded table before anything is added up. The rules are general (they
look at the columns that are present, not at any particular file):

  * Every row is either ANALYSED (it counts in the money totals) or EXCLUDED (it does not), and an excluded row always has a
    specific reason. Rows are never silently dropped, and duplicate-looking rows are never deleted.
  * Missing values are found independently for Reference, Date, Type, Amount, Description, Counterparty and Status. A cell that
    holds only spaces counts as missing.
  * A negative amount is never treated as an ordinary positive credit or debit (see TREATMENT below).
  * Status (Completed / Pending / Reversed / ...) is kept apart from direction (credit / debit).

core.py is not edited: core.add_money_columns() has already turned the amount into "always positive" and added Credit/Debit;
this module recovers the ORIGINAL signed amount, type text and date text from the same table so the audit can see what was uploaded.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

import core

COUNTERPARTY_RX = re.compile(r"counter\s*-?party|beneficiar|payee|payer|recipient|sender|merchant|party|customer|vendor|supplier", re.I)
STATUS_RX = re.compile(r"status|state$|outcome", re.I)
_MISSING_TEXT = {"", "nan", "none", "null", "n/a", "na", "-", "--", "nat"}

# How each kind of row is treated in the money totals. Shown in the report so nobody has to guess.
TREATMENT = [
    "Every uploaded row is accounted for: uploaded rows = analysed rows + excluded rows. Nothing is dropped silently.",
    "Analysed rows have a supported type (credit or debit) and a positive amount; they count in the gross totals whatever their status.",
    "Excluded rows (not in any total, each listed with its reason): missing or non-numeric amount, zero amount, negative amount, and missing or unsupported transaction type.",
    "A negative amount is not turned into an ordinary credit or debit: it is excluded and listed so a person can decide what it really is. "
    "(Exception: a file that shows debits as negative numbers throughout is read that way, and nothing is flagged.)",
    "Rows with a missing or invalid date, reference, description, counterparty or status are still analysed when their amount and type are usable; they carry a warning. "
    "A row without a valid date counts in the totals but not in the daily summary.",
    "Pending and Reversed rows are included in the gross totals and are also shown separately. 'Settled' totals count Completed rows only.",
    "Duplicate-looking rows are reported but never removed: they stay in the totals unless they were excluded for another reason.",
]

_STATUS_WORDS = {
    "Completed": {"completed", "complete", "successful", "success", "settled", "posted", "cleared", "approved", "paid", "done"},
    "Pending": {"pending", "processing", "in progress", "awaiting", "initiated", "queued", "on hold", "unsettled"},
    "Reversed": {"reversed", "reversal", "reverse", "rev", "refunded", "returned", "chargeback", "charged back"},
}


def is_blank(s: pd.Series) -> pd.Series:
    """True where a cell is empty, NaN, whitespace only or a placeholder such as '-' or 'N/A'."""
    t = s.astype("object").where(s.notna(), "").astype(str).str.strip().str.lower()
    return t.isin(_MISSING_TEXT)


def norm_status(v) -> str:
    t = re.sub(r"[^a-z ]", "", str(v).lower()).strip()
    if t in _MISSING_TEXT:
        return "(missing)"
    for label, words in _STATUS_WORDS.items():
        if t in words:
            return label
    return str(v).strip().title()


def _raw_text(s: pd.Series, date_fmt: bool = False) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.strftime("%Y-%m-%d %H:%M").str.replace(" 00:00", "", regex=False).fillna("")
    return s.astype("object").where(s.notna(), "").astype(str).str.strip().replace({"nan": "", "NaT": "", "None": ""})


def _type_col(d: pd.DataFrame, helper: set):
    """The text column core.add_money_columns used to read credit/debit from (same test), or None."""
    for c in d.columns:
        if c in helper or pd.api.types.is_numeric_dtype(d[c]) or pd.api.types.is_datetime64_any_dtype(d[c]):
            continue
        vals = d[c].dropna().map(core._norm_word)
        if vals.empty or vals.nunique() > 8:
            continue
        known = vals.isin(core.CREDIT_WORDS | core.DEBIT_WORDS)
        if known.mean() >= 0.8 and vals[known].isin(core.CREDIT_WORDS).any() and vals[known].isin(core.DEBIT_WORDS).any():
            return c
    return None


def _raw_amount(d: pd.DataFrame, amt_abs: pd.Series, helper: set) -> pd.Series:
    """The amount as uploaded (with its sign), found by matching the numeric columns against the 'always positive' helper."""
    raw = pd.Series(np.nan, index=d.index, dtype="float64")
    cands = []
    for c in d.columns:
        if c in helper or not pd.api.types.is_numeric_dtype(d[c]) or pd.api.types.is_bool_dtype(d[c]) or core._NOT_AMOUNT.search(str(c)):
            continue
        v = pd.to_numeric(d[c], errors="coerce")
        m = v.notna() & amt_abs.notna() & ((v.abs() - amt_abs).abs() < 0.005) & (v.abs() > 0)
        cands.append((int(m.sum()), str(c), v, m))
    for _, _, v, m in sorted(cands, key=lambda x: (-x[0], x[1])):
        take = m & raw.isna()
        raw[take] = v[take]
    return raw.where(raw.notna(), amt_abs)


def find_columns(d: pd.DataFrame, skip: set) -> dict:
    out = {"counterparty": None, "status": None}
    for key, rx in (("counterparty", COUNTERPARTY_RX), ("status", STATUS_RX)):
        for c in d.columns:
            if c in skip or c == "_rows" or c in (core.CRED_COL, core.AMT_COL):
                continue
            if rx.search(str(c)):
                out[key] = c
                skip = skip | {c}
                break
    return out


def classify(d: pd.DataFrame, dcol, rcol, ncol, date: pd.Series, src: pd.Series) -> pd.DataFrame:
    """One row per row of d (the table after core.add_money_columns). Index matches d."""
    helper = {core.CRED_COL, core.AMT_COL, "_rows"}
    n = len(d)
    amt_abs = pd.to_numeric(d[core.AMT_COL], errors="coerce")
    direction = d[core.CRED_COL].where(d[core.CRED_COL].isin(["Credit", "Debit"]), None)
    tcol = _type_col(d, helper)
    skip = {c for c in (dcol, rcol, ncol, tcol) if c}
    cols = find_columns(d, skip)
    ccol, scol = cols["counterparty"], cols["status"]
    raw_amt = _raw_amount(d, amt_abs, helper)

    # --- original text values (kept exactly as uploaded, for audit)
    blank_s = pd.Series([""] * n, index=d.index)
    ref_raw = _raw_text(d[rcol]) if rcol else blank_s
    desc_raw = _raw_text(d[ncol]) if ncol else blank_s
    cp_raw = _raw_text(d[ccol]) if ccol else blank_s
    st_raw = _raw_text(d[scol]) if scol else blank_s
    type_raw = _raw_text(d[tcol]) if tcol else direction.fillna("")
    date_dtype = dcol is not None and pd.api.types.is_datetime64_any_dtype(d[dcol])
    if dcol is not None:
        date_raw = _raw_text(d[dcol])
    else:
        date_raw = blank_s
    miss = {"Reference": is_blank(d[rcol]) if rcol else None, "Date": (is_blank(d[dcol]) if dcol else None),
            "Type": (is_blank(d[tcol]) if tcol else None), "Amount": amt_abs.isna(),
            "Description": is_blank(d[ncol]) if ncol else None, "Counterparty": is_blank(d[ccol]) if ccol else None,
            "Status": is_blank(d[scol]) if scol else None}
    if dcol is not None and pd.api.types.is_datetime64_any_dtype(d[dcol]):
        miss["Date"] = d[dcol].isna()
    invalid_date = pd.Series(False, index=d.index)
    if dcol is not None:
        invalid_date = (~miss["Date"]) & date.isna()
        if date_dtype:   # the file reader already turned unreadable text into "no date", so blank and unreadable cannot be told apart
            date_raw = date_raw.where(~miss["Date"], "(blank or unreadable)")

    # --- direction convention: a file that shows debits as negatives throughout is read that way
    signed_file = False
    if tcol is None:
        dr_vals, cr_vals = raw_amt[direction == "Debit"], raw_amt[direction == "Credit"]
        signed_file = bool(len(dr_vals) and (dr_vals < 0).all() and (len(cr_vals) == 0 or (cr_vals > 0).all()))
    negative = (raw_amt < 0) & (not signed_file)
    zero = amt_abs.notna() & (amt_abs == 0)
    no_amount = amt_abs.isna()
    no_dir = direction.isna() & amt_abs.notna() & ~zero

    reasons = [[] for _ in range(n)]
    for i in range(n):
        if no_amount.iloc[i]:
            reasons[i].append("No amount, or the amount is not a number")
        if zero.iloc[i]:
            reasons[i].append("Zero amount")
        if no_dir.iloc[i]:
            if tcol is not None and miss["Type"] is not None and bool(miss["Type"].iloc[i]):
                reasons[i].append("Transaction type is missing")
            elif tcol is not None:
                reasons[i].append(f"Unsupported transaction type '{type_raw.iloc[i]}' (only credit and debit can be added up)")
            else:
                reasons[i].append("No clear credit or debit direction")
        if negative.iloc[i] and amt_abs.notna().iloc[i] and not zero.iloc[i]:
            reasons[i].append(f"Negative amount ({raw_amt.iloc[i]:,.2f}) is not treated as an ordinary {str(direction.iloc[i] or 'credit/debit').lower()}")
    include = pd.Series([not r for r in reasons], index=d.index)

    flags = [[] for _ in range(n)]
    labels = {"Reference": "Missing reference", "Description": "Missing description", "Counterparty": "Missing counterparty", "Status": "Missing status"}
    for i in range(n):
        for k, lab in labels.items():
            if miss[k] is not None and bool(miss[k].iloc[i]):
                flags[i].append(lab)
        if miss["Date"] is not None and bool(miss["Date"].iloc[i]):
            flags[i].append("Missing or unreadable date" if date_dtype else "Missing date")
        elif bool(invalid_date.iloc[i]):
            flags[i].append(f"Invalid date '{date_raw.iloc[i]}'")

    status = st_raw.map(norm_status) if scol else pd.Series([""] * n, index=d.index)
    return pd.DataFrame({
        "Row": pd.to_numeric(src, errors="coerce").to_numpy(),
        "Date": date_raw.to_numpy(), "Reference": ref_raw.to_numpy(), "Description": desc_raw.to_numpy(), "Counterparty": cp_raw.to_numpy(),
        "Type": type_raw.to_numpy(), "AmountRaw": raw_amt.to_numpy(), "AmountAbs": amt_abs.to_numpy(),
        "Status": st_raw.to_numpy(), "StatusNorm": status.to_numpy(), "Direction": direction.to_numpy(dtype=object),
        "Include": include.to_numpy(), "Reasons": ["; ".join(r) for r in reasons], "Flags": ["; ".join(f) for f in flags],
        "MissReference": miss["Reference"].to_numpy() if miss["Reference"] is not None else np.zeros(n, bool),
        "MissDate": miss["Date"].to_numpy() if miss["Date"] is not None else np.zeros(n, bool),
        "MissType": miss["Type"].to_numpy() if miss["Type"] is not None else np.zeros(n, bool),
        "MissAmount": no_amount.to_numpy(),
        "MissDescription": miss["Description"].to_numpy() if miss["Description"] is not None else np.zeros(n, bool),
        "MissCounterparty": miss["Counterparty"].to_numpy() if miss["Counterparty"] is not None else np.zeros(n, bool),
        "MissStatus": miss["Status"].to_numpy() if miss["Status"] is not None else np.zeros(n, bool),
        "InvalidDate": invalid_date.to_numpy(), "Negative": np.asarray(negative & amt_abs.notna() & ~zero), "Zero": zero.to_numpy(),
    }, index=d.index).assign(_cols_type=tcol, _cols_cp=ccol, _cols_status=scol)


def column_found(L: pd.DataFrame) -> dict:
    """Which optional columns exist in the upload (a missing COLUMN is reported differently from missing CELLS)."""
    return {"type": L["_cols_type"].iloc[0] if len(L) else None, "counterparty": L["_cols_cp"].iloc[0] if len(L) else None,
            "status": L["_cols_status"].iloc[0] if len(L) else None}


def row_detail(r) -> dict:
    """The audit view of one uploaded row: exactly what was uploaded."""
    amt = r["AmountRaw"]
    return {"Row": None if pd.isna(r["Row"]) else int(r["Row"]), "Reference": r["Reference"] or "(none)", "Date": r["Date"] or "(none)",
            "Type": r["Type"] or "(none)", "Amount": None if pd.isna(amt) else float(amt), "Counterparty": r["Counterparty"] or "(none)",
            "Status": r["Status"] or "(none)", "Description": r["Description"] or "(none)"}
