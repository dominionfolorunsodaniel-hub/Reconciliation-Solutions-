"""
statement.py - turns a bank statement into: income vs expenses per month, what is left each month, and an
indicative "loan readiness" score. Everything here is computed by plain code from the statement itself
(the AI is only used afterwards, to explain the finished numbers in simple words).

The score is a guide, NOT a lending decision: lenders also look at credit-bureau history, ID checks, collateral, etc.
"""
import io
import re

import pandas as pd

import core

NAIRA = "₦"
BET_WORDS = re.compile(r"\b(?:bet|bets|betting|sportybet|bet9ja|1xbet|nairabet|betking|msport|betway|baba ?ijebu|lotto|casino)\w*", re.I)
LOAN_WORDS = re.compile(r"(?:loan|repay|fairmoney|renmoney|carbon|palmcredit|quickcheck|branch ?intl|lcredit|okash|overdraft)", re.I)
DISCLAIMER = ("This score is a guide worked out only from the statement you uploaded. It is not a loan approval or a credit decision. "
              "Banks and lenders also check credit-bureau records, existing loans, identity, collateral and their own rules.")


STATEMENT_PROMPT = """You are a friendly, careful money coach explaining a bank-statement check to someone who is not technical.
You are given the finished numbers (computed by code): monthly income, spending, what is left, the loan-readiness score, its parts,
good signs, worries, tips and a rough loan guide. Write plain text (no markdown symbols like ** or #) with these sections, each on its own line:
IN SIMPLE WORDS: 3 to 5 short sentences saying how the account looks overall and what the score means.
WHAT STANDS OUT: 2 to 5 lines starting with '- ' about the most important months or habits, with exact figures.
WHAT TO DO NEXT: 2 to 4 lines starting with '- ' with practical steps, in order of importance.
Rules: use ONLY numbers that appear in the data, copied exactly (keep commas and the naira sign); never invent figures, causes or lender rules;
say clearly that this is a guide and not a loan approval; keep it encouraging but honest when the score is weak."""


def money(v) -> str:
    v = float(v)
    sign = "-" if v < -0.004 else ""
    a = abs(v)
    return f"{sign}{NAIRA}0" if a < 0.005 else f"{sign}{NAIRA}{a:,.0f}" if (a >= 100 or a == int(a)) else f"{sign}{NAIRA}{a:,.2f}"


def _date_column(d: pd.DataFrame):
    cands = [c for c in d.columns if pd.api.types.is_datetime64_any_dtype(d[c]) and d[c].notna().sum() > 0]
    if not cands:
        return None
    named = [c for c in cands if re.search(r"date|time|day", str(c), re.I)]
    pool = named or cands
    # banks list a transaction/posting date AND a value date; the day the transaction happened is the better one
    first = [c for c in pool if re.search(r"trans|tran\b|post|booking", str(c), re.I) and not re.search(r"value", str(c), re.I)]
    pool = first or [c for c in pool if not re.search(r"value", str(c), re.I)] or pool
    return max(pool, key=lambda c: d[c].notna().sum())


# lines like "Opening balance" or "Brought forward" are not money moving, so they must not count as income or spending
_BALANCE_LINE = re.compile(r"^\s*(opening|closing|previous|prior|starting|ending)\s+(balance|bal)\b|brought\s+forward|carried\s+forward|^\s*(b/?f|c/?f|bal\s*b/?f|bal\s*c/?f)\b|^\s*balance\s+(b/?f|c/?f|brought|carried)", re.I)

BANK_FILE_WORDS = [("GTBank", r"gtb|guaranty"), ("Access Bank", r"\baccess"), ("Zenith Bank", r"zenith"), ("UBA", r"\buba\b|united bank"),
                   ("First Bank", r"first\s*bank|\bfbn\b"), ("OPay", r"opay"), ("Moniepoint", r"monie\s*point"), ("Kuda", r"kuda"),
                   ("PalmPay", r"palm\s*pay"), ("Fidelity Bank", r"fidelity"), ("Sterling Bank", r"sterling"), ("Wema Bank", r"wema|alat"),
                   ("Stanbic IBTC", r"stanbic"), ("Union Bank", r"union\s*bank"), ("Polaris Bank", r"polaris"), ("FCMB", r"fcmb"),
                   ("Ecobank", r"ecobank"), ("Providus Bank", r"providus"), ("Keystone Bank", r"keystone"), ("Taj Bank", r"\btaj\b")]


def detect_bank(columns, filename: str | None = None) -> str | None:
    """A best guess of which bank produced the file, from its name or its column layout. Only used for a friendly note."""
    name = (filename or "").lower()
    for bank, pat in BANK_FILE_WORDS:
        if re.search(pat, name):
            return bank
    cols = " | ".join(str(c).lower() for c in columns)
    if "originating branch" in cols:
        return "GTBank"
    if "channel" in cols and "transaction reference" in cols and re.search(r"debit|credit", cols):
        return "OPay"
    if "money in" in cols and "money out" in cols and "category" in cols:
        return "Kuda"
    if "money in" in cols and "money out" in cols and "transaction details" in cols:
        return "PalmPay"
    if "withdrawal" in cols and "deposit" in cols and re.search(r"tran(saction)? details", cols):
        return "First Bank"
    if "posted date" in cols:
        return "Access Bank"
    return None


def _balance_column(d: pd.DataFrame):
    for c in d.columns:
        if pd.api.types.is_numeric_dtype(d[c]) and re.search(r"balance|\bbal\b", str(c), re.I):
            return c
    return None


def _narration_column(d: pd.DataFrame):
    for c in d.columns:
        if not pd.api.types.is_numeric_dtype(d[c]) and not pd.api.types.is_datetime64_any_dtype(d[c]) and \
                re.search(r"narrat|descri|detail|remark|particular|memo|reference", str(c), re.I):
            return c
    return None


