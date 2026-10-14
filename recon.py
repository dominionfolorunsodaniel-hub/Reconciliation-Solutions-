"""
recon.py - reconcile two reports, e.g. ATM switch log vs bank ledger, POS sales vs settlement.

Claude (or a name-based fallback) only decides WHICH columns correspond; the matching and
all amounts are computed by pandas, so the result is exact.
"""
import asyncio
import json
import re

import pandas as pd

import core

MONEY_RE = re.compile(r"amount|amt|value|total|sum|debit|credit", re.I)
STATUSES = ["Matched", "Amount mismatch", "Only in A", "Only in B"]

RECON_PROMPT = """You map columns between two reports so they can be reconciled.
Reply with ONE JSON object and nothing else:
{"keys": [{"a": "<column in A>", "b": "<column in B>"}],
 "amount": {"a": "<money column in A>", "b": "<money column in B>"} or null,
 "tolerance": 0.01,
 "clarify": null or "a short question if no sensible mapping exists"}
keys = the column(s) that identify the SAME transaction in both reports (reference / RRN / transaction id;
add date or terminal only if no unique id exists). Use ONLY exact column names. Never guess."""


def _norm(s: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.strftime("%Y-%m-%d").fillna("")
    if pd.api.types.is_numeric_dtype(s):
        return s.map(lambda v: "" if pd.isna(v) else (str(int(v)) if float(v).is_integer() else str(round(float(v), 6))))
    t = s.fillna("").astype(str).str.strip().str.lower().str.replace(r"\s+", " ", regex=True)
    return t.str.replace(r"^0+(?=\d)", "", regex=True)


def _keycol(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    key = _norm(df[cols[0]])
    for c in cols[1:]:
        key = key + "|" + _norm(df[c])
    return key


def guess_plan(a: pd.DataFrame, b: pd.DataFrame) -> dict:
    """Fallback when Claude isn't available: match same-named columns."""
    low_b = {str(c).lower(): c for c in b.columns}
    common = [(c, low_b[str(c).lower()]) for c in a.columns if str(c).lower() in low_b]
    if not common:
        raise ValueError(
            "The two reports share no column names. Say which columns match, e.g. "
            "/recon 3 4 match Reference with Ref No and Amount with Value."
        )
    num = [p for p in common if pd.api.types.is_numeric_dtype(a[p[0]]) and pd.api.types.is_numeric_dtype(b[p[1]])]
    money = next((p for p in num if MONEY_RE.search(str(p[0]))), None)
    cands = [p for p in common if p != money and not pd.api.types.is_datetime64_any_dtype(a[p[0]])]
    cands.sort(key=lambda p: a[p[0]].nunique() / max(len(a), 1), reverse=True)
    for k in range(1, min(3, len(cands)) + 1):
        sel = cands[:k]
        if _keycol(a, [p[0] for p in sel]).nunique() / max(len(a), 1) >= 0.9:
            return {
                "keys": [{"a": x, "b": y} for x, y in sel],
                "amount": {"a": money[0], "b": money[1]} if money else None,
                "tolerance": 0.01,
            }
    raise ValueError(
        "I couldn't find columns that uniquely identify a transaction. Tell me which ones, e.g. "
        "/recon 3 4 match Reference with Ref No."
    )


def _valid(plan: dict, a: pd.DataFrame, b: pd.DataFrame) -> bool:
    keys = plan.get("keys") or []
    if not keys or any(k.get("a") not in a.columns or k.get("b") not in b.columns for k in keys):
        return False
    amt = plan.get("amount")
    return not amt or (amt.get("a") in a.columns and amt.get("b") in b.columns)


def _prefixed(df: pd.DataFrame, cols: list[str], prefix: str) -> pd.DataFrame:
    key = _keycol(df, cols)
    out = df.add_prefix(prefix)
    out.insert(0, "_key", key.to_numpy())
    out.insert(1, "_n", key.groupby(key).cumcount().to_numpy())  # repeated keys match 1:1
    return out


def run_recon(a: pd.DataFrame, b: pd.DataFrame, plan: dict) -> dict:
    if not _valid(plan, a, b):
        raise ValueError("Those columns don't exist in the reports.")
    keys, amt = plan["keys"], plan.get("amount")
    tol = float(plan.get("tolerance") or 0.01)

    m = _prefixed(a, [k["a"] for k in keys], "A: ").merge(
        _prefixed(b, [k["b"] for k in keys], "B: "), on=["_key", "_n"], how="outer", indicator=True
    )
    status = pd.Series("Matched", index=m.index, dtype=object)
    status[(m["_merge"] == "left_only").to_numpy()] = "Only in A"
    status[(m["_merge"] == "right_only").to_numpy()] = "Only in B"

    def money(col):
        return pd.to_numeric(m[col], errors="coerce").astype(float)

    if amt:
        av, bv = money(f"A: {amt['a']}"), money(f"B: {amt['b']}")
        diff = (av - bv).round(2)
        m["Difference (A-B)"] = diff
        bad = ((m["_merge"] == "both") & (diff.abs() > tol)).to_numpy()
        status[bad] = "Amount mismatch"
    m.insert(0, "Status", status)
    m = m.drop(columns=["_merge", "_n"]).rename(columns={"_key": "Match key"})

    rows = []
    for st in STATUSES:
        part = m[m["Status"] == st]
        row = {"Status": st, "Count": int(len(part))}
        if amt:
            row["Amount A"] = round(float(pd.to_numeric(part[f"A: {amt['a']}"], errors="coerce").astype(float).sum()), 2)
            row["Amount B"] = round(float(pd.to_numeric(part[f"B: {amt['b']}"], errors="coerce").astype(float).sum()), 2)
            row["Difference"] = round(row["Amount A"] - row["Amount B"], 2)
        rows.append(row)
    return {"summary": pd.DataFrame(rows), "detail": m, "plan": plan}


async def reconcile(a: pd.DataFrame, b: pd.DataFrame, instruction: str = "") -> dict:
    """Returns run_recon's dict, or {"clarify": "..."}; raises ValueError if it can't be matched."""
    plan = None
    try:
        user_msg = (
            f"Report A columns:\n{core.describe_schema(a)}\n\nReport A sample:\n"
            f"{a.head(3).to_string(index=False, max_colwidth=22)}\n\n"
            f"Report B columns:\n{core.describe_schema(b)}\n\nReport B sample:\n"
            f"{b.head(3).to_string(index=False, max_colwidth=22)}\n\nUser note: {instruction or 'none'}"
        )
        resp = await core._ask(
            model=core.MODEL, max_tokens=500, system=RECON_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = core._text_of(resp)
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        if data.get("clarify"):
            return {"clarify": str(data["clarify"])}
        plan = data if _valid(data, a, b) else None
    except Exception:  # no API key / bad reply -> name-based fallback
        plan = None
    if plan is None:
        plan = guess_plan(a, b)
    return await asyncio.to_thread(run_recon, a, b, plan)


def summary_text(result: dict, label_a: str, label_b: str) -> str:
    plan = result["plan"]
    keys = ", ".join(f'{k["a"]} = {k["b"]}' for k in plan["keys"])
    amt = plan.get("amount")
    head = f"Reconciliation {label_a} (A) vs {label_b} (B)\nMatched on: {keys}"
    if amt:
        head += f"\nAmount: {amt['a']} vs {amt['b']} (tolerance {plan.get('tolerance') or 0.01})"
    table = result["summary"].to_string(index=False, float_format=lambda x: f"{x:,.2f}")
    return head + "\n\n" + table


def to_excel(result: dict):
    d, s = result["detail"], result["summary"]
    cut = lambda st: d[d["Status"] == st].dropna(axis=1, how="all")  # noqa: E731
    return core.dfs_to_excel({
        "Summary": s,
        "Amount mismatches": cut("Amount mismatch"),
        "Only in A": cut("Only in A"),
        "Only in B": cut("Only in B"),
        "Matched": cut("Matched"),
        "Plan": pd.DataFrame({"plan": [json.dumps(result["plan"], indent=2)]}),
    })
