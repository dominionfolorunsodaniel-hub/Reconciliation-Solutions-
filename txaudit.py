"""Transaction-by-transaction investigation of ONE bank statement.

Every row is inspected. Every finding lists the exact rows behind it (number, date, reference, narration, type, amount),
and nothing is cut off. All figures are plain arithmetic; the AI is not involved. The report never claims a missing
transaction or a reconciliation difference, because that needs a second file to compare against.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import core

NAIRA = "₦"
BLANK_REF = {"", "nan", "none", "-", "null", "n/a"}
THRESHOLDS = [100_000, 500_000, 1_000_000, 5_000_000]
NO_SECOND_FILE = ("WHAT THIS REPORT CANNOT TELL YOU: only one file was analysed. Without a second file (for example your ledger) "
                  "to compare against, nobody can say whether a transaction is missing, whether anything fails to reconcile, or "
                  "whether the statement is complete. Everything above describes what is inside this statement only. To check "
                  "for missing or unmatched transactions, upload the second file in Reconciliation.")


def _m(v) -> str:
    v = float(v)
    return f"-{NAIRA}{abs(v):,.2f}" if v < 0 else f"{NAIRA}{v:,.2f}"


def _frame(x: pd.DataFrame) -> pd.DataFrame:
    amt, cd = core.AMT_COL, core.CRED_COL
    n = len(x)
    ref = x["Reference"].astype(str).str.strip() if "Reference" in x.columns else pd.Series([""] * n, index=x.index)
    ref = ref.where(~ref.str.lower().isin(BLANK_REF), "")
    d = pd.DataFrame({
        "No.": np.arange(1, n + 1),
        "Date": pd.to_datetime(x["Date"].to_numpy()),
        "Reference": ref.to_numpy(),
        "Narration": (x["Narration"].astype(str).replace({"nan": "", "None": ""}) if "Narration" in x.columns else pd.Series([""] * n, index=x.index)).to_numpy(),
        "Type": x[cd].astype(str).to_numpy(),
        "Amount": x[amt].astype(float).round(2).to_numpy(),
    })
    return d


def _rows(d: pd.DataFrame, has_time: bool, group=None) -> list[dict]:
    out = []
    for i, r in d.iterrows():
        out.append({"Group": (group[i] if group is not None else ""), "No.": int(r["No."]),
                    "Date": r["Date"].strftime("%Y-%m-%d %H:%M" if has_time else "%Y-%m-%d"),
                    "Reference": r["Reference"] or "(none)", "Narration": r["Narration"], "Type": r["Type"], "Amount": float(r["Amount"])})
    return out


def _totals(d: pd.DataFrame) -> dict:
    t = {"rows": int(len(d)), "first_date": d["Date"].min().strftime("%Y-%m-%d"), "last_date": d["Date"].max().strftime("%Y-%m-%d")}
    for kind, key in (("Credit", "in"), ("Debit", "out")):
        s = d[d["Type"] == kind]
        t[f"{key}_count"] = int(len(s))
        t[f"money_{key}"] = round(float(s["Amount"].sum()), 2)
        t[f"average_{key}"] = round(float(s["Amount"].mean()), 2) if len(s) else None   # over this direction only
        for name, idx in (("highest", s["Amount"].idxmax() if len(s) else None), ("lowest", s["Amount"].idxmin() if len(s) else None)):
            t[f"{name}_{key}"] = None if idx is None else {"amount": float(d.loc[idx, "Amount"]), "date": d.loc[idx, "Date"].strftime("%Y-%m-%d"),
                                                          "reference": d.loc[idx, "Reference"] or "(none)", "no": int(d.loc[idx, "No."])}
    t["net_movement"] = round(t["money_in"] - t["money_out"], 2)
    return t


def audit(x: pd.DataFrame, integrity: dict | None = None) -> dict:
    d = _frame(x).reset_index(drop=True)
    if d.empty:
        raise ValueError("There are no transactions to inspect.")
    has_time = bool((d["Date"].dt.hour != 0).any() or (d["Date"].dt.minute != 0).any())
    day = d["Date"].dt.normalize()
    totals = _totals(d)
    findings: list[dict] = []
    notes: list[str] = []

    def add(kind, severity, title, why, method, idx, group=None):
        idx = list(idx)
        if not idx:
            return
        sub = d.loc[idx].reset_index(drop=True)
        g = None if group is None else [group[i] for i in idx]
        findings.append({"id": len(findings) + 1, "kind": kind, "severity": severity, "title": title, "why": why, "method": method,
                         "count": int(len(sub)), "value": round(float(sub["Amount"].sum()), 2), "rows": _rows(sub, has_time, g)})

    # 1. references used more than once
    has_ref = (d["Reference"] != "").any()
    if not has_ref:
        notes.append("This statement has no reference column (or every reference is blank), so duplicate-reference checks could not be run.")
    else:
        blank = int((d["Reference"] == "").sum())
        if blank:
            notes.append(f"{blank} of {len(d)} transactions have no reference, so they cannot be checked for duplicate references.")
        r = d[d["Reference"] != ""]
        same, diff, grp_same, grp_diff = [], [], {}, {}
        for ref, g in r.groupby("Reference", sort=False):
            if len(g) < 2:
                continue
            if g["Amount"].nunique() == 1 and g["Type"].nunique() == 1:
                for i in g.index:
                    same.append(i); grp_same[i] = f"Reference {ref}"
            else:
                for i in g.index:
                    diff.append(i); grp_diff[i] = f"Reference {ref}"
        add("duplicate_reference_same", "high", "Same reference, same amount, same direction",
            "A reference should identify one transaction. Seeing it again with an identical amount and direction is the usual sign of a transaction posted twice.",
            "Rows grouped by exact reference; flagged when a reference appears more than once with the same amount and direction.", same, grp_same)
        add("duplicate_reference_different", "medium", "Same reference used with different amounts or directions",
            "The same reference appears on rows that do not look identical. This can be a legitimate reversal or part-payment, or a reused or mistyped reference. It needs a person to look.",
            "Rows grouped by exact reference; flagged when the amounts or the credit/debit direction differ.", diff, grp_diff)

    # 2. identical transactions on the same day
    base = d[d["Amount"] > 100]
    idx, grp = [], {}
    for key, g in base.groupby([day[base.index], "Type", "Amount", "Narration"], sort=False):
        if len(g) > 1:
            for i in g.index:
                idx.append(i); grp[i] = f"{key[0]:%Y-%m-%d} {key[1]} {_m(key[2])}"
    add("same_day_identical", "medium", "Identical transactions on the same day",
        "Same day, same direction, same amount and same description more than once. It may be a genuine repeat payment or the same one posted twice.",
        "Grouped by date, credit/debit, exact amount and exact narration; amounts of 100 or less are ignored.", idx, grp)

    # 3. repeated amounts
    idx, grp = [], {}
    for (typ, amt), g in d[d["Amount"] >= 100].groupby(["Type", "Amount"], sort=False):
        if len(g) >= 3:
            for i in g.index:
                idx.append(i); grp[i] = f"{typ} {_m(amt)} x{len(g)}"
    add("repeated_amount", "low", "The same amount appearing three or more times",
        "Repeating amounts are normal for subscriptions, salaries and fixed charges, but they are also how split or recycled transactions look. Check that each repeat has a real reason.",
        "Grouped by credit/debit and exact amount; amounts under 100 are ignored; groups of 3 or more are listed.", idx, grp)

    # 4. unusually high amounts (stated method)
    for kind in ("Credit", "Debit"):
        s = d[d["Type"] == kind]
        if len(s) < 8:
            if len(s):
                notes.append(f"Only {len(s)} {kind.lower()}(s): too few to judge which amounts are unusual (at least 8 are needed).")
            continue
        q1, q3 = float(s["Amount"].quantile(0.25)), float(s["Amount"].quantile(0.75))
        iqr = q3 - q1
        fence = q3 + 3 * iqr if iqr > 0 else 5 * float(s["Amount"].median())
        hi = s[s["Amount"] > fence].sort_values("Amount", ascending=False)
        add(f"unusual_{kind.lower()}", "medium", f"Unusually large {kind.lower()}s",
            f"These {kind.lower()}s are far above the normal range of this account's {kind.lower()}s. Unusual does not mean wrong: confirm the source or purpose of each.",
            f"Tukey far-out fence on {len(s)} {kind.lower()}s: Q1 {_m(q1)}, Q3 {_m(q3)}, IQR {_m(iqr)}; flagged above Q3 + 3 x IQR = {_m(fence)}"
            + ("" if iqr > 0 else " (IQR was zero, so 5 x the median was used)") + ". High side only.", hi.index)

    # 5. round numbers
    big = d[d["Amount"] >= 10000]
    if len(big) >= 10:
        rnd = big[(big["Amount"] % 10000) == 0]
        if len(rnd) / len(big) > 0.7:
            add("round_numbers", "low", "Mostly round-number transactions",
                f"{len(rnd)} of {len(big)} transactions of {NAIRA}10,000 or more are exact multiples of {NAIRA}10,000 ({len(rnd) / len(big) * 100:.0f}%). Common for transfers, but also seen in made-up statements.",
                "Share of transactions of 10,000 or more that are exact multiples of 10,000; flagged above 70% when there are at least 10 such transactions.", rnd.index)

    # 6. bursts of activity
    per_day = day.value_counts()
    med = float(per_day.median())
    busy_days = per_day[(per_day >= 15) & (per_day >= 3 * max(med, 1))].index
    idx, grp = [], {}
    for bd in sorted(busy_days):
        for i in d.index[day == bd]:
            idx.append(i); grp[i] = f"{bd:%Y-%m-%d}: {int(per_day[bd])} transactions"
    add("busy_day", "medium", "Unusually busy days",
        f"Normal days in this statement have about {med:g} transaction(s). These days had far more.",
        "Days with at least 15 transactions and at least 3 x the typical (median) day.", idx, grp)
    if has_time:
        order = d.sort_values("Date")
        t = order["Date"].to_numpy().astype("datetime64[s]").astype("int64")
        flag, grp = set(), {}
        for pos in range(len(order)):
            end = np.searchsorted(t, t[pos] + 600, side="right")
            if end - pos >= 4:
                for p2 in range(pos, end):
                    i = order.index[p2]
                    flag.add(i); grp.setdefault(i, f"Burst starting {order.iloc[pos]['Date']:%Y-%m-%d %H:%M}")
        add("rapid_burst", "medium", "Four or more transactions within 10 minutes",
            "A rapid run of transactions can be a bulk payment run or automated activity, but also card testing or structuring. Check the purpose.",
            "Any window of 10 minutes containing at least 4 transactions (needs times in the statement).", sorted(flag), grp)

    # 7. amounts just under round limits
    idx, grp = [], {}
    for typ in ("Credit", "Debit"):
        for thr in THRESHOLDS:
            g = d[(d["Type"] == typ) & (d["Amount"] >= 0.9 * thr) & (d["Amount"] < thr)]
            if len(g) >= 3:
                for i in g.index:
                    idx.append(i); grp[i] = f"{typ}s just under {_m(thr)}"
    add("below_threshold", "medium", "Several amounts just below a round limit",
        "Three or more amounts sitting within 10% below a round figure can mean transactions were kept under a limit on purpose. It can also be coincidence.",
        f"{typ_list()} amounts from 90% up to (not including) each of the limits {', '.join(_m(t) for t in THRESHOLDS)}; 3 or more per limit.", idx, grp)

    # 8. money passing straight through
    idx, grp = [], {}
    daily_out = d[d["Type"] == "Debit"].groupby(day[d["Type"] == "Debit"])["Amount"].sum()
    thru = 0
    for ci, c in d[(d["Type"] == "Credit") & (d["Amount"] >= 50000)].iterrows():
        d0 = day[ci]
        win = d[(d["Type"] == "Debit") & (day >= d0) & (day <= d0 + pd.Timedelta(days=1))]
        if float(win["Amount"].sum()) >= 0.9 * float(c["Amount"]):
            thru += 1
            label = f"Credit No.{int(c['No.'])} {_m(c['Amount'])}"
            for i in [ci] + list(win.index):
                if i not in grp:
                    idx.append(i); grp[i] = label
    add("pass_through", "medium" if thru >= 3 else "low", "Money leaving almost as fast as it arrives",
        f"{thru} credit(s) of {NAIRA}50,000 or more were followed by debits totalling 90% or more of the credit on the same or next day.",
        "For each credit of 50,000 or more: sum of all debits dated the same day or the next day, compared with 90% of the credit.", idx, grp)

    # 9. credit and debit of the same amount close together
    idx, grp, n_pair = [], {}, 0
    for amt, g in d[d["Amount"] >= 1000].groupby("Amount", sort=False):
        cr, dr = g[g["Type"] == "Credit"], g[g["Type"] == "Debit"]
        if cr.empty or dr.empty:
            continue
        used = set()
        for ci, c in cr.iterrows():
            near = dr[(~dr.index.isin(used)) & ((day[dr.index] - day[ci]).abs() <= pd.Timedelta(days=3))]
            if len(near):
                di = near.index[0]; used.add(di); n_pair += 1
                for i in (ci, di):
                    idx.append(i); grp[i] = f"Pair {n_pair}: {_m(amt)}"
    add("offsetting_pair", "low", "A credit and a debit of the same amount within 3 days",
        "This is what a reversal, refund or a round trip looks like. Usually harmless, but confirm that each pair has a real explanation.",
        "Credit and debit with exactly the same amount (1,000 or more) dated within 3 days of each other; each row used once.", idx, grp)

    # 10. long quiet periods (observation only)
    ds = day.sort_values().drop_duplicates()
    gaps = ds.diff().dt.days
    idx, grp = [], {}
    for pos in gaps[gaps >= 14].index:
        before, after = ds.shift(1)[pos], ds[pos]
        for i in list(d.index[day == before][-1:]) + list(d.index[day == after][:1]):
            idx.append(i); grp[i] = f"{int(gaps[pos])} days without transactions: {before:%Y-%m-%d} to {after:%Y-%m-%d}"
    add("quiet_period", "info", "Long periods with no transactions",
        "These are observations only. A quiet account is normal. This does NOT show that anything is missing from the statement; confirming that needs a second file or the bank's own record.",
        "Gaps of 14 days or more between consecutive transaction dates. The rows listed are the last one before and the first one after each gap.", idx, grp)

    # 11. the statement's own running balance
    if integrity and integrity.get("status") in ("failed", "warning"):
        notes.append("Running-balance check: " + integrity["message"])
        for e in (integrity.get("examples") or []):
            notes.append(f"  Balance does not follow: {e['date']} {e['narration']}: expected {e['expected']:,.2f}, statement shows {e['found']:,.2f}.")
    elif integrity and integrity.get("status") == "passed":
        notes.append("Running-balance check passed: " + integrity["message"])

    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings.sort(key=lambda f: (order[f["severity"]], f["id"]))
    for i, f in enumerate(findings, 1):
        f["id"] = i
    table = pd.DataFrame([{"Finding": f["id"], "Severity": f["severity"], "What was found": f["title"], "Group": r["Group"], "No.": r["No."], "Date": r["Date"],
                           "Reference": r["Reference"], "Narration": r["Narration"], "Type": r["Type"], "Amount": r["Amount"]}
                          for f in findings for r in f["rows"]])
    out = {"totals": totals, "findings": findings, "findings_table": table, "notes": notes, "no_second_file": NO_SECOND_FILE}
    out["text"] = report_text(out, has_ref)
    return out


def typ_list() -> str:
    return "Credit and debit"


def report_text(a: dict, has_ref: bool = True) -> str:
    t, f = a["totals"], a["findings"]
    L = ["TRANSACTION-BY-TRANSACTION INVESTIGATION",
         f"Rows inspected: {t['rows']:,} (every row). Period: {t['first_date']} to {t['last_date']}.", "",
         "TOTALS (calculated by the application)",
         f"- Total inflows (credits): {_m(t['money_in'])} from {t['in_count']:,} credit(s)",
         f"- Total outflows (debits): {_m(t['money_out'])} from {t['out_count']:,} debit(s)",
         f"- Net movement: {_m(t['net_movement'])} (inflows minus outflows; " + ("more came in than went out" if t["net_movement"] >= 0 else "more went out than came in") + "). "
         "This is cash movement only and says nothing by itself about the financial health of the account holder."]
    for key, name in (("in", "credit"), ("out", "debit")):
        if t[f"average_{key}"] is not None:
            h, lo = t[f"highest_{key}"], t[f"lowest_{key}"]
            L.append(f"- Average {name}: {_m(t[f'average_{key}'])} (over {t[f'{key}_count']:,} {name}s only). Highest {_m(h['amount'])} on {h['date']} (ref {h['reference']}, No.{h['no']}); "
                     f"lowest {_m(lo['amount'])} on {lo['date']} (ref {lo['reference']}, No.{lo['no']}).")
    L.append("")
    if a["notes"]:
        L += ["DATA NOTES"] + [f"- {n}" if not n.startswith("  ") else n for n in a["notes"]] + [""]
    counts = {s: sum(1 for x in f if x["severity"] == s) for s in ("high", "medium", "low", "info")}
    L.append(f"FINDINGS: {len(f)} in total ({counts['high']} high, {counts['medium']} medium, {counts['low']} low, {counts['info']} observation).")
    if not f:
        L.append("None of the checks found anything that deserves investigation in this statement.")
    for x in f:
        L += ["", f"FINDING {x['id']} [{x['severity'].upper()}]: {x['title']} - {x['count']} row(s), total {_m(x['value'])}",
              f"Why it matters: {x['why']}", f"How it was found: {x['method']}", "Rows:"]
        last = None
        for r in x["rows"]:
            if r["Group"] and r["Group"] != last:
                L.append(f"  [{r['Group']}]")
                last = r["Group"]
            L.append(f"  - No.{r['No.']} | {r['Date']} | ref {r['Reference']} | {r['Type']} {_m(r['Amount'])} | {r['Narration'][:60] or 'no narration'}")
    L += ["", a["no_second_file"]]
    return "\n".join(L)