def monthly_table(df: pd.DataFrame, from_month: str | None = None, to_month: str | None = None):
    """Returns (monthly DataFrame, prepared transactions DataFrame, notes). Transaction columns are renamed to a
    standard set: Date, Credit/Debit, Amount (always positive), and (when present) Balance and Narration."""
    d, money_note = core.add_money_columns(df)
    if core.CRED_COL not in d.columns:
        raise ValueError("I couldn't tell which rows are money coming in (credits) and which are money going out (debits). "
                         "Please upload a bank statement that has Debit/Credit columns, a CR/DR column, or amounts with plus/minus signs.")
    dc = _date_column(d)
    if dc is None:
        raise ValueError("I couldn't find a date column in this file, so I can't group the transactions by month.")
    bc, nc = _balance_column(d), _narration_column(d)
    keep = [dc, core.CRED_COL, core.AMT_COL] + ([bc] if bc else []) + ([nc] if nc else [])
    x = d[keep].dropna(subset=[dc, core.CRED_COL, core.AMT_COL]).reset_index(drop=True)
    x = x.rename(columns={dc: "Date", **({bc: "Balance"} if bc else {}), **({nc: "Narration"} if nc else {})})
    bank_notes = []
    if "Narration" in x.columns:
        is_bal = x["Narration"].astype(str).str.contains(_BALANCE_LINE)
        if is_bal.any():
            bank_notes.append(f"Ignored {int(is_bal.sum())} 'opening/closing/brought forward balance' line(s): they show a balance, not money moving.")
            x = x[~is_bal].reset_index(drop=True)
    bank = detect_bank(df.columns, df.attrs.get("filename"))
    if bank:
        bank_notes.append(f"This looks like a {bank} statement (recognised from its name or layout).")
    if x.empty:
        raise ValueError("No usable transactions were found (each row needs a date, an amount and credit/debit).")
    if len(x) > 1 and x["Date"].iloc[0] > x["Date"].iloc[-1]:  # newest-first statement: put it in time order
        x = x.iloc[::-1].reset_index(drop=True)
    x["Month"] = x["Date"].dt.strftime("%Y-%m")
    all_months = sorted(pd.period_range(x["Month"].min(), x["Month"].max(), freq="M").astype(str))
    if from_month:
        x = x[x["Month"] >= from_month]
    if to_month:
        x = x[x["Month"] <= to_month]
    x = x.reset_index(drop=True)
    if x.empty:
        raise ValueError("There are no transactions in the months you chose.")

    months = pd.period_range(x["Month"].min(), x["Month"].max(), freq="M").astype(str)
    inc = x[x[core.CRED_COL] == "Credit"].groupby("Month")[core.AMT_COL].agg(["sum", "count"])
    exp = x[x[core.CRED_COL] == "Debit"].groupby("Month")[core.AMT_COL].agg(["sum", "count"])
    rows = []
    for m in months:
        i, e = float(inc["sum"].get(m, 0.0)), float(exp["sum"].get(m, 0.0))
        row = {"Month": m, "Income (credits)": round(i, 2), "Expenses (debits)": round(e, 2), "Left over (income - expenses)": round(i - e, 2),
               "Credits": int(inc["count"].get(m, 0)), "Debits": int(exp["count"].get(m, 0))}
        if bc:
            sub = x[x["Month"] == m]
            row["Closing balance"] = round(float(sub["Balance"].dropna().iloc[-1]), 2) if sub["Balance"].notna().any() else None
        rows.append(row)
    table = pd.DataFrame(rows)
    notes = ([money_note] if money_note else []) + bank_notes
    x.attrs.update({"balance_col": "Balance" if bc else None, "narration_col": "Narration" if nc else None, "all_months": all_months})
    return table, x, notes


# --------------------------------------------------------------------------- #
# Is the statement genuine?  Does every balance follow from the one before it?
# --------------------------------------------------------------------------- #
def integrity_check(x: pd.DataFrame) -> dict:
    """In a real statement, balance = previous balance + credit - debit on every row. Edited or forged statements usually break this."""
    if "Balance" not in x.columns or x["Balance"].notna().sum() < 3:
        return {"status": "unavailable", "checked": 0, "mismatches": 0, "examples": [],
                "message": "This statement has no running balance column, so the balance check could not be done."}
    y = x.dropna(subset=["Balance"]).reset_index(drop=True)
    signed = y[core.AMT_COL].where(y[core.CRED_COL] == "Credit", -y[core.AMT_COL])
    expected = y["Balance"].shift(1) + signed
    diff = (y["Balance"] - expected).abs()
    checked = int(diff.notna().sum())
    bad = diff.notna() & (diff > 1.0)
    n_bad = int(bad.sum())
    examples = []
    for i in list(y.index[bad])[:5]:
        examples.append({"date": f"{y.loc[i, 'Date']:%Y-%m-%d}", "narration": str(y.loc[i, "Narration"])[:60] if "Narration" in y.columns else "",
                         "expected": round(float(expected[i]), 2), "found": round(float(y.loc[i, "Balance"]), 2)})
    share = n_bad / checked if checked else 0.0
    if n_bad == 0:
        status, msg = "passed", f"Balance check passed: all {checked:,} balances follow correctly from the transactions."
    elif share <= 0.02:
        status, msg = "warning", (f"Balance check: {n_bad} of {checked:,} balances do not follow from the transactions. "
                                  "A few can be caused by the way the bank orders same-day rows, but please look at the examples.")
    else:
        status, msg = "failed", (f"Balance check FAILED: {n_bad} of {checked:,} balances ({share * 100:.0f}%) do not follow from the transactions. "
                                 "This can mean the statement was edited, rows are missing, or the file is incomplete.")
    return {"status": status, "checked": checked, "mismatches": n_bad, "examples": examples, "message": msg}


