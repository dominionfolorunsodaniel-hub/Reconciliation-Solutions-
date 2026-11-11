"""facts.py - ONE verified dataset for a table of transactions.

The calculation engine, the AI wording, the on-screen tables, the Excel file and the PDF all read from the dictionary that
build() returns, so they cannot disagree. Everything here is plain arithmetic on the uploaded rows (money is summed in whole
kobo, as integers, so there is no rounding drift). Before anything is reported, validate() cross-checks the figures against each
other and against an independent recount; the result is carried in F["validation"].

The AI never produces a number that goes into a report. It may only phrase what is in these facts, and check_narrative()
rejects wording that contradicts them.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import logging
import re
from decimal import Decimal

import numpy as np
import pandas as pd

import core

log = logging.getLogger("facts")
NAIRA = "₦"
BLANK_REF = {"", "nan", "none", "-", "null", "n/a"}
MAX_AUDIT_ROWS = 20000          # the slow pattern scan is skipped above this; totals, daily and duplicate checks always run
CURRENT: contextvars.ContextVar = contextvars.ContextVar("verified_facts", default=None)


def _m(v) -> str:
    v = float(v)
    return f"-{NAIRA}{abs(v):,.2f}" if v < 0 else f"{NAIRA}{v:,.2f}"


def _k(values) -> np.ndarray:
    """Money in whole kobo (integers): exact sums, no floating point drift."""
    return np.rint(np.asarray(values, dtype="float64") * 100).astype("int64")


def _kd(k: int) -> float:
    return round(int(k) / 100, 2)


def _fence(s: pd.Series) -> tuple[float, float, float, float]:
    q1, q3 = float(s.quantile(0.25)), float(s.quantile(0.75))
    iqr = q3 - q1
    fence = q3 + 3 * iqr if iqr > 0 else 5 * float(s.median())
    return q1, q3, iqr, fence


# --------------------------------------------------------------------------- #
# 1. read the table into one clean transaction list
# --------------------------------------------------------------------------- #
def _find(df: pd.DataFrame, exact: str, rx, skip=()):
    import deepdive
    if exact in df.columns:
        return exact
    return deepdive._col(df, rx, skip)


def _to_transactions(df: pd.DataFrame) -> tuple[pd.DataFrame | None, dict]:
    import deepdive
    info: dict = {"rows_in_file": int(len(df)), "notes": []}
    d, note = core.add_money_columns(df)
    if core.CRED_COL not in d.columns or core.AMT_COL not in d.columns:
        return None, info
    if note:
        info["notes"].append(note)
    d = d.reset_index(drop=True)
    info["rows_after_split"] = int(len(d))
    dcol = None
    if "Date" in d.columns:
        dcol = "Date"
    else:
        dt_cols = [c for c in d.columns if c != "_rows" and pd.api.types.is_datetime64_any_dtype(d[c])]
        dcol = dt_cols[0] if dt_cols else deepdive._col(d, deepdive.DATE_RX)
    rcol = _find(d, "Reference", deepdive.REF_RX)
    ncol = _find(d, "Narration", deepdive.DESC_RX, skip=(rcol,))
    n = len(d)
    if dcol is not None:
        s = d[dcol]
        if not pd.api.types.is_datetime64_any_dtype(s):
            try:
                dayfirst = bool(core._dayfirst(s.dropna().astype(str).head(300)))
            except Exception:  # noqa: BLE001
                dayfirst = True
            s = pd.to_datetime(s, errors="coerce", dayfirst=dayfirst)
        date = s
    else:
        date = pd.Series(pd.NaT, index=d.index)
    ref = (d[rcol].astype(str).str.strip() if rcol else pd.Series([""] * n, index=d.index))
    ref = ref.where(~ref.str.lower().isin(BLANK_REF), "")
    narr = (d[ncol].astype(str).replace({"nan": "", "None": ""}) if ncol else pd.Series([""] * n, index=d.index))
    typ = d[core.CRED_COL]
    amt = pd.to_numeric(d[core.AMT_COL], errors="coerce")
    src = pd.to_numeric(d["_rows"], errors="coerce") if "_rows" in d.columns else pd.Series(np.arange(2, n + 2), index=d.index)
    ok_dir = typ.isin(["Credit", "Debit"])
    valid = ok_dir & amt.notna() & (amt > 0)
    info.update({"fields": {"date": dcol is not None and bool(date.notna().any()), "reference": rcol is not None, "narration": ncol is not None},
                 "columns": {"date": dcol, "reference": rcol, "narration": ncol},
                 "excluded_no_amount": int(amt.isna().sum()), "excluded_no_direction": int((~ok_dir & amt.notna()).sum()),
                 "excluded_total": int((~valid).sum())})
    T = pd.DataFrame({"Row": src[valid].to_numpy(), "Date": date[valid].to_numpy(), "Reference": ref[valid].to_numpy(),
                      "Narration": narr[valid].to_numpy(), "Type": typ[valid].astype(str).to_numpy(),
                      "Amount": amt[valid].astype(float).round(2).to_numpy()}).reset_index(drop=True)
    T.insert(0, "No.", np.arange(1, len(T) + 1))
    info["rows_analysed"] = int(len(T))
    return T, info


# --------------------------------------------------------------------------- #
# 2. the facts
# --------------------------------------------------------------------------- #
def build(df: pd.DataFrame, audit: dict | None = None, label: str = "", multi_account: bool = False) -> dict | None:
    """The verified dataset, or None when the table is not a list of money in/out (then nothing here applies)."""
    try:
        return _build(df, audit, label, multi_account)
    except Exception:  # noqa: BLE001   a facts problem must never break an analysis
        log.exception("facts.build failed")
        return None


def _row(r, has_time=False) -> dict:
    return {"No.": int(r["No."]), "Row": int(r["Row"]) if pd.notna(r["Row"]) else None,
            "Date": "" if pd.isna(r["Date"]) else r["Date"].strftime("%Y-%m-%d %H:%M" if has_time else "%Y-%m-%d"),
            "Reference": r["Reference"] or "(none)", "Narration": r["Narration"], "Type": r["Type"], "Amount": float(r["Amount"])}


def _build(df, audit, label, multi_account):
    T, info = _to_transactions(df)
    if T is None or T.empty:
        return None
    n = len(T)
    kobo = _k(T["Amount"])
    is_cr = (T["Type"] == "Credit").to_numpy()
    cr_k, dr_k = int(kobo[is_cr].sum()), int(kobo[~is_cr].sum())
    dated_mask = T["Date"].notna().to_numpy()
    has_time = bool(dated_mask.any() and ((T.loc[dated_mask, "Date"].dt.hour != 0).any() or (T.loc[dated_mask, "Date"].dt.minute != 0).any()))

    totals = {"rows": n, "credit_count": int(is_cr.sum()), "debit_count": int((~is_cr).sum()),
              "money_in": _kd(cr_k), "money_out": _kd(dr_k), "net_movement": _kd(cr_k - dr_k),
              "first_date": T["Date"].min().strftime("%Y-%m-%d") if dated_mask.any() else None,
              "last_date": T["Date"].max().strftime("%Y-%m-%d") if dated_mask.any() else None}
    for kind, key in (("Credit", "in"), ("Debit", "out")):
        s = T[T["Type"] == kind]
        totals[f"average_{key}"] = round(float(s["Amount"].mean()), 2) if len(s) else None
        for nm, idx in (("highest", s["Amount"].idxmax() if len(s) else None), ("lowest", s["Amount"].idxmin() if len(s) else None)):
            totals[f"{nm}_{key}"] = None if idx is None else _row(T.loc[idx], has_time)

    # ---- daily summary
    daily, unusual_days = [], []
    if dated_mask.any():
        D = T[dated_mask].assign(Day=T.loc[dated_mask, "Date"].dt.normalize(), K=kobo[dated_mask], Cr=is_cr[dated_mask])
        run = 0
        for day, g in D.groupby("Day", sort=True):
            c_k, d_k = int(g.loc[g["Cr"], "K"].sum()), int(g.loc[~g["Cr"], "K"].sum())
            run += c_k - d_k
            daily.append({"date": f"{day:%Y-%m-%d}", "transactions": int(len(g)), "credit_count": int(g["Cr"].sum()), "debit_count": int((~g["Cr"]).sum()),
                          "credit_total": _kd(c_k), "debit_total": _kd(d_k), "net": _kd(c_k - d_k), "cumulative_net": _kd(run),
                          "gross": _kd(c_k + d_k), "unusual": []})
        if len(daily) >= 8:
            cnt = pd.Series([x["transactions"] for x in daily], dtype=float)
            gro = pd.Series([x["gross"] for x in daily], dtype=float)
            _, _, _, fc = _fence(cnt)
            _, _, _, fg = _fence(gro)
            for x in daily:
                why = []
                if x["transactions"] > fc and x["transactions"] >= 5:
                    why.append(f"{x['transactions']} transactions on this day; a typical day has {cnt.median():g} (flagged above {fc:g})")
                if x["gross"] > fg:
                    why.append(f"{_m(x['gross'])} moved in total; a typical day moves {_m(gro.median())} (flagged above {_m(fg)})")
                if why:
                    x["unusual"] = why
                    top = D[D["Day"] == pd.Timestamp(x["date"])].sort_values("Amount", ascending=False).head(3)
                    unusual_days.append({"date": x["date"], "reasons": why, "largest": [_row(r, has_time) for _, r in top.iterrows()]})
        else:
            pass

    # ---- duplicate references (all rows, independent of the pattern scan)
    refs = T[T["Reference"] != ""]
    dup_groups, same_rows, diff_rows = [], 0, 0
    if len(refs):
        cnts = refs["Reference"].value_counts()
        for ref_, c in cnts[cnts >= 2].items():
            g = refs[refs["Reference"] == ref_]
            same = g["Amount"].nunique() == 1 and g["Type"].nunique() == 1
            same_rows += len(g) if same else 0
            diff_rows += 0 if same else len(g)
            dup_groups.append({"reference": ref_, "count": int(len(g)), "kind": "same_amount_same_direction" if same else "different_amount_or_direction",
                               "total": round(float(g["Amount"].sum()), 2), "rows": [_row(r, has_time) for _, r in g.iterrows()]})
    dup_groups.sort(key=lambda x: (x["kind"] != "same_amount_same_direction", -x["count"], x["reference"]))
    key_cols = ["Date", "Reference", "Narration", "Type", "Amount"]
    ex = T[T.duplicated(subset=key_cols, keep=False)] if dated_mask.any() else T[T.duplicated(subset=key_cols[1:], keep=False)]
    exact_rows = [_row(r, has_time) for _, r in ex.iterrows()]
    duplicates = {"groups": dup_groups, "groups_same": sum(1 for g in dup_groups if g["kind"].startswith("same")),
                  "groups_different": sum(1 for g in dup_groups if not g["kind"].startswith("same")),
                  "rows_same": same_rows, "rows_different": diff_rows, "exact_copies": exact_rows,
                  "references_checked": int(len(refs)), "references_blank": int(n - len(refs))}

    # ---- unusually large transactions (stated method)
    unusual, notes = [], list(info["notes"])
    for kind in ("Credit", "Debit"):
        s = T[T["Type"] == kind]
        if len(s) < 8:
            if len(s):
                notes.append(f"Only {len(s)} {kind.lower()}(s): too few to judge which amounts are unusual (at least 8 are needed).")
            continue
        q1, q3, iqr, fence = _fence(s["Amount"])
        med = float(s["Amount"].median())
        for _, r in s[s["Amount"] > fence].sort_values("Amount", ascending=False).iterrows():
            unusual.append({**_row(r, has_time), "why": f"{_m(r['Amount'])} is above the unusual-range limit of {_m(fence)} for {kind.lower()}s "
                            f"(middle half of {kind.lower()}s: {_m(q1)} to {_m(q3)}; median {_m(med)}); about {r['Amount'] / med:,.1f} times the median." if med > 0 else
                            f"{_m(r['Amount'])} is above the unusual-range limit of {_m(fence)} for {kind.lower()}s."})

    # ---- pattern scan (existing, tested engine) on the dated rows
    patterns, audit_used, aligned = [], None, False
    all_dated = bool(dated_mask.all())
    if not multi_account and dated_mask.any():
        dated = T[dated_mask].reset_index(drop=True)
        if audit is not None and all_dated and (audit.get("totals") or {}).get("rows") == n:
            audit_used, aligned = audit, True
        elif len(dated) <= MAX_AUDIT_ROWS:
            try:
                import txaudit
                x = pd.DataFrame({"Date": dated["Date"], "Reference": dated["Reference"], "Narration": dated["Narration"],
                                  core.CRED_COL: dated["Type"], core.AMT_COL: dated["Amount"]})
                audit_used = txaudit.audit(x)
                aligned = True
            except Exception:  # noqa: BLE001
                log.exception("pattern scan failed")
        else:
            notes.append(f"The file has {len(dated):,} dated rows, so the slower pattern scan was skipped. Totals, daily activity and duplicate checks were still run on every row.")
        if audit_used is not None:
            dated_no = dated["No."].to_numpy()
            for f_ in audit_used["findings"]:
                if f_["kind"].startswith("duplicate_reference") or f_["kind"].startswith("unusual_"):
                    continue                    # shown from the all-rows checks above
                rows = []
                for r in f_["rows"]:
                    rr = dict(r)
                    rr["No."] = int(dated_no[r["No."] - 1]) if aligned else r["No."]
                    rows.append(rr)
                patterns.append({**{k: f_[k] for k in ("kind", "severity", "title", "why", "method", "count", "value")}, "rows": rows})
            for nt in audit_used.get("notes", []):
                if "no reference" in nt.lower() or "duplicate-reference" in nt.lower() or "too few" in nt.lower():
                    continue
                notes.append(nt)
    elif multi_account:
        notes.append("Several accounts were combined, so patterns are checked per account in the investigation section; the totals, daily activity and duplicate checks below cover the combined list.")

    # ---- data quality
    today = pd.Timestamp(dt.date.today())
    quality = []
    ex_t = info["excluded_total"]
    quality.append({"check": "Rows in the file", "status": "ok", "detail": f"{info['rows_in_file']:,} row(s) read" + (f"; {info['rows_after_split'] - info['rows_in_file']:,} extra row(s) created because a row held both a credit and a debit" if info["rows_after_split"] != info["rows_in_file"] else "") + "."})
    quality.append({"check": "Rows analysed", "status": "ok" if ex_t == 0 else "warn",
                    "detail": f"{n:,} of {info['rows_after_split']:,} row(s) are real money movements." + ("" if ex_t == 0 else
                              f" {ex_t:,} row(s) were left out: {info['excluded_no_amount']:,} with no amount, {info['excluded_no_direction']:,} with no clear credit/debit direction or a zero amount.")})
    if info["fields"]["date"]:
        miss = int((~dated_mask).sum())
        fut = int((T["Date"] > today + pd.Timedelta(days=1)).sum())
        quality.append({"check": "Dates", "status": "ok" if miss == 0 and fut == 0 else "warn",
                        "detail": f"{n - miss:,} of {n:,} transaction(s) have a valid date ({totals['first_date']} to {totals['last_date']})." + (f" {miss:,} have no usable date, so they count in the totals but not in the daily summary." if miss else "") + (f" {fut:,} are dated in the future." if fut else "")})
    else:
        quality.append({"check": "Dates", "status": "warn", "detail": "No usable date column was found, so the daily summary could not be produced."})
    if info["fields"]["reference"]:
        b = duplicates["references_blank"]
        quality.append({"check": "References", "status": "ok" if b == 0 else "warn",
                        "detail": f"{n - b:,} of {n:,} transaction(s) have a reference." + (f" {b:,} ({b / n * 100:.1f}%) are blank, so they cannot be checked for duplicate references." if b else "")})
    else:
        quality.append({"check": "References", "status": "warn", "detail": "No reference column was found, so duplicate-reference checks could not be run."})
    if info["fields"]["narration"]:
        bn = int((T["Narration"].str.strip() == "").sum())
        quality.append({"check": "Descriptions", "status": "ok" if bn == 0 else "info", "detail": f"{n - bn:,} of {n:,} transaction(s) have a description" + (f"; {bn:,} are blank." if bn else ".")})
    quality.append({"check": "Exact duplicate rows", "status": "ok" if not exact_rows else "warn",
                    "detail": "No row is an exact copy of another." if not exact_rows else f"{len(exact_rows):,} row(s) are exact copies of another row (same date, reference, description, direction and amount)."})
    quality.append({"check": "Unusual amounts", "status": "ok" if not unusual else "info", "detail": f"{len(unusual):,} transaction(s) are far above the normal range of their direction."})

    F = {"label": label, "fields": info["fields"], "columns": info["columns"], "totals": totals, "daily": daily, "unusual_days": unusual_days,
         "duplicates": duplicates, "unusual": unusual, "patterns": patterns, "quality": quality, "notes": notes,
         "rows_in_file": info["rows_in_file"], "rows_analysed": n, "has_time": has_time}
    F["validation"] = _validate(F, T, kobo, is_cr, dated_mask, audit_used if aligned and all_dated else None, info)
    F["summary"] = _summary(F)
    F["recommendations"] = _recommendations(F)
    return F


# --------------------------------------------------------------------------- #
# 3. validation layer: the facts are checked against each other and against an independent recount
# --------------------------------------------------------------------------- #
def _validate(F, T, kobo, is_cr, dated_mask, audit, info) -> dict:
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    t = F["totals"]
    # independent recount with exact decimals (a different route to the same totals)
    dc = sum((Decimal(str(round(a, 2))) for a, c in zip(T["Amount"], is_cr) if c), Decimal(0))
    dd = sum((Decimal(str(round(a, 2))) for a, c in zip(T["Amount"], is_cr) if not c), Decimal(0))
    add("Credits recounted with exact decimals", abs(dc - Decimal(str(t["money_in"]))) < Decimal("0.005"), f"{_m(dc)} recounted vs {_m(t['money_in'])} reported")
    add("Debits recounted with exact decimals", abs(dd - Decimal(str(t["money_out"]))) < Decimal("0.005"), f"{_m(dd)} recounted vs {_m(t['money_out'])} reported")
    add("Net movement = credits - debits", abs(round(t["money_in"] - t["money_out"], 2) - t["net_movement"]) < 0.005, f"{_m(t['money_in'])} - {_m(t['money_out'])} = {_m(t['net_movement'])}")
    add("Credit count + debit count = rows analysed", t["credit_count"] + t["debit_count"] == t["rows"] == F["rows_analysed"], f"{t['credit_count']:,} + {t['debit_count']:,} = {t['rows']:,}")
    add("Every row in the file is accounted for", info["rows_analysed"] + info["excluded_total"] == info["rows_after_split"],
        f"{info['rows_analysed']:,} analysed + {info['excluded_total']:,} left out = {info['rows_after_split']:,}")
    und = int((~dated_mask).sum())
    if F["daily"] or und == len(T):
        dk_c = sum(_k(x["credit_total"]).item() for x in F["daily"]) if F["daily"] else 0
        dk_d = sum(_k(x["debit_total"]).item() for x in F["daily"]) if F["daily"] else 0
        uc = int(kobo[(~dated_mask) & is_cr].sum()); ud = int(kobo[(~dated_mask) & ~is_cr].sum())
        add("Daily credits add up to the total credits", dk_c + uc == int(kobo[is_cr].sum()), f"{_m(dk_c / 100)} over {len(F['daily']):,} day(s)" + (f" + {_m(uc / 100)} on undated rows" if und else "") + f" vs {_m(t['money_in'])}")
        add("Daily debits add up to the total debits", dk_d + ud == int(kobo[~is_cr].sum()), f"{_m(dk_d / 100)} over {len(F['daily']):,} day(s)" + (f" + {_m(ud / 100)} on undated rows" if und else "") + f" vs {_m(t['money_out'])}")
        add("Daily transaction counts add up to the row count", sum(x["transactions"] for x in F["daily"]) + und == t["rows"], f"{sum(x['transactions'] for x in F['daily']):,} dated + {und:,} undated = {t['rows']:,}")
        if F["daily"]:
            add("Cumulative net on the last day equals the dated net movement", abs(F["daily"][-1]["cumulative_net"] - _kd(int(kobo[dated_mask & is_cr].sum()) - int(kobo[dated_mask & ~is_cr].sum()))) < 0.005, f"{_m(F['daily'][-1]['cumulative_net'])}")
    # duplicates recounted a second way
    refs = T[T["Reference"] != ""]
    again = int((refs["Reference"].value_counts() >= 2).sum())
    add("Duplicate references recounted", again == len(F["duplicates"]["groups"]), f"{again:,} repeated reference(s) found both ways")
    # every listed transaction really is that transaction
    bad = 0
    listed = [r for g in F["duplicates"]["groups"] for r in g["rows"]] + list(F["unusual"]) + [r for p in F["patterns"] for r in p["rows"]]
    for r in listed:
        src = T.iloc[r["No."] - 1] if 1 <= r["No."] <= len(T) else None
        ref_ = r["Reference"] if r["Reference"] != "(none)" else ""
        if src is None or abs(float(src["Amount"]) - float(r["Amount"])) > 0.005 or src["Type"] != r["Type"] or src["Reference"] != ref_:
            bad += 1
    add("Every listed transaction matches the uploaded row", bad == 0, f"{len(listed):,} listed row(s) checked against the file; {bad:,} mismatch(es)")
    if audit is not None:
        ad_dup = {r["No."] for f_ in audit["findings"] if f_["kind"].startswith("duplicate_reference") for r in f_["rows"]}
        mine = {r["No."] for g in F["duplicates"]["groups"] for r in g["rows"]}
        add("Duplicate references agree with the pattern scan", ad_dup == mine or (len(ad_dup) == 0 and len(mine) == 0), f"{len(mine):,} row(s) both ways" if ad_dup == mine else f"{len(mine):,} vs {len(ad_dup):,}")
        ad_un = sum(f_["count"] for f_ in audit["findings"] if f_["kind"].startswith("unusual_"))
        add("Unusual transactions agree with the pattern scan", ad_un == len(F["unusual"]), f"{len(F['unusual']):,} vs {ad_un:,}")
        at = audit.get("totals") or {}
        add("Totals agree with the pattern scan", abs(at.get("money_in", 0) - t["money_in"]) < 0.005 and abs(at.get("money_out", 0) - t["money_out"]) < 0.005, f"{_m(at.get('money_in', 0))} in / {_m(at.get('money_out', 0))} out")
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "passed": sum(c["ok"] for c in checks), "total": len(checks), "checks": checks}


# --------------------------------------------------------------------------- #
# 4. words made from the facts (no AI)
# --------------------------------------------------------------------------- #
def _summary(F) -> list[str]:
    t, d, v = F["totals"], F["duplicates"], F["validation"]
    out = []
    per = f" from {t['first_date']} to {t['last_date']}" if t["first_date"] else ""
    out.append(f"{t['rows']:,} transaction(s) were analysed{per}: {t['credit_count']:,} credit(s) and {t['debit_count']:,} debit(s).")
    out.append(f"Money in {_m(t['money_in'])}, money out {_m(t['money_out'])}, net movement {_m(t['net_movement'])} "
               f"({'more came in than went out' if t['net_movement'] > 0 else 'more went out than came in' if t['net_movement'] < 0 else 'inflows and outflows are equal'}). "
               "This is cash movement only; by itself it does not show the financial health of the account holder.")
    if F["daily"]:
        busiest = max(F["daily"], key=lambda x: x["transactions"])
        biggest = max(F["daily"], key=lambda x: x["gross"])
        out.append(f"{len(F['daily']):,} active day(s). Busiest day {busiest['date']} with {busiest['transactions']:,} transaction(s); largest total movement {biggest['date']} ({_m(biggest['gross'])}).")
    if F["unusual_days"]:
        out.append(f"{len(F['unusual_days']):,} day(s) show unusual activity: " + ", ".join(u["date"] for u in F["unusual_days"][:8]) + (" ..." if len(F["unusual_days"]) > 8 else "") + ".")
    if F["fields"]["reference"]:
        if d["groups"]:
            out.append(f"{len(d['groups']):,} reference(s) appear more than once: {d['groups_same']:,} with an identical amount and direction (most likely posted twice) and {d['groups_different']:,} with different amounts or directions.")
        else:
            out.append(f"No reference is repeated among the {d['references_checked']:,} transaction(s) that have one.")
    else:
        out.append("No reference column was found, so duplicate references could not be checked.")
    if F["unusual"]:
        out.append(f"{len(F['unusual']):,} transaction(s) are unusually large for their direction.")
    warn = [q for q in F["quality"] if q["status"] == "warn"]
    if warn:
        out.append("Data quality: " + " ".join(q["detail"].split(". ")[0].rstrip(".") + "." for q in warn[:3]))
    out.append(f"Verification: {v['passed']} of {v['total']} cross-checks passed." + ("" if v["ok"] else " Some checks FAILED: do not rely on these figures until the cause is found."))
    return out


def _recommendations(F) -> list[str]:
    d, rec = F["duplicates"], []
    same = [g for g in d["groups"] if g["kind"].startswith("same")]
    diff = [g for g in d["groups"] if not g["kind"].startswith("same")]
    if same:
        rec.append(f"Confirm with the bank or source system whether these {len(same):,} reference(s) were really posted twice (identical amount and direction): " + ", ".join(g["reference"] for g in same[:6]) + (" ..." if len(same) > 6 else "") + ". Reverse any real double postings.")
    if diff:
        rec.append(f"Check that the {len(diff):,} reference(s) used with different amounts or directions are genuine reversals or part-payments, not reused or mistyped references: " + ", ".join(g["reference"] for g in diff[:6]) + (" ..." if len(diff) > 6 else "") + ".")
    if F["unusual"]:
        top = F["unusual"][0]
        rec.append(f"Obtain supporting documents for the {len(F['unusual']):,} unusually large transaction(s), starting with the largest ({_m(top['Amount'])}, ref {top['Reference']}, {top['Date']}).")
    if F["unusual_days"]:
        rec.append("Review what happened on " + ", ".join(u["date"] for u in F["unusual_days"][:6]) + (" ..." if len(F["unusual_days"]) > 6 else "") + ": activity was far above a typical day.")
    hi = [p for p in F["patterns"] if p["severity"] in ("high", "medium")]
    if hi:
        rec.append("Look at the pattern finding(s): " + "; ".join(p["title"] for p in hi[:4]) + ". Each lists the exact rows.")
    if not F["fields"]["reference"]:
        rec.append("Export the statement or ledger with a reference column so duplicate references can be checked.")
    elif d["references_blank"]:
        rec.append(f"Ask for the {d['references_blank']:,} missing reference(s) to be filled in; those rows cannot be checked for duplicates.")
    if not F["fields"]["date"] or any(q["check"] == "Dates" and q["status"] == "warn" for q in F["quality"]):
        rec.append("Correct the missing or unreadable dates so every transaction appears in the daily summary.")
    if F["quality"] and any(q["check"] == "Rows analysed" and q["status"] == "warn" for q in F["quality"]):
        rec.append("Look at the rows that were left out (no amount, no direction or zero amount) to make sure none is a real transaction.")
    if not F["validation"]["ok"]:
        rec.append("Some verification checks failed. Do not rely on these figures until the failed checks are explained.")
    rec.append("This report describes one file only. To find transactions that are missing or do not match, upload the second file (for example the ledger or bank statement) in Reconciliation.")
    return rec


def analysis_text(F, sections=("summary", "daily", "duplicates", "unusual", "quality", "validation", "recommendations")) -> str:
    L = ["VERIFIED ANALYSIS OF THE WHOLE FILE (every figure is calculated by the application from your rows; the AI only words it)"]
    if "summary" in sections:
        L += ["", "SUMMARY"] + [f"- {s}" for s in F["summary"]]
    if "daily" in sections and F["daily"]:
        L += ["", f"DAILY ACTIVITY ({len(F['daily']):,} day(s))", "Date | Transactions | Credits | Debits | Credit total | Debit total | Net | Note"]
        for x in F["daily"]:
            L.append(f"{x['date']} | {x['transactions']:,} | {x['credit_count']:,} | {x['debit_count']:,} | {_m(x['credit_total'])} | {_m(x['debit_total'])} | {_m(x['net'])}" + (" | UNUSUAL" if x["unusual"] else ""))
        for u in F["unusual_days"]:
            L.append(f"  Unusual day {u['date']}: " + " ".join(u["reasons"]))
            for r in u["largest"]:
                L.append(f"    - No.{r['No.']} | ref {r['Reference']} | {r['Type']} {_m(r['Amount'])} | {r['Narration'][:50] or 'no narration'}")
    if "duplicates" in sections:
        d = F["duplicates"]
        L += ["", "DUPLICATE REFERENCES"]
        if not F["fields"]["reference"]:
            L.append("- No reference column was found, so this check could not be run.")
        elif not d["groups"]:
            L.append(f"- No reference is repeated among the {d['references_checked']:,} transaction(s) that have one.")
        for g in d["groups"][:100]:
            L.append(f"- Reference {g['reference']}: {g['count']} rows, total {_m(g['total'])} ({'identical amount and direction' if g['kind'].startswith('same') else 'amounts or directions differ'})")
            for r in g["rows"]:
                L.append(f"    No.{r['No.']} | {r['Date']} | {r['Type']} {_m(r['Amount'])} | {r['Narration'][:50] or 'no narration'}")
        if len(d["groups"]) > 100:
            L.append(f"  ...and {len(d['groups']) - 100:,} more (all are in the Excel file).")
    if "unusual" in sections and F["unusual"]:
        L += ["", "UNUSUAL TRANSACTIONS"]
        for r in F["unusual"][:100]:
            L.append(f"- No.{r['No.']} | {r['Date']} | ref {r['Reference']} | {r['Type']} {_m(r['Amount'])} | {r['Narration'][:40] or 'no narration'}: {r['why']}")
    if "quality" in sections:
        L += ["", "DATA QUALITY"] + [f"- {q['check']}: {q['detail']}" for q in F["quality"]] + [f"- {n}" for n in F["notes"]]
    if "validation" in sections:
        v = F["validation"]
        L += ["", f"VALIDATION: {v['passed']} of {v['total']} cross-checks passed"] + [f"- {'PASS' if c['ok'] else 'FAIL'}: {c['check']} ({c['detail']})" for c in v["checks"]]
    if "recommendations" in sections:
        L += ["", "RECOMMENDATIONS"] + [f"- {r}" for r in F["recommendations"]]
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# 5. the AI may only word these facts
# --------------------------------------------------------------------------- #
def prompt_block(F, max_chars: int = 3800) -> str:
    t, d = F["totals"], F["duplicates"]
    L = ["VERIFIED FACTS about the WHOLE uploaded file (exact, computed by code).",
         "RULES: use these numbers exactly as written; never recalculate or contradict them; never say a field is missing or unavailable if it is listed as present below; "
         "when you mention a duplicate, unusual transaction or unusual day, quote its exact reference or date from this list; if something is not in these facts or the result table, say you cannot confirm it.",
         f"Fields present: date={'yes' if F['fields']['date'] else 'NO'}, reference={'yes' if F['fields']['reference'] else 'NO'}, description={'yes' if F['fields']['narration'] else 'NO'}.",
         f"Rows analysed {t['rows']:,}; period {t['first_date']} to {t['last_date']}; credits {t['credit_count']:,} = {_m(t['money_in'])}; debits {t['debit_count']:,} = {_m(t['money_out'])}; net movement {_m(t['net_movement'])}.",
         f"Duplicate references: {len(d['groups']):,} ({d['groups_same']:,} identical amount+direction, {d['groups_different']:,} different). "
         + "; ".join(f"{g['reference']} x{g['count']} ({_m(g['total'])})" for g in d["groups"][:12]),
         f"Unusual transactions: {len(F['unusual']):,}. " + "; ".join(f"ref {r['Reference']} {_m(r['Amount'])} on {r['Date']}" for r in F["unusual"][:8]),
         f"Unusual days: {len(F['unusual_days']):,}. " + "; ".join(u["date"] for u in F["unusual_days"][:8]),
         "Data quality: " + " | ".join(f"{q['check']}: {q['detail']}" for q in F["quality"] if q["status"] != "ok")[:700],
         f"Validation: {F['validation']['passed']} of {F['validation']['total']} cross-checks passed."]
    return "\n".join(L)[:max_chars]


_CLAIM_UNAVAILABLE = {
    "reference": re.compile(r"(\bno\b|without|lacks?|missing|unavailable|not available|does not (contain|have|include)|doesn'?t (contain|have|include)|cannot find|could not find|isn'?t (any|a)|there is no)\W+(?:\w+\W+){0,3}?references?(\s+(column|field|data|information|numbers?))\b|references?\s+(column|field|data|information)\s+(is|are)?\s*(not available|unavailable|missing|absent|not present)|unable to (check|identify|detect|find)\W+(?:\w+\W+){0,4}duplicate", re.I),
    "date": re.compile(r"(\bno\b|without|lacks?|missing|unavailable|not available|does not (contain|have|include)|doesn'?t (contain|have|include)|there is no)\W+(?:\w+\W+){0,3}?dates?(\s+(column|field|data|information))\b|dates?\s+(column|field|data|information)\s+(is|are)?\s*(not available|unavailable|missing|absent|not present)|cannot (produce|do|create) (a )?daily", re.I),
}
_NET_OUT = re.compile(r"net (outflow|deficit|loss)|more (money )?(went|was|is) out|spent more than|outflows? (exceed|were higher|are higher)", re.I)
_NET_IN = re.compile(r"net (inflow|surplus|gain)|more (money )?(came|was|is) in|earned more than|inflows? (exceed|were higher|are higher)", re.I)
_NO_DUP = re.compile(r"\bno (duplicate|repeated|repeating)|duplicates? (were|was|are|is) not (found|detected|present)|did not (find|detect) any duplicate|not (found|detected) any duplicate", re.I)


def check_narrative(text: str, F: dict, whole_file: bool) -> list[str]:
    """Wording that contradicts the verified facts. Statements about a SUBSET of rows are only checked for facts that hold for any subset."""
    t = text or ""
    issues = []
    for key, rx in _CLAIM_UNAVAILABLE.items():
        if F["fields"].get(key) and rx.search(t):
            issues.append(f"said the {key} information is unavailable, but the file has it")
    if whole_file:
        net = F["totals"]["net_movement"]
        if net > 0 and _NET_OUT.search(t):
            issues.append("described a net outflow, but money in is higher than money out")
        if net < 0 and _NET_IN.search(t):
            issues.append("described a net inflow, but money out is higher than money in")
        if F["duplicates"]["groups"] and _NO_DUP.search(t):
            issues.append(f"said there are no duplicates, but {len(F['duplicates']['groups']):,} reference(s) repeat")
    return issues


def install_grounding() -> None:
    """Give the AI the verified facts and let its answer quote them, WITHOUT editing core.py (wraps two functions, safely)."""
    if getattr(core, "_facts_installed", False):
        return
    orig_sum, orig_unv = core.summarize, core.unverified_figures

    async def summarize(*a, **k):
        F = CURRENT.get()
        if F and len(a) >= 3 and isinstance(a[2], str):
            a = (a[0], a[1], a[2] + "\n\n" + prompt_block(F)) + tuple(a[3:])
        return await orig_sum(*a, **k)

    def unverified(text, allowed_text, *a, **k):
        F = CURRENT.get()
        if F:
            allowed_text = f"{allowed_text} {prompt_block(F, 20000)} {analysis_text(F)}"
        return orig_unv(text, allowed_text, *a, **k)

    core.summarize, core.unverified_figures, core._facts_installed = summarize, unverified, True


# --------------------------------------------------------------------------- #
# 6. outputs
# --------------------------------------------------------------------------- #
def tables(F) -> dict:
    """Every table in the facts, as DataFrames (Excel sheets and PDF sections use the same ones)."""
    out = {}
    out["Verified summary"] = pd.DataFrame({"Summary": F["summary"] + [""] + ["Recommendation: " + r for r in F["recommendations"]]})
    if F["daily"]:
        out["Daily activity"] = pd.DataFrame([{"Date": x["date"], "Transactions": x["transactions"], "Credits": x["credit_count"], "Debits": x["debit_count"],
                                               "Credit total": x["credit_total"], "Debit total": x["debit_total"], "Net": x["net"],
                                               "Cumulative net": x["cumulative_net"], "Unusual": "; ".join(x["unusual"])} for x in F["daily"]])
    rows = [{"Reference": g["reference"], "Kind": "Identical amount and direction" if g["kind"].startswith("same") else "Amounts or directions differ", **{k: r[k] for k in ("No.", "Date", "Type", "Amount", "Narration")}}
            for g in F["duplicates"]["groups"] for r in g["rows"]]
    out["Duplicate references"] = pd.DataFrame(rows, columns=["Reference", "Kind", "No.", "Date", "Type", "Amount", "Narration"])
    out["Unusual transactions"] = pd.DataFrame([{k: r[k] for k in ("No.", "Date", "Reference", "Type", "Amount", "Narration", "why")} for r in F["unusual"]],
                                               columns=["No.", "Date", "Reference", "Type", "Amount", "Narration", "why"]).rename(columns={"why": "Why it stands out"})
    out["Data quality"] = pd.DataFrame([{"Check": q["check"], "Status": q["status"], "Detail": q["detail"]} for q in F["quality"]] + [{"Check": "Note", "Status": "info", "Detail": n} for n in F["notes"]])
    out["Validation"] = pd.DataFrame([{"Check": c["check"], "Result": "PASS" if c["ok"] else "FAIL", "Detail": c["detail"]} for c in F["validation"]["checks"]])
    return out


def compact(F) -> dict:
    """What the browser needs (small): verdict, checks, and the daily table."""
    v = F["validation"]
    return {"ok": v["ok"], "passed": v["passed"], "total": v["total"], "checks": v["checks"], "summary": F["summary"],
            "recommendations": F["recommendations"], "daily": F["daily"][:400], "unusual_days": F["unusual_days"],
            "duplicate_groups": len(F["duplicates"]["groups"]), "unusual_count": len(F["unusual"])}


def add_sheets(xlsx_bytes: bytes, F: dict) -> bytes:
    """Add the verified sheets to an Excel file that already exists. On any problem, return the file unchanged."""
    import io
    try:
        buf = io.BytesIO(xlsx_bytes)
        with pd.ExcelWriter(buf, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
            for name, df in tables(F).items():
                df.to_excel(w, sheet_name=name[:31], index=False)
                ws = w.sheets[name[:31]]
                for col in ws.columns:
                    width = min(max(len(str(c.value)) if c.value is not None else 0 for c in col[:200]) + 2, 70)
                    ws.column_dimensions[col[0].column_letter].width = max(width, 10)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        log.exception("could not add verified sheets")
        return xlsx_bytes
