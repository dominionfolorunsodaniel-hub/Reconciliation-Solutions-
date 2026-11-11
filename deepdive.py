"""A detailed, deterministic 'DETAILED ANALYSIS' section added to every Ask / Analytics answer.
All arithmetic here is plain pandas. It lists exact rows, states its method and says what the data cannot show."""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

REF_RX = re.compile(r"ref|rrn|stan|txn|trx|trans(action)?\s*(id|no)|session|\bid$|invoice|order", re.I)
DATE_RX = re.compile(r"date|time|posted|day", re.I)
DESC_RX = re.compile(r"narrat|descri|particular|detail|remark|memo|merchant|payee|name", re.I)
MONEY_RX = re.compile(r"amount|amt|value|total|credit|debit|sum|price|cost|balance", re.I)
MAX_LIST = 100


def _col(d, rx, skip=()):
    for c in d.columns:
        if c not in skip and c != "_rows" and rx.search(str(c)):
            return c
    return None


def _money_col(d):
    import core
    if core.AMT_COL in d.columns:
        return core.AMT_COL
    for c in d.columns:
        if c != "_rows" and pd.api.types.is_numeric_dtype(d[c]) and MONEY_RX.search(str(c)):
            return c
    for c in d.columns:
        if c != "_rows" and pd.api.types.is_numeric_dtype(d[c]) and not pd.api.types.is_bool_dtype(d[c]):
            return c
    return None


def _n(v) -> str:
    return f"{float(v):,.2f}"


def _row(r, ref, date, desc, amt) -> str:
    bits = []
    if date and pd.notna(r[date]):
        v = r[date]
        bits.append(f"{v:%Y-%m-%d}" if hasattr(v, "strftime") else str(v)[:19])
    if ref and str(r[ref]).strip() not in ("", "nan"):
        bits.append(f"ref {r[ref]}")
    if desc and str(r[desc]).strip() not in ("", "nan"):
        bits.append(str(r[desc])[:40])
    bits.append(_n(r[amt]))
    return " | ".join(bits)


def detail(out: dict, total_rows: int | None = None, whole_file_verified: bool = False) -> str:
    try:
        return _detail(out, total_rows, whole_file_verified)
    except Exception:  # noqa: BLE001  never break an answer
        return ""