# --------------------------------------------------------------------------- #
# Unusual patterns worth a second look
# --------------------------------------------------------------------------- #
def fraud_flags(x: pd.DataFrame) -> list[dict]:
    flags = []

    def add(kind, severity, title, detail, count, examples=None):
        flags.append({"type": kind, "severity": severity, "title": title, "detail": detail, "count": int(count), "examples": examples or []})

    amt, cd = core.AMT_COL, core.CRED_COL
    day = x["Date"].dt.normalize()
    # duplicates: same day, same amount, same direction, same description
    g = pd.DataFrame({"d": day, "c": x[cd], "a": x[amt]})
    if "Narration" in x.columns:
        g["n"] = x["Narration"].astype(str)
    dup = g.duplicated(keep=False) & (g["a"] > 100)
    if dup.any():
        grp = g[dup].drop_duplicates()
        ex = []
        for _, r in grp.head(4).iterrows():
            ex.append(f"{r['d']:%Y-%m-%d} {r['c']} {money(r['a'])}" + (f" - {r['n'][:40]}" if "n" in grp.columns else ""))
        add("duplicates", "high" if len(grp) >= 3 else "medium", "Possible duplicate transactions",
            f"{len(grp)} transaction(s) appear more than once on the same day with the same amount.", len(grp), ex)
    # unusually large amounts
    for direction in ("Credit", "Debit"):
        s = x.loc[x[cd] == direction, amt]
        if len(s) >= 10:
            lim = max(5 * float(s.median()), float(s.mean() + 3 * s.std(ddof=0)))
            big = x[(x[cd] == direction) & (x[amt] > lim)]
            if len(big) and lim > 0:
                add("large", "medium", f"Unusually large {direction.lower()}s",
                    f"{len(big)} {direction.lower()}(s) are far bigger than the account's normal activity (above {money(lim)}).", len(big),
                    [f"{r['Date']:%Y-%m-%d} {money(r[amt])}" for _, r in big.head(3).iterrows()])
    # round numbers
    big_tx = x[x[amt] >= 10000]
    if len(big_tx) >= 10:
        share = float((big_tx[amt] % 10000 == 0).mean())
        if share > 0.7:
            add("round", "low", "Many round-number transactions", f"{share * 100:.0f}% of the larger transactions are exact multiples of {NAIRA}10,000, "
                "which can be normal for transfers but is also common in made-up statements.", int((big_tx[amt] % 10000 == 0).sum()))
    # night-time activity (only if the statement has times)
    if (x["Date"].dt.hour != 0).any():
        night = x[x["Date"].dt.hour < 4]
        if len(night) >= 5 and len(night) / len(x) > 0.1:
            add("night", "low", "Lots of activity between midnight and 4am", f"{len(night)} transactions ({len(night) / len(x) * 100:.0f}%).", len(night))
    # very busy days
    per_day = day.value_counts()
    busy = per_day[per_day > 20]
    if len(busy):
        add("busy", "medium", "Very busy days", f"{len(busy)} day(s) had more than 20 transactions (busiest: {int(busy.max())}).", len(busy))
    # money that passes straight through the account
    cr = x[(x[cd] == "Credit") & (x[amt] >= 50000)]
    if len(cr):
        daily_out = x[x[cd] == "Debit"].groupby(day[x[cd] == "Debit"])[amt].sum()
        thru = 0
        for _, r in cr.iterrows():
            d0 = r["Date"].normalize()
            out = float(daily_out.get(d0, 0.0) + daily_out.get(d0 + pd.Timedelta(days=1), 0.0))
            if out >= 0.9 * float(r[amt]):
                thru += 1
        tot_cr = float(x.loc[x[cd] == "Credit", amt].sum())
        if thru >= 3 or (thru and thru / len(cr) >= 0.5):
            add("passthrough", "medium", "Money leaves almost as fast as it arrives",
                f"{thru} of {len(cr)} larger credits were spent (90% or more) within a day. Lenders may see this as money passing through rather than income kept.", thru)
    return flags


# --------------------------------------------------------------------------- #
# Where does the money go / come from?
# --------------------------------------------------------------------------- #
_I = re.I
DEBIT_RULES = [
    ("Betting", BET_WORDS),
    ("Loan repayments", LOAN_WORDS),
    ("Education", re.compile(r"school|tuition|university|waec|jamb|exam|college", _I)),
    ("Bank charges & fees", re.compile(r"\bfees?\b|charge|stamp ?duty|\bvat\b|\bsms\b|maintenance|commission|levy|cot\b", _I)),
    ("Airtime & data", re.compile(r"airtime|data ?bundle|\bdata\b|\bmtn\b|\bglo\b|airtel|9mobile|vtu|recharge", _I)),
    ("Bills & subscriptions", re.compile(r"electric|ikedc|ekedc|phcn|nepa|disco|dstv|gotv|startimes|water|cable|internet|spectranet|smile|subscription|netflix|\btax\b|firs|prepaid|postpaid", _I)),
    ("Rent & housing", re.compile(r"\brent\b|landlord|estate|mortgage|housing", _I)),
    ("Food & groceries", re.compile(r"food|shoprite|market|restaurant|chicken|kfc|domino|supermarket|grocer|bakery|eatery|cafe|\bspar\b|bigg|sweet sensation|suya|canteen", _I)),
    ("Transport & fuel", re.compile(r"uber|bolt|fuel|petrol|filling|total ?energies|mobil|oando|transport|\bbus\b|taxi|\bride\b|\btoll\b|brt|indriver|keke", _I)),
    ("Health", re.compile(r"pharmac|hospital|clinic|medic|health|\blab\b|dental", _I)),
    ("Cash withdrawals", re.compile(r"\batm\b|cash ?withdraw|withdrawal", _I)),
    ("Transfers out", re.compile(r"transfer|\btrf\b|\bnip\b|\bto\b", _I)),
    ("Card / POS purchases", re.compile(r"\bpos\b|web ?pay|purchase|payment|\bpay\b", _I)),
]
CREDIT_RULES = [
    ("Salary & wages", re.compile(r"salary|wages|payroll|stipend", _I)),
    ("Business sales (POS / settlements)", re.compile(r"\bpos\b|sales?\b|settlement|merchant|invoice", _I)),
    ("Cash deposits", re.compile(r"cash ?deposit|\bdeposit|lodg", _I)),
    ("Interest & refunds", re.compile(r"interest|refund|reversal|cashback", _I)),
    ("Loans received", LOAN_WORDS),
    ("Transfers in", re.compile(r"transfer|\btrf\b|\bnip\b|\bfrom\b", _I)),
]


