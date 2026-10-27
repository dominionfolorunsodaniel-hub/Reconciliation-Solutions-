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
    return max(named or cands, key=lambda c: d[c].notna().sum())


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
    """Returns (monthly DataFrame, prepared transactions DataFrame, notes)."""
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
    if x.empty:
        raise ValueError("No usable transactions were found (each row needs a date, an amount and credit/debit).")
    if len(x) > 1 and x[dc].iloc[0] > x[dc].iloc[-1]:  # newest-first statement: put it in time order
        x = x.iloc[::-1].reset_index(drop=True)
    x["Month"] = x[dc].dt.strftime("%Y-%m")
    if from_month:
        x = x[x["Month"] >= from_month]
    if to_month:
        x = x[x["Month"] <= to_month]
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
            row["Closing balance"] = round(float(sub[bc].dropna().iloc[-1]), 2) if sub[bc].notna().any() else None
        rows.append(row)
    table = pd.DataFrame(rows)
    notes = [money_note] if money_note else []
    x.attrs.update({"date_col": dc, "balance_col": bc, "narration_col": nc})
    return table, x, notes


def _clip(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def score_statement(table: pd.DataFrame, x: pd.DataFrame) -> dict:
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

    mx = sum(f["max"] for f in factors)
    score = int(round(100 * sum(f["score"] for f in factors) / mx)) if mx else 0
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
        "totals": {"months": n, "income": round(total_inc, 2), "expenses": round(total_exp, 2), "net": round(total_inc - total_exp, 2),
                   "avg_income": round(avg_inc, 2), "avg_expenses": round(avg_exp, 2), "avg_left_over": round(avg_net, 2),
                   "kept_pct": round(sr * 100, 1)},
        "disclaimer": DISCLAIMER,
    }


def build(df: pd.DataFrame, from_month: str | None = None, to_month: str | None = None) -> dict:
    table, x, notes = monthly_table(df, from_month, to_month)
    out = score_statement(table, x)
    out.update({"table": table, "notes": notes, "period": f"{table['Month'].iloc[0]} to {table['Month'].iloc[-1]}",
                "months_available": sorted(set(pd.period_range(
                    x["Month"].min(), x["Month"].max(), freq="M").astype(str)))})
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
    return core.dfs_to_excel({"Summary": summary, "Month by month": r["table"], "Score breakdown": factors, "Tips": tips})