def _detail(out: dict, total_rows, whole_file_verified: bool = False) -> str:
    res, keys, metric, f = out.get("res"), out.get("keys") or [], out.get("metric"), out.get("filtered")
    if f is None or res is None:
        return ""
    f = f.drop(columns=["_rows"], errors="ignore")
    L = ["DETAILED ANALYSIS (calculated by the application from your file, not estimated by the AI)"]
    scope = f"Rows used for this answer: {len(f):,}" + (f" of {total_rows:,} in the file" if total_rows else "") + "."
    L.append(scope)

    # 1. the result, group by group
    if keys and metric in res.columns and pd.api.types.is_numeric_dtype(res[metric]) and len(res) >= 1:
        v = res[metric].astype(float)
        tot = float(v.sum())
        L += ["", f"BREAKDOWN OF THE RESULT ({len(res):,} group(s)):",
              f"- Total {metric}: {_n(tot)}; average per group {_n(v.mean())} (over {len(v):,} groups); median {_n(v.median())}.",
              f"- Highest: {' | '.join(str(res.loc[v.idxmax(), k]) for k in keys)} = {_n(v.max())}" + (f" ({v.max() / tot * 100:.1f}% of the total)" if tot else "") + ".",
              f"- Lowest: {' | '.join(str(res.loc[v.idxmin(), k]) for k in keys)} = {_n(v.min())}" + (f" ({v.min() / tot * 100:.1f}% of the total)" if tot else "") + "."]
        order = v.sort_values(ascending=False)
        if len(order) >= 3 and tot:
            top3 = float(order.head(3).sum()) / tot * 100
            L.append(f"- The top 3 groups make up {top3:.1f}% of the total" + (" - the result is concentrated in a few groups." if top3 >= 70 and len(order) > 5 else "."))
        show = order if len(order) <= 25 else pd.concat([order.head(10), order.tail(5)])
        L.append("- Ranking (value, share of total):" if len(order) <= 25 else f"- Top 10 and bottom 5 of {len(order)} groups (the full list is in the table and the Excel file):")
        for rank_pos, (i, val) in enumerate(show.items(), 1):
            share = f"{val / tot * 100:.1f}%" if tot else "n/a"
            L.append(f"   {rank_pos}. {' | '.join(str(res.loc[i, k]) for k in keys)}: {_n(val)} ({share})")

    if whole_file_verified:      # totals, duplicates, unusual values and data quality come from the verified whole-file facts instead
        L += ["", "Totals, duplicate references, unusual transactions and data-quality checks for the whole file are in the verified analysis below."]
        return "\n".join(L)
    # 2. the underlying rows
    amt, ref = _money_col(f), _col(f, REF_RX)
    date, desc = _col(f, DATE_RX), _col(f, DESC_RX, skip=(ref,))
    if amt is not None and len(f):
        s = pd.to_numeric(f[amt], errors="coerce")
        blank, neg, zero = int(s.isna().sum()), int((s < 0).sum()), int((s == 0).sum())
        L += ["", f"CHECKS ON THE UNDERLYING ROWS (column used: {amt}):",
              f"- {len(s) - blank:,} filled, {blank:,} blank (blank is not counted as zero), {neg:,} negative, {zero:,} exactly zero.",
              f"- Total {_n(s.sum())}; average {_n(s.mean())} over the {len(s) - blank:,} filled values; lowest {_n(s.min())}; highest {_n(s.max())}."]
        g = f.assign(_v=s)
        if len(s.dropna()) >= 8:
            q1, q3 = float(s.quantile(.25)), float(s.quantile(.75))
            iqr = q3 - q1
            fence = q3 + 3 * iqr if iqr > 0 else 5 * float(s.median())
            hi = g[g["_v"] > fence].sort_values("_v", ascending=False)
            L.append(f"- Unusually high values (method: above Q3 + 3 x IQR = {_n(fence)}; Q1 {_n(q1)}, Q3 {_n(q3)}): "
                     + (f"{len(hi):,} row(s)." if len(hi) else "none."))
            for _, r in hi.head(MAX_LIST).iterrows():
                L.append("   - " + _row(r, ref, date, desc, "_v"))
            if len(hi) > MAX_LIST:
                L.append(f"   ...and {len(hi) - MAX_LIST} more (all of them are in the Excel file).")
        else:
            L.append(f"- Too few values ({len(s.dropna())}) to say which are unusually high or low (at least 8 are needed).")
        rep = g[g["_v"].notna() & (g["_v"] >= 100)].groupby("_v").size()
        rep = rep[rep >= 3].sort_values(ascending=False)
        if len(rep):
            L.append(f"- Amounts that repeat 3 or more times: {len(rep):,} different amount(s).")
            for val, cnt in rep.head(10).items():
                refs = g.loc[g["_v"] == val, ref].astype(str).head(6).tolist() if ref else []
                L.append(f"   - {_n(val)} appears {cnt} times" + (f" (refs {', '.join(refs)}{', ...' if cnt > 6 else ''})" if refs else ""))
    if ref is not None:
        r = f[ref].astype(str).str.strip()
        ok = r[~r.str.lower().isin(["", "nan", "none", "-"])]
        dup = ok[ok.duplicated(keep=False)]
        L.append("")
        L.append(f"DUPLICATE CHECK ON {ref}: " + (f"{dup.nunique():,} reference(s) appear more than once, covering {len(dup):,} rows." if len(dup) else "no reference is repeated."))
        for k in sorted(set(dup))[:MAX_LIST]:
            rows = f[r == k]
            parts = [(_n(x) if pd.notna(x) else "blank") for x in (pd.to_numeric(rows[amt], errors="coerce") if amt else [])]
            dts = [f"{d_:%Y-%m-%d}" for d_ in rows[date]] if date and pd.api.types.is_datetime64_any_dtype(rows[date]) else []
            L.append(f"   - {k}: {len(rows)} rows" + (f", amounts {', '.join(parts)}" if parts else "") + (f", dates {', '.join(dts)}" if dts else ""))
        if len(set(dup)) > MAX_LIST:
            L.append(f"   ...and {len(set(dup)) - MAX_LIST} more.")
        if len(ok) < len(r):
            L.append(f"- {len(r) - len(ok):,} row(s) have no reference, so they cannot be checked for duplicates.")
    full_dup = f.duplicated(keep=False)
    if full_dup.any():
        L.append(f"- {int(full_dup.sum()):,} row(s) are exact copies of another row (every column identical).")

    L += ["", "LIMITS OF THIS ANALYSIS: it describes only the file you uploaded. It does not show whether any transaction is missing or whether records "
              "agree with another source (that needs a second file in Reconciliation), and totals or net movement alone do not prove that a business is healthy."]
    return "\n".join(L)