def _label(text: str, rules, default: str) -> str:
    for name, rx in rules:
        if rx.search(text):
            return name
    return default


def categorize(x: pd.DataFrame) -> dict | None:
    """Group spending and income by what the description says. Returns None when there are no descriptions."""
    if "Narration" not in x.columns or x["Narration"].notna().sum() == 0:
        return None
    y = x.copy()
    txt = y["Narration"].astype(str)
    y["Category"] = [(_label(t, DEBIT_RULES, "Other spending") if c == "Debit" else _label(t, CREDIT_RULES, "Other income"))
                     for t, c in zip(txt, y[core.CRED_COL])]

    def summarize(direction):
        sub = y[y[core.CRED_COL] == direction]
        if sub.empty:
            return [], None
        g = sub.groupby("Category")[core.AMT_COL].agg(["sum", "count"]).sort_values("sum", ascending=False)
        tot = float(g["sum"].sum())
        rows = [{"category": c, "amount": round(float(r["sum"]), 2), "count": int(r["count"]), "share_pct": round(100 * float(r["sum"]) / tot, 1) if tot else 0.0}
                for c, r in g.iterrows()]
        piv = sub.pivot_table(index="Category", columns="Month", values=core.AMT_COL, aggfunc="sum", fill_value=0.0).reindex([r["category"] for r in rows]).round(2)
        return rows, piv

    spend, spend_m = summarize("Debit")
    inc, inc_m = summarize("Credit")
    return {"spending": spend, "income": inc, "spending_by_month": spend_m, "income_by_month": inc_m}


def _clip(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def score_statement(table: pd.DataFrame, x: pd.DataFrame, integrity: dict | None = None, flags: list | None = None) -> dict:
    n = len(table)
    inc, exp, net = table["Income (credits)"], table["Expenses (debits)"], table["Left over (income - expenses)"]
    total_inc, total_exp = float(inc.sum()), float(exp.sum())
    avg_inc, avg_exp, avg_net = float(inc.mean()), float(exp.mean()), float(net.mean())
    sr = (total_inc - total_exp) / total_inc if total_inc > 0 else -1.0
    bc, nc = x.attrs.get("balance_col"), x.attrs.get("narration_col")
    factors, strengths, concerns, advice = [], [], [], []

    def add(name, score, mx, detail):
        factors.append({"name": name, "score": round(score, 1), "max": mx, "detail": detail})

    # 1) how much history we can see
    hist = {1: 1, 2: 3, 3: 5, 4: 7, 5: 8}.get(n, 10 if n >= 6 else 0)
    add("Length of history", hist, 10, f"{n} month(s) of statement. Lenders usually like 6 months or more.")
    if n < 3:
        concerns.append(f"Only {n} month(s) of history. Most lenders ask for 3 to 6 months.")
        advice.append("Upload 6 months of statements for a fairer score.")
    elif n >= 6:
        strengths.append(f"{n} months of history to judge from.")

    # 2) is money coming in regularly and steadily?
    active = float((inc > 0).mean())
    cv = float(inc.std(ddof=0) / inc.mean()) if inc.mean() > 0 and n > 1 else 0.0
    cv_pts = 13 if cv <= 0.25 else 10 if cv <= 0.5 else 6 if cv <= 0.8 else 3 if cv <= 1.2 else 1
    add("Income regularity", 12 * active + (cv_pts if n > 1 else 7), 25,
        f"Money came in during {int((inc > 0).sum())} of {n} month(s); month-to-month swing is {cv * 100:.0f}% (lower is steadier).")
    if active == 1 and cv <= 0.5:
        strengths.append("Money comes in every month and the amounts are fairly steady.")
    if active < 1:
        concerns.append(f"No income at all in {int((inc == 0).sum())} of {n} month(s).")
        advice.append("Lenders like income that arrives every month. Keep your main income going through this account.")
    if cv > 0.8 and n > 1:
        concerns.append("Income swings a lot from month to month.")

    # 3) does the person keep money after spending?
    add("Money kept after spending", 25 * _clip(sr / 0.25) if sr > 0 else 0, 25,
        f"Over the period, income was {money(total_inc)} and spending {money(total_exp)}: kept {sr * 100:.0f}% of income." if total_inc > 0 else "No income found.")
    if sr >= 0.2:
        strengths.append(f"You kept about {sr * 100:.0f}% of what came in.")
    elif sr < 0.05:
        concerns.append("Almost nothing, or less than nothing, is left after spending." if sr >= 0 else "Spending is higher than income.")
        advice.append("Reduce regular spending so at least 10% to 20% of income is left each month.")

    # 4) how many months ended in surplus?
    pos = float((net >= 0).mean())
    add("Months ending in surplus", 15 * pos, 15, f"{int((net >= 0).sum())} of {n} month(s) ended with more coming in than going out.")
    if pos < 0.5:
        concerns.append(f"Spending beat income in {int((net < 0).sum())} of {n} month(s).")

    # 5) cushion in the account
    if bc:
        bal = x[bc].dropna()
        neg_share = float((bal < 0).mean()) if len(bal) else 0.0
        closing = table["Closing balance"].dropna()
        cover = float(closing.mean() / avg_exp) if len(closing) and avg_exp > 0 else 1.0
        pts = 6 * (1 - _clip(neg_share * 5)) + 9 * _clip(cover)
        add("Cash cushion in the account", pts, 15,
            f"Average month-end balance {money(float(closing.mean()))}, about {cover:.1f} month(s) of spending; "
            f"lowest balance {money(float(bal.min()))}." if len(closing) else "No balance figures found.")
        if neg_share > 0:
            concerns.append("The balance went below zero at times.")
        elif cover >= 1:
            strengths.append("The account usually holds at least a month of spending.")
        elif cover < 0.5:
            concerns.append("Month-end balances are small compared with monthly spending.")
            advice.append("Try to keep at least one month of spending in the account as a cushion.")

    # 6) is income growing or shrinking?
    if n >= 2:
        half = n // 2
        first, last = float(inc.iloc[:half].mean()), float(inc.iloc[-half:].mean())
        ch = (last - first) / first if first > 0 else (1.0 if last > 0 else 0.0)
        pts = 10 if ch >= 0 else 7 if ch >= -0.1 else 4 if ch >= -0.25 else 1
        add("Income direction", pts, 10, f"Average monthly income went from {money(first)} (earlier months) to {money(last)} (recent months).")
        if ch >= 0.1:
            strengths.append("Income is growing.")
        elif ch < -0.1:
            concerns.append("Income is lower in the recent months than earlier.")

    # 7) spending habits and existing loans (only if descriptions exist)
    loan_monthly = 0.0
    if nc:
        deb = x[x[core.CRED_COL] == "Debit"]
        txt = deb[nc].astype(str)
        bet = deb[txt.str.contains(BET_WORDS)]
        loans = deb[txt.str.contains(LOAN_WORDS)]
        bet_amt, loan_amt = float(bet[core.AMT_COL].sum()), float(loans[core.AMT_COL].sum())
        bet_share = bet_amt / total_exp if total_exp > 0 else 0.0
        pts = 10 if bet_amt == 0 else 7 if bet_share <= 0.02 else 4 if bet_share <= 0.05 else 0
        add("Spending habits", pts, 10, "No betting-type spending found." if bet_amt == 0 else
            f"Betting-type spending: {len(bet)} transaction(s), {money(bet_amt)} ({bet_share * 100:.1f}% of spending).")
        if bet_amt > 0 and bet_share > 0.02:
            concerns.append(f"Betting-type spending of {money(bet_amt)} ({bet_share * 100:.1f}% of spending). Many lenders count this against you.")
            advice.append("Cut betting-type spending for a few months before applying for a loan.")
        elif bet_amt == 0:
            strengths.append("No betting-type spending found.")
        if len(loans):
            loan_monthly = loan_amt / n
            concerns.append(f"Possible existing loan repayments found: {len(loans)} transaction(s), about {money(loan_monthly)} a month.")
            advice.append("Existing loan repayments reduce how much new repayment you can carry.")

    # 8) unusual patterns in the account
    if flags is not None:
        pen = {"high": 5, "medium": 3, "low": 1}
        pts = max(0, 10 - sum(pen[f["severity"]] for f in flags))
        add("Account behaviour", pts, 10, "No unusual patterns found." if not flags else
            "Found: " + "; ".join(f["title"].lower() for f in flags) + ".")
        for f in flags:
            if f["severity"] in ("high", "medium"):
                concerns.append(f"{f['title']}: {f['detail']}")
        if not flags:
            strengths.append("No duplicate, oversized or pass-through patterns found.")
    mx = sum(f["max"] for f in factors)
    score = int(round(100 * sum(f["score"] for f in factors) / mx)) if mx else 0
    if integrity:
        if integrity["status"] == "passed":
            strengths.append("Balance check passed: every balance follows from the transactions, so the statement looks untouched.")
        elif integrity["status"] == "warning":
            concerns.append(integrity["message"])
        elif integrity["status"] == "failed":
            score = min(score, 45)
            concerns.insert(0, integrity["message"])
            advice.insert(0, "Upload the statement exactly as the bank issued it (download it again from the bank app or get a stamped copy).")
    if score >= 75:
        verdict, elig = "Strong", "Likely to qualify with many lenders for a loan within the guide below."
    elif score >= 60:
        verdict, elig = "Good", "May qualify, possibly for a smaller amount or with extra conditions."
    elif score >= 45:
        verdict, elig = "Fair", "Borderline. Lenders may ask for more months of statements, a guarantor or collateral."
    else:
        verdict, elig = "Weak", "Unlikely to qualify right now. See the tips below to improve before applying."
    if n < 3:
        elig += " (With so little history this result is not reliable.)"

    # rough affordability guide: about one third of income, never more than the usual monthly surplus, minus loans already being repaid
    capacity = max(0.0, min(0.33 * avg_inc, 0.8 * max(avg_net, 0.0)) - loan_monthly)
    guide = None
    if capacity > 0:
        guide = {"max_monthly_repayment": round(capacity, 0), "rough_loan_6_months": round(capacity * 6, 0),
                 "rough_loan_12_months": round(capacity * 12, 0),
                 "how": "About one third of average monthly income, and never more than 80% of the average monthly surplus"
                        + (", after existing loan repayments" if loan_monthly else "")
                        + ". Loan amounts are rough and before interest; a real loan costs more, so a lender will offer less."}
    else:
        advice.append("There is no monthly surplus to repay a loan from yet. Build a surplus first.")
    return {
        "score": score, "verdict": verdict, "eligibility": elig, "factors": factors,
        "strengths": strengths, "concerns": concerns, "advice": list(dict.fromkeys(advice)), "loan_guide": guide,
        "integrity": integrity, "flags": flags or [],
        "totals": {"months": n, "income": round(total_inc, 2), "expenses": round(total_exp, 2), "net": round(total_inc - total_exp, 2),
                   "avg_income": round(avg_inc, 2), "avg_expenses": round(avg_exp, 2), "avg_left_over": round(avg_net, 2),
                   "kept_pct": round(sr * 100, 1)},
        "disclaimer": DISCLAIMER,
    }


def _combine(tables: list[pd.DataFrame]) -> pd.DataFrame:
    """Add several accounts' monthly tables together (balances are carried forward, then added)."""
    months = pd.period_range(min(t["Month"].min() for t in tables), max(t["Month"].max() for t in tables), freq="M").astype(str)
    out = pd.DataFrame({"Month": months})
    for col in ("Income (credits)", "Expenses (debits)", "Left over (income - expenses)", "Credits", "Debits"):
        out[col] = sum(t.set_index("Month")[col].reindex(months).fillna(0) for t in tables).values
    if all("Closing balance" in t.columns for t in tables):
        out["Closing balance"] = sum(t.set_index("Month")["Closing balance"].reindex(months).ffill().bfill() for t in tables).round(2).values
    out["Credits"], out["Debits"] = out["Credits"].astype(int), out["Debits"].astype(int)
    return out


# --------------------------------------------------------------------------- #
# A simple, honest look ahead: what the next months may look like if habits stay the same
# --------------------------------------------------------------------------- #
FORECAST_NOTE = ("This is a simple projection from the months in the statement (recent months count more, with a gentle allowance for the trend). "
                 "Real months can differ, especially when income is irregular. It is a planning aid, not a promise.")


def _project(vals: list[float], steps: int) -> list[tuple[float, float, float]]:
    """(expected, low, high) for each future month."""
    import numpy as np
    k = len(vals)
    w = np.arange(1, k + 1, dtype=float)
    base = float(np.average(vals, weights=w))
    slope = float(np.polyfit(np.arange(k), vals, 1)[0]) if k >= 3 else 0.0
    cap = 0.15 * abs(base) if base else 0.0
    slope = max(-cap, min(cap, slope)) * 0.5          # damped and capped so one odd month cannot run away
    spread = float(np.std(vals))
    out = []
    for i in range(1, steps + 1):
        e = max(0.0, base + slope * i)
        out.append((e, max(0.0, e - spread), e + spread))
    return out


def forecast(table: pd.DataFrame, steps: int = 3) -> dict:
    if len(table) < 3:
        return {"available": False, "reason": "A forecast needs at least 3 months of history. Upload a longer statement."}
    t = table.tail(6)
    inc, exp = t["Income (credits)"].astype(float).tolist(), t["Expenses (debits)"].astype(float).tolist()
    pi, pe = _project(inc, steps), _project(exp, steps)
    last = pd.Period(str(table["Month"].iloc[-1]), freq="M")
    bal = None
    if "Closing balance" in table.columns and pd.notna(table["Closing balance"].iloc[-1]):
        bal = float(table["Closing balance"].iloc[-1])
    rows, warnings = [], []
    mean_inc = sum(inc) / len(inc)
    spread_ratio = (pd.Series(inc).std(ddof=0) / mean_inc) if mean_inc else 0
    for i in range(steps):
        net = pi[i][0] - pe[i][0]
        row = {"Month": str(last + (i + 1)), "Expected income": round(pi[i][0], 2), "Expected spending": round(pe[i][0], 2),
               "Expected left over": round(net, 2), "Income (low)": round(pi[i][1], 2), "Income (high)": round(pi[i][2], 2),
               "Spending (low)": round(pe[i][1], 2), "Spending (high)": round(pe[i][2], 2)}
        if bal is not None:
            bal += net
            row["Expected balance"] = round(bal, 2)
        rows.append(row)
    negatives = [r["Month"] for r in rows if r["Expected left over"] < 0]
    if negatives:
        warnings.append("Spending is expected to be higher than income in " + ", ".join(negatives) + ".")
    if bal is not None and any(r.get("Expected balance", 0) < 0 for r in rows):
        warnings.append("The balance could go below zero if habits stay the same.")
    confidence = "low" if len(table) < 4 or spread_ratio > 0.5 else "medium" if len(table) < 6 or spread_ratio > 0.25 else "good"
    if confidence == "low":
        warnings.append("Income moves a lot from month to month (or there is little history), so treat this forecast loosely.")
    return {"available": True, "rows": rows, "warnings": warnings, "confidence": confidence, "based_on_months": len(t), "note": FORECAST_NOTE}


def build_many(items: list[tuple[str, pd.DataFrame]], from_month: str | None = None, to_month: str | None = None) -> dict:
    """items = [(account label, DataFrame), ...]. One item = a normal statement check; several = the accounts added together."""
    parts = []
    for label, df in items:
        try:
            table, x, notes = monthly_table(df, from_month, to_month)
        except ValueError as e:
            raise ValueError(f"{label}: {e}" if len(items) > 1 else str(e)) from e
        x["Account"] = label
        parts.append((label, table, x, notes))
    table = parts[0][1] if len(parts) == 1 else _combine([p[1] for p in parts])
    x = pd.concat([p[2] for p in parts], ignore_index=True) if len(parts) > 1 else parts[0][2]
    if len(parts) > 1:
        x.attrs.update({"balance_col": "Balance" if "Balance" in x.columns else None,
                        "narration_col": "Narration" if "Narration" in x.columns else None})
    notes = [n for p in parts for n in p[3]]
    # authenticity and unusual patterns are judged per account (money moving between your own accounts is not a duplicate)
    integ_list, flags = [], []
    for label, _, xi, _ in parts:
        ic = integrity_check(xi)
        integ_list.append({**ic, "account": label})
        for f in fraud_flags(xi):
            flags.append({**f, "title": f["title"] + (f" ({label})" if len(parts) > 1 and label else "")})
    order = ["failed", "warning", "passed", "unavailable"]
    worst = min(integ_list, key=lambda i: order.index(i["status"]))
    integrity = {**worst, "per_account": integ_list if len(parts) > 1 else []}
    if len(parts) > 1:
        integrity["message"] = " | ".join(f"{i['account']}: {i['message']}" for i in integ_list if i["status"] != "unavailable") or worst["message"]
        notes.append("Money moved between your own accounts counts as income in one and spending in the other, so combined figures can look bigger than they really are.")
    out = score_statement(table, x, integrity, flags)
    cats = categorize(x)
    all_months = sorted(set(m for p in parts for m in p[2].attrs.get("all_months", [])))
    out.update({"table": table, "notes": notes, "period": f"{table['Month'].iloc[0]} to {table['Month'].iloc[-1]}",
                "months_available": all_months, "categories": cats, "accounts": [p[0] for p in parts],
                "forecast": forecast(table)})
    return out


def build(df: pd.DataFrame, from_month: str | None = None, to_month: str | None = None) -> dict:
    return build_many([("", df)], from_month, to_month)


def score_one(label: str, df: pd.DataFrame) -> dict:
    """One row of the lender's ranking table. Never raises: a file that cannot be read shows its reason instead."""
    try:
        r = build(df)
    except Exception as e:  # noqa: BLE001
        return {"applicant": label, "error": str(e)[:200]}
    t, g = r["totals"], r["loan_guide"]
    return {"applicant": label, "score": r["score"], "verdict": r["verdict"], "months": t["months"], "period": r["period"],
            "avg_income": t["avg_income"], "avg_expenses": t["avg_expenses"], "kept_pct": t["kept_pct"],
            "balance_check": r["integrity"]["status"], "flags_high": sum(f["severity"] == "high" for f in r["flags"]),
            "flags_medium": sum(f["severity"] == "medium" for f in r["flags"]),
            "max_monthly_repayment": g["max_monthly_repayment"] if g else 0,
            "main_concern": (r["concerns"][0] if r["concerns"] else "")[:160],
            "next_step": next_step(r["verdict"], r["integrity"]["status"], sum(f["severity"] == "high" for f in r["flags"]))}


def next_step(verdict: str, balance_check: str, high_flags: int) -> str:
    """A plain suggestion for the lender's reviewer. It is a guide, never a decision."""
    if balance_check == "failed":
        return "Verify the statement is genuine first (balances do not add up)"
    if high_flags:
        return "Review the unusual patterns before deciding"
    return {"Excellent": "Looks strong: continue with normal checks", "Good": "Looks fine: continue with normal checks",
            "Fair": "Borderline: ask for more months or a guarantor"}.get(verdict, "Weak: likely to need support before lending")


BATCH_COLUMNS = [("rank", "Rank"), ("applicant", "Applicant / file"), ("score", "Score"), ("verdict", "Verdict"), ("months", "Months"),
                 ("period", "Period"), ("avg_income", "Avg monthly income"), ("avg_expenses", "Avg monthly spending"), ("kept_pct", "% kept"),
                 ("max_monthly_repayment", "Comfortable monthly repayment"), ("balance_check", "Balance check"),
                 ("flags_high", "High flags"), ("flags_medium", "Medium flags"), ("next_step", "Suggested next step"),
                 ("main_concern", "Main concern"), ("error", "Problem")]


def rank_rows(rows: list[dict]) -> list[dict]:
    """Best score first; files that could not be read go to the bottom."""
    ok = sorted([r for r in rows if "error" not in r], key=lambda r: (-r["score"], r["applicant"]))
    bad = [r for r in rows if "error" in r]
    out = []
    for i, r in enumerate(ok + bad, 1):
        out.append({"rank": i if "error" not in r else None, **r})
    return out


def batch_table(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rank_rows(rows))
    cols = [(k, v) for k, v in BATCH_COLUMNS if k in df.columns and df[k].notna().any()]
    return df[[k for k, _ in cols]].rename(columns=dict(cols))


def batch_excel(rows: list[dict]) -> io.BytesIO:
    t = batch_table(rows)
    out = io.BytesIO()
    note = pd.DataFrame({"Read me": [DISCLAIMER, "Scores are computed the same way for every file so applicants can be compared fairly.",
                                     "'Balance check' compares each running balance with the credits and debits. 'failed' means the statement may have been edited."]})
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        t.to_excel(w, "Ranking", index=False)
        note.to_excel(w, "Read me", index=False)
        for ws in w.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(60, max(12, max(len(str(c.value or "")) for c in col[:50]) + 2))
    out.seek(0)
    return out


def summary_text(r: dict) -> str:
    """Plain-language summary written by code (used by itself if the AI is unavailable)."""
    t, lines = r["totals"], []
    lines.append(f"LOAN READINESS: {r['score']} out of 100 - {r['verdict']}. {r['eligibility']}")
    lines.append(f"\nPERIOD: {r['period']} ({t['months']} month(s)). Income {money(t['income'])}, spending {money(t['expenses'])}, "
                 f"left over {money(t['net'])}.")
    lines.append("\nMONTH BY MONTH:")
    for _, m in r["table"].iterrows():
        extra = f", closing balance {money(m['Closing balance'])}" if "Closing balance" in m and pd.notna(m.get("Closing balance")) else ""
        lines.append(f"- {m['Month']}: income {money(m['Income (credits)'])}, spending {money(m['Expenses (debits)'])}, "
                     f"left over {money(m['Left over (income - expenses)'])}{extra}")
    ig = r.get("integrity")
    if ig and ig["status"] != "unavailable":
        lines.append("\nIS THE STATEMENT GENUINE? " + ig["message"])
    elif ig:
        lines.append("\nIS THE STATEMENT GENUINE? " + ig["message"])
    if r.get("flags"):
        lines.append("\nUNUSUAL PATTERNS:\n" + "\n".join(f"- [{f['severity']}] {f['title']}: {f['detail']}" for f in r["flags"]))
    cats = r.get("categories")
    if cats and cats["spending"]:
        lines.append("\nWHERE THE MONEY WENT:\n" + "\n".join(f"- {c['category']}: {money(c['amount'])} ({c['share_pct']}%)" for c in cats["spending"][:8]))
    if cats and cats["income"]:
        lines.append("\nWHERE THE MONEY CAME FROM:\n" + "\n".join(f"- {c['category']}: {money(c['amount'])} ({c['share_pct']}%)" for c in cats["income"][:8]))
    fc = r.get("forecast")
    if fc and fc.get("available"):
        lines.append(f"\nLOOKING AHEAD (confidence: {fc['confidence']}):\n" + "\n".join(
            f"- {x['Month']}: income about {money(x['Expected income'])}, spending about {money(x['Expected spending'])}, left over about {money(x['Expected left over'])}"
            + (f", balance about {money(x['Expected balance'])}" if "Expected balance" in x else "") for x in fc["rows"]))
        lines += ["- " + w for w in fc["warnings"]]
        lines.append(fc["note"])
    if r["strengths"]:
        lines.append("\nGOOD SIGNS:\n" + "\n".join("- " + s for s in r["strengths"]))
    if r["concerns"]:
        lines.append("\nWHAT LENDERS MAY WORRY ABOUT:\n" + "\n".join("- " + s for s in r["concerns"]))
    if r["advice"]:
        lines.append("\nHOW TO IMPROVE:\n" + "\n".join("- " + s for s in r["advice"]))
    g = r["loan_guide"]
    if g:
        lines.append(f"\nROUGH LOAN GUIDE: comfortable monthly repayment up to {money(g['max_monthly_repayment'])}; roughly "
                     f"{money(g['rough_loan_6_months'])} over 6 months or {money(g['rough_loan_12_months'])} over 12 months before interest. {g['how']}")
    lines.append("\nHOW THE SCORE IS BUILT:\n" + "\n".join(f"- {f['name']}: {f['score']:g} of {f['max']} - {f['detail']}" for f in r["factors"]))
    lines.append("\n" + r["disclaimer"])
    return "\n".join(lines)


def chart_png(table: pd.DataFrame):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 5))
    xs = range(len(table))
    ax.bar([i - 0.2 for i in xs], table["Income (credits)"], width=0.4, label="Income", color="#10b981")
    ax.bar([i + 0.2 for i in xs], table["Expenses (debits)"], width=0.4, label="Expenses", color="#ef4444")
    ax.plot(list(xs), table["Left over (income - expenses)"], marker="o", color="#2b59ff", label="Left over")
    ax.set_xticks(list(xs))
    ax.set_xticklabels(table["Month"], rotation=45, ha="right")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    ax.set_title("Income, spending and what is left each month")
    fig.tight_layout()
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=150)
    plt.close(fig)
    return out.getvalue()


def excel(r: dict) -> io.BytesIO:
    t = r["totals"]
    summary = pd.DataFrame([["Loan readiness score", f"{r['score']} / 100"], ["Verdict", r["verdict"]], ["What it means", r["eligibility"]],
                            ["Period", r["period"]], ["Total income", t["income"]], ["Total spending", t["expenses"]],
                            ["Left over", t["net"]], ["Average monthly income", t["avg_income"]], ["Average monthly spending", t["avg_expenses"]],
                            ["Note", r["disclaimer"]]], columns=["Item", "Value"])
    factors = pd.DataFrame(r["factors"]).rename(columns={"name": "Factor", "score": "Points", "max": "Out of", "detail": "Why"})
    tips = pd.DataFrame({"Good signs": pd.Series(r["strengths"], dtype=object), "Worries": pd.Series(r["concerns"], dtype=object),
                         "How to improve": pd.Series(r["advice"], dtype=object)})
    sheets = {"Summary": summary, "Month by month": r["table"], "Score breakdown": factors, "Tips": tips}
    ig = r.get("integrity")
    if ig:
        sheets["Balance check"] = pd.DataFrame([{"Result": ig["status"].upper(), "Message": ig["message"]}] + [
            {"Result": "Mismatch", "Message": f"{e['date']} {e['narration']}: expected {e['expected']:,.2f}, statement shows {e['found']:,.2f}"} for e in ig["examples"]])
    if r.get("flags"):
        sheets["Unusual patterns"] = pd.DataFrame([{"Severity": f["severity"], "Pattern": f["title"], "Details": f["detail"], "Examples": "; ".join(f["examples"])} for f in r["flags"]])
    fc = r.get("forecast")
    if fc and fc.get("available"):
        sheets["Forecast"] = pd.DataFrame(fc["rows"]).assign(Note=[fc["note"]] + [""] * (len(fc["rows"]) - 1))
    cats = r.get("categories")
    if cats:
        if cats["spending"]:
            sheets["Spending by category"] = pd.DataFrame(cats["spending"]).rename(columns={"category": "Category", "amount": "Total", "count": "Transactions", "share_pct": "% of spending"})
            sheets["Spending by month"] = cats["spending_by_month"].reset_index()
        if cats["income"]:
            sheets["Income by category"] = pd.DataFrame(cats["income"]).rename(columns={"category": "Category", "amount": "Total", "count": "Transactions", "share_pct": "% of income"})
    return core.dfs_to_excel(sheets)


def categories_json(cats: dict | None) -> dict | None:
    """Make the category result safe to send to the browser."""
    if not cats:
        return None
    def by_month(piv):
        if piv is None:
            return None
        return {"months": [str(c) for c in piv.columns], "rows": [{"category": str(i), "values": [float(v) for v in row]} for i, row in zip(piv.index, piv.values)]}
    return {"spending": cats["spending"], "income": cats["income"],
            "spending_by_month": by_month(cats.get("spending_by_month")), "income_by_month": by_month(cats.get("income_by_month"))}
