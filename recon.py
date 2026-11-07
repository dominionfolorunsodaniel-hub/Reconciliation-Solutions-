"""
recon.py - reconcile two reports, e.g. ATM switch log vs bank ledger, POS sales vs settlement.

Gemini (or a name-based fallback) only decides WHICH columns correspond; the matching and
all amounts are computed by pandas, so the result is exact.
"""
import asyncio
import json
import re

import pandas as pd

import core

MONEY_RE = re.compile(r"amount|amt|value|total|sum|debit|credit", re.I)
STATUSES = ["Matched", "Amount mismatch", "Only in A", "Only in B"]

RECON_PROMPT = """You map columns between two reports so they can be reconciled (matched row by row).
Reply with ONE JSON object and nothing else:
{"keys": [{"a": "<column in A>", "b": "<column in B>"}],
 "amount": {"a": "<money column in A>", "b": "<money column in B>"} or null,
 "tolerance": 0.01,
 "clarify": null}
keys = the column(s) that identify the SAME transaction in both reports. Reference numbers can be named very differently
(Reference, Ref No, RRN, STAN, Transaction ID, Session ID, Auth code...). Prefer a unique id; add date or terminal only if no unique id exists.
The user's note can be written in any wording or with spelling mistakes ("match ref with rrn", "ignore small differences under 10 naira",
"compare by date and amount"). Follow it. A tolerance is the biggest money difference to ignore, in naira.
Use ONLY exact column names from the lists. ALWAYS give your best mapping. Never ask the user a question; leave "clarify" null."""

REF_RE = re.compile(r"\b(ref(erence)?s?|rrn|stan|txn|trx|tran(saction)?s?\s*(id|no|num(ber)?|ref)?|session\s*id|auth(orization)?\s*code|order\s*(id|no)|receipt\s*(no|num(ber)?)|id)\b", re.I)
DATE_RE_NAME = re.compile(r"date|time|\bday\b", re.I)
TERM_RE = re.compile(r"terminal|\btid\b|\batm\b|\bpos\b|machine|device|merchant", re.I)
TOL_RE = re.compile(r"(?:under|below|within|less than|upto|up to|tolerance(?: of)?|ignore(?: differences?)?(?: of)?)\s*(?:₦|ngn|naira|n)?\s*([\d,]+(?:\.\d+)?)", re.I)
MATCH_RE = re.compile(r"match\s+(.+?)\s+(?:with|to|and|against|vs\.?)\s+(.+?)(?=\s+and\s+match|\s*[,;.]|\s+and\s+|$)", re.I)


def parse_note(note: str, a: pd.DataFrame, b: pd.DataFrame) -> dict:
    """Understand the simple things people write in the note box, without any AI: a tolerance and 'match X with Y' pairs."""
    out = {"tolerance": None, "pairs": []}
    note = note or ""
    m = TOL_RE.search(note)
    if m:
        try:
            out["tolerance"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    for x, y in MATCH_RE.findall(note):
        ca, cb = core.resolve_column(x.strip(" '\""), list(a.columns)), core.resolve_column(y.strip(" '\""), list(b.columns))
        if ca in a.columns and cb in b.columns:
            out["pairs"].append((ca, cb))
    return out


def _pair_by(a: pd.DataFrame, b: pd.DataFrame, rx, numeric: bool | None = None):
    """Columns of A and B that both look like the same kind of thing (an id, a date, a terminal...). Same names first."""
    ca = [c for c in a.columns if rx.search(str(c)) and (numeric is None or pd.api.types.is_numeric_dtype(a[c]) == numeric)]
    cb = [c for c in b.columns if rx.search(str(c)) and (numeric is None or pd.api.types.is_numeric_dtype(b[c]) == numeric)]
    low_b = {str(c).lower(): c for c in cb}
    same = [(c, low_b[str(c).lower()]) for c in ca if str(c).lower() in low_b]
    pairs = same + [(x, y) for x in ca for y in cb if (x, y) not in same]
    seen, out = set(), []
    for x, y in pairs:
        if x not in seen:
            seen.add(x)
            out.append((x, y))
    return out


def _unique_share(df: pd.DataFrame, cols) -> float:
    return _keycol(df, list(cols)).nunique() / max(len(df), 1)


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


def _money_pair(a: pd.DataFrame, b: pd.DataFrame, taken=()):
    pairs = [p for p in _pair_by(a, b, MONEY_RE, numeric=True) if p[0] not in taken]
    return pairs[0] if pairs else None


def guess_plan(a: pd.DataFrame, b: pd.DataFrame, note: str = "") -> dict:
    """Works out the matching without AI: the user's note first, then same-named columns, then columns that mean the same
    thing under different names (Reference / Ref No / RRN), then date + amount (+ terminal). Never gives up while a sensible guess exists."""
    info = parse_note(note, a, b)
    tol = info["tolerance"] if info["tolerance"] is not None else 0.01
    chosen = list(info["pairs"])
    money = _money_pair(a, b, taken=[p[0] for p in chosen])
    # pairs the user named that are money columns become the amount, not a key
    keys = [p for p in chosen if not (pd.api.types.is_numeric_dtype(a[p[0]]) and MONEY_RE.search(str(p[0])))]
    for p in chosen:
        if p not in keys:
            money = p
    if keys:
        return {"keys": [{"a": x, "b": y} for x, y in keys], "amount": {"a": money[0], "b": money[1]} if money else None, "tolerance": tol,
                "how": "your note"}
    low_b = {str(c).lower(): c for c in b.columns}
    common = [(c, low_b[str(c).lower()]) for c in a.columns if str(c).lower() in low_b]
    num = [p for p in common if pd.api.types.is_numeric_dtype(a[p[0]]) and pd.api.types.is_numeric_dtype(b[p[1]])]
    money = next((p for p in num if MONEY_RE.search(str(p[0]))), None) or _money_pair(a, b)
    attempts = []
    cands = [p for p in common if p != money and not pd.api.types.is_datetime64_any_dtype(a[p[0]])]
    cands.sort(key=lambda p: _unique_share(a, [p[0]]), reverse=True)
    attempts.append(("columns with the same names", cands))
    refs = [p for p in _pair_by(a, b, REF_RE) if p != money]
    refs.sort(key=lambda p: _unique_share(a, [p[0]]), reverse=True)
    attempts.append(("reference-style columns", refs))
    for label, group in attempts:
        for k in range(1, min(3, len(group)) + 1):
            sel = group[:k]
            if _unique_share(a, [p[0] for p in sel]) >= 0.9:
                return {"keys": [{"a": x, "b": y} for x, y in sel], "amount": {"a": money[0], "b": money[1]} if money else None,
                        "tolerance": tol, "how": f"automatically, from {label}"}
    # no unique id anywhere: combine date + amount (+ terminal)
    dates, terms = _pair_by(a, b, DATE_RE_NAME), _pair_by(a, b, TERM_RE)
    if money and dates:
        sel = [dates[0], money] + ([terms[0]] if terms else [])
        return {"keys": [{"a": x, "b": y} for x, y in sel], "amount": None, "tolerance": tol,
                "how": "automatically, from date + amount" + (" + terminal" if terms else "") + " (the reports have no unique reference, so similar transactions may pair up wrongly)"}
    raise ValueError("I could not find which column in the first report is the same thing as a column in the second. "
                     "Add a note such as: match Reference with Ref No and Amount with Value.")


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



def _add_evidence(m: pd.DataFrame, status: pd.Series, keys: list, amt) -> pd.DataFrame:
    """Adds two plain-language columns, worked out only from the rows themselves (no AI):
    Confidence - how strong the evidence for the match is; Finding - what kind of difference it is."""
    strong = any(REF_RE.search(str(k.get("a", ""))) or REF_RE.search(str(k.get("b", ""))) for k in keys)
    n_keys = len(keys)
    conf = pd.Series("", index=m.index, dtype=object)
    finding = pd.Series("", index=m.index, dtype=object)
    both = (m["_merge"] == "both").to_numpy()
    conf[both] = "High" if strong else ("Medium" if n_keys >= 2 else "Low")
    if "Difference (A-B)" in m.columns:
        av = pd.to_numeric(m[f"A: {amt['a']}"], errors="coerce").astype(float)
        bv = pd.to_numeric(m[f"B: {amt['b']}"], errors="coerce").astype(float)
        mism = (status == "Amount mismatch").to_numpy()
        conf[mism] = "Medium" if strong else "Low"
        part = mism & (bv > 0).to_numpy() & (av > bv).to_numpy()
        over = mism & (bv > av).to_numpy()
        finding[part] = "Possible partial settlement (B is less than A)"
        finding[over] = "B is more than A (possible overpayment)"
        finding[mism & ~part & ~over] = "Amount differs"
    # the same key present in both reports but counted more times in one of them = likely duplicate
    for side, tag in (("A: ", "Only in A"), ("B: ", "Only in B")):
        only = (status == tag).to_numpy()
        if only.any():
            matched_keys = set(m.loc[both, "_key"].astype(str))
            dup = only & m["_key"].astype(str).isin(matched_keys).to_numpy()
            finding[dup] = f"Possible duplicate in report {tag[-1]} (same key already matched)"
            miss = only & ~dup
            finding[miss] = f"Missing from report {'B' if tag[-1] == 'A' else 'A'}"
    m.insert(1, "Confidence", conf)
    m.insert(2, "Finding", finding)
    return m

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
    grouped = pd.Series(False, index=m.index)
    if amt:
        try:
            grouped = _group_matches(m, status, amt, tol)
            status[grouped] = "Matched"
        except Exception:  # noqa: BLE001  grouping is a bonus; fall back to plain one-to-one results
            grouped = pd.Series(False, index=m.index)
    m.insert(0, "Status", status)
    try:
        m = _add_evidence(m, status, keys, amt)
    except Exception:  # noqa: BLE001  evidence columns are a bonus; never let them break a reconciliation
        pass
    try:
        m = _flag_reversals(m)
    except Exception:  # noqa: BLE001
        pass
    if grouped.any() and "Finding" in m.columns:
        m.loc[grouped, "Finding"] = "Grouped match: several rows in one report add up to the amount in the other"
        m.loc[grouped, "Confidence"] = "Medium"
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
    out = {"summary": pd.DataFrame(rows), "detail": m, "plan": plan, "grouped_rows": int(grouped.sum())}
    try:
        out["settlement"] = settlement_report(m, amt)
    except Exception:  # noqa: BLE001
        pass
    try:
        out["warnings"] = _currency_warnings(a, b)
    except Exception:  # noqa: BLE001
        pass
    try:
        out["quality"] = quality_score(out)
        out["exposure"] = exposure(out)
    except Exception:  # noqa: BLE001  extras must never break a reconciliation
        pass
    return out


async def reconcile(a: pd.DataFrame, b: pd.DataFrame, instruction: str = "") -> dict:
    """Returns run_recon's dict. The AI proposes the column mapping; if it is unavailable, unsure or wrong, the built-in logic
    works it out from the note and the column names. A question comes back only when no sensible matching exists at all."""
    # Bank statement vs ledger (reference + credit/debit or amount+type columns on both sides): deterministic engine.
    try:
        import bankrec
        if bankrec.applicable(a, b):
            tol = parse_note(instruction, a, b).get("tolerance")
            return await asyncio.to_thread(bankrec.reconcile_bank_ledger, a, b, 0.01 if tol is None else tol)
    except Exception:  # noqa: BLE001  fall back to the general engine
        pass
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
        if _valid(data, a, b):
            plan = data
            plan["how"] = "by the AI, from your columns" + (" and your note" if instruction else "")
            info = parse_note(instruction, a, b)
            if info["tolerance"] is not None:
                plan["tolerance"] = info["tolerance"]
    except Exception:  # no API key / bad reply -> built-in logic
        plan = None
    if plan is None:
        try:
            plan = guess_plan(a, b, instruction)
        except ValueError as e:
            return {"clarify": str(e)}
    out = await asyncio.to_thread(run_recon, a, b, plan)
    out["how"] = describe_plan(plan)
    return out


def describe_plan(plan: dict) -> str:
    keys = " + ".join(f"{k['a']} (A) = {k['b']} (B)" for k in plan["keys"])
    amt = plan.get("amount")
    tol = float(plan.get("tolerance") or 0.01)
    txt = f"HOW I MATCHED THEM ({plan.get('how', 'automatically')}): rows are the same transaction when {keys}."
    if amt:
        txt += f" Amounts compared: {amt['a']} (A) with {amt['b']} (B); differences up to {tol:g} are ignored."
    return txt


def summary_text(result: dict, label_a: str, label_b: str) -> str:
    if result.get("report_text"):
        return result["report_text"].replace(result["bank_name"], label_a, 1).replace(result["ledger_name"], label_b, 1)
    base = _summary_text(result, label_a, label_b)
    return (result["how"] + "\n\n" + base) if result.get("how") else base


def _summary_text(result: dict, label_a: str, label_b: str) -> str:
    plan = result["plan"]
    keys = ", ".join(f'{k["a"]} = {k["b"]}' for k in plan["keys"])
    amt = plan.get("amount")
    head = f"Reconciliation {label_a} (A) vs {label_b} (B)\nMatched on: {keys}"
    if amt:
        head += f"\nAmount: {amt['a']} vs {amt['b']} (tolerance {plan.get('tolerance') or 0.01})"
    table = result["summary"].to_string(index=False, float_format=lambda x: f"{x:,.2f}")
    return head + "\n\n" + table


def likely_causes(result: dict) -> list[str]:
    """Plain-English guesses about WHY rows differ, found by comparing the numbers (never invented).
    Each line is a possibility to check, not a proven cause."""
    out = []
    if result.get("engine") == "bank-ledger":
        return out
    try:
        d, plan = result["detail"], result["plan"]
        amt = plan.get("amount")
        mism = d[d["Status"] == "Amount mismatch"]
        if len(mism) and "Difference (A-B)" in d.columns:
            diffs = mism["Difference (A-B)"].round(2)
            top = diffs.value_counts()
            if len(top) and top.iloc[0] / len(diffs) >= 0.6 and len(diffs) >= 2:
                out.append(f"{int(top.iloc[0])} of the {len(diffs)} amount differences are exactly {abs(float(top.index[0])):,.2f} "
                           f"({'A is higher' if top.index[0] > 0 else 'B is higher'}). A repeated amount like this is usually a fee, "
                           "charge or VAT added in only one of the systems.")
            if amt:
                base = pd.to_numeric(mism[f"A: {amt['a']}"], errors="coerce").abs().replace(0, float("nan"))
                small = (diffs.abs() / base < 0.01).sum()
                if small >= max(2, len(diffs) * 0.5):
                    out.append(f"{int(small)} differences are smaller than 1% of the amount, which often means rounding.")
            out.append(f"{len(mism)} transaction(s) were found in both reports but with different amounts. Check these first; "
                       "they are the easiest to fix.")
        oa, ob = d[d["Status"] == "Only in A"], d[d["Status"] == "Only in B"]
        if amt and len(oa) and len(ob):
            ca = pd.to_numeric(oa[f"A: {amt['a']}"], errors="coerce").round(2).dropna()
            cb = pd.to_numeric(ob[f"B: {amt['b']}"], errors="coerce").round(2).dropna()
            shared = set(ca) & set(cb)
            pairs = int(min(ca.isin(shared).sum(), cb.isin(shared).sum()))
            if pairs:
                out.append(f"About {pairs} item(s) only in A have an item only in B with the same amount. They may be the SAME "
                           "transaction recorded with a different reference, so look at the date and narration to confirm.")
        if len(oa) and not len(ob):
            out.append(f"{len(oa)} item(s) exist only in A and nothing is extra in B. A may include items B has not recorded yet "
                       "(timing) or B may have missed them.")
        if len(ob) and not len(oa):
            out.append(f"{len(ob)} item(s) exist only in B and nothing is extra in A. B may include items A has not recorded yet "
                       "(timing) or A may have missed them.")
        for side, label in (("A", "A"), ("B", "B")):
            col = [c for c in d.columns if c.startswith(f"{side}: ")]
            if col and d["Match key"].duplicated(keep=False).any():
                pass  # repeated keys are matched one-to-one already
        dup_a = int(d[d["Status"] == "Only in A"]["Match key"].duplicated().sum())
        if dup_a:
            out.append(f"{dup_a} item(s) in A repeat the same reference. Check for double entries.")
    except Exception:  # noqa: BLE001  (hints are a bonus)
        return out
    return out


_REV_WORDS = re.compile(r"revers|refund|chargeback|charge ?back|failed|declined|unsuccessful|cancel|void|returned", re.I)
_TEXT_COLS = re.compile(r"narrat|descri|remark|detail|status|type|response|particular|memo|message", re.I)


def _flag_reversals(m: pd.DataFrame) -> pd.DataFrame:
    """Rows whose own text says reversal / refund / failed / cancelled. Flagged for review (not decided by AI)."""
    cols = [c for c in m.columns if _TEXT_COLS.search(str(c)) and str(c).startswith(("A: ", "B: "))]
    if not cols or "Finding" not in m.columns:
        return m
    text = m[cols].astype(str).agg(" ".join, axis=1)
    hit = text.str.contains(_REV_WORDS.pattern, flags=re.I, regex=True) & (m["Status"] != "Matched")
    note = "Possible reversal, refund or failed transaction (the description says so)"
    m.loc[hit, "Finding"] = note + m.loc[hit, "Finding"].map(lambda f: f"; {f}" if f else "")
    return m


def _amount_cols(d: pd.DataFrame):
    a = [c for c in d.columns if str(c).startswith("A: ")]
    return a


def _group_matches(m: pd.DataFrame, status: pd.Series, amt: dict, tol: float) -> pd.Series:
    """Many-to-one / one-to-many: for a key, the rows in A and in B are different in number but add up to the same total."""
    av = pd.to_numeric(m[f"A: {amt['a']}"], errors="coerce")
    bv = pd.to_numeric(m[f"B: {amt['b']}"], errors="coerce")
    g = pd.DataFrame({"k": m["_key"].astype(str), "a": av, "b": bv, "na": av.notna().astype(int), "nb": bv.notna().astype(int)})
    tot = g.groupby("k").agg(a=("a", "sum"), b=("b", "sum"), na=("na", "sum"), nb=("nb", "sum"))
    ok = tot[(tot["na"] != tot["nb"]) & (tot["na"] > 0) & (tot["nb"] > 0) & ((tot["a"] - tot["b"]).abs() <= max(tol, 0.01))].index
    return (g["k"].isin(ok) & (status != "Matched")).reset_index(drop=True).set_axis(m.index)


def _currency_warnings(a: pd.DataFrame, b: pd.DataFrame) -> list:
    out = []
    for label, df in (("A", a), ("B", b)):
        for c in df.columns:
            if re.search(r"currenc|\bccy\b|\bcur\b", str(c), re.I):
                vals = sorted(set(df[c].dropna().astype(str).str.strip().str.upper()) - {""})
                if len(vals) > 1:
                    out.append(f"Report {label} has more than one currency ({', '.join(vals[:5])}). Totals add them together without converting, so check them per currency.")
    return out


def settlement_report(m: pd.DataFrame, amt) -> pd.DataFrame | None:
    """Daily expected-versus-settled view. Expected = report A (e.g. POS/switch), settled = report B (e.g. bank).
    Needs a date column in both reports. Delay = days between the date in A and the date in B for matched rows."""
    if not amt:
        return None
    pick = lambda pre: next((c for c in m.columns if str(c).startswith(pre) and re.search(r"date|time|settle|posted", str(c), re.I)), None)  # noqa: E731
    da, db_ = pick("A: "), pick("B: ")
    if not da:
        return None
    d_a = pd.to_datetime(m[da], errors="coerce", dayfirst=True)
    av = pd.to_numeric(m[f"A: {amt['a']}"], errors="coerce")
    bv = pd.to_numeric(m[f"B: {amt['b']}"], errors="coerce")
    both = m["Status"].isin(["Matched", "Amount mismatch"]) & av.notna() & bv.notna()
    t = pd.DataFrame({"Day": d_a.dt.strftime("%Y-%m-%d"), "exp": av.where(av.notna(), 0.0), "set": bv.where(both, 0.0), "has": av.notna()})
    t = t[t["Day"].notna() & t["has"]]
    if t.empty:
        return None
    day = t.groupby("Day").agg(Expected=("exp", "sum"), Settled=("set", "sum"), Transactions=("has", "sum")).reset_index()
    day["Outstanding"] = (day["Expected"] - day["Settled"]).round(2)
    day["Expected"], day["Settled"] = day["Expected"].round(2), day["Settled"].round(2)
    if db_:
        d_b = pd.to_datetime(m[db_], errors="coerce", dayfirst=True)
        delay = ((d_b - d_a).dt.days).where(both)
        dd = pd.DataFrame({"Day": d_a.dt.strftime("%Y-%m-%d"), "delay": delay}).dropna()
        if len(dd):
            ag = dd.groupby("Day")["delay"].agg(["mean", "max"]).round(1).reset_index().rename(columns={"mean": "Average delay (days)", "max": "Longest delay (days)"})
            day = day.merge(ag, on="Day", how="left")
    day["Transactions"] = day["Transactions"].astype(int)
    return day


def exposure(result: dict) -> dict:
    """How much money is affected by what did not match. Plain sums of the rows; no AI."""
    s = result["summary"].set_index("Status")
    if "Amount A" not in s.columns:
        return {}
    g = lambda st, col: float(s.loc[st, col]) if st in s.index else 0.0  # noqa: E731
    mismatch = abs(g("Amount mismatch", "Difference"))
    only_a, only_b = g("Only in A", "Amount A"), g("Only in B", "Amount B")
    return {"mismatch_value": round(mismatch, 2), "only_in_a_value": round(only_a, 2), "only_in_b_value": round(only_b, 2),
            "total_at_risk": round(mismatch + abs(only_a) + abs(only_b), 2)}


def quality_score(result: dict) -> dict:
    """0-100 score from the evidence in the rows. Each factor says why it got its points."""
    d, s = result["detail"], result["summary"].set_index("Status")
    total = max(int(len(d)), 1)
    cnt = lambda st: int(s.loc[st, "Count"]) if st in s.index else 0  # noqa: E731
    matched, mism, oa, ob = cnt("Matched"), cnt("Amount mismatch"), cnt("Only in A"), cnt("Only in B")
    find = d["Finding"].astype(str) if "Finding" in d.columns else pd.Series([""] * len(d))
    dups = int(find.str.contains("duplicate", case=False).sum())
    revs = int(find.str.contains("reversal", case=False).sum())
    exceptions = mism + oa + ob
    ratio = exceptions / total
    factors = []

    def add(name, pts, mx, detail):
        factors.append({"name": name, "score": round(pts, 1), "max": mx, "detail": detail})
    add("Matching accuracy", 30 * matched / total, 30, f"{matched:,} of {total:,} rows matched ({matched / total * 100:.1f}%).")
    pts = 15 if exceptions == 0 else 12 if ratio <= 0.02 else 8 if ratio <= 0.10 else 4 if ratio <= 0.25 else 0
    add("Unresolved exceptions", pts, 15, f"{exceptions:,} row(s) still need a decision ({ratio * 100:.1f}% of rows).")
    add("Missing records", 20 * (1 - min(1.0, (oa + ob) / total * 2)), 20, f"{oa:,} only in report A and {ob:,} only in report B.")
    add("Duplicate detection", 15 * (1 - min(1.0, dups / total * 5)), 15, f"{dups:,} possible duplicate row(s) found and flagged." if dups else "No duplicates found.")
    add("Reversal identification", 10 * (1 - min(1.0, revs / total * 5)), 10, f"{revs:,} reversal/refund/failed row(s) identified for review." if revs else "No reversal-type rows found.")
    ok = int(s["Count"].sum()) == len(d)
    if ok and "Amount A" in s.columns:
        ok = abs(float(s["Difference"].sum()) - (float(s["Amount A"].sum()) - float(s["Amount B"].sum()))) < 0.05
    add("Calculation consistency", 10 if ok else 0, 10, "Counts and amounts add up between the summary and the details." if ok else "The summary does not add up to the details. Do not rely on this result.")
    score = round(sum(f["score"] for f in factors))
    grade = "Excellent" if score >= 90 else "Good" if score >= 75 else "Needs attention" if score >= 50 else "Poor"
    return {"score": int(score), "grade": grade, "factors": factors}


def to_excel(result: dict):
    if result.get("engine") == "bank-ledger":
        import bankrec
        return core.dfs_to_excel(bankrec.sheets(result))
    d, s = result["detail"], result["summary"]
    cut = lambda st: d[d["Status"] == st].dropna(axis=1, how="all")  # noqa: E731
    extra = {}
    if result.get("quality"):
        q = result["quality"]
        extra["Quality score"] = pd.DataFrame([{"Factor": f["name"], "Points": f["score"], "Out of": f["max"], "Why": f["detail"]} for f in q["factors"]]
                                              + [{"Factor": f"TOTAL ({q['grade']})", "Points": q["score"], "Out of": 100, "Why": ""}])
    if result.get("settlement") is not None and len(result["settlement"]):
        extra["Daily settlement"] = result["settlement"]
    if result.get("exposure"):
        e = result["exposure"]
        extra["Financial exposure"] = pd.DataFrame([["Value of amount mismatches", e["mismatch_value"]], ["Value only in report A", e["only_in_a_value"]],
                                                    ["Value only in report B", e["only_in_b_value"]], ["Total value affected", e["total_at_risk"]]], columns=["Item", "Amount"])
    return core.dfs_to_excel({
        "Summary": s, **extra,
        "Amount mismatches": cut("Amount mismatch"),
        "Only in A": cut("Only in A"),
        "Only in B": cut("Only in B"),
        "Matched": cut("Matched"),
        "Plan": pd.DataFrame({"plan": [json.dumps(result["plan"], indent=2)]}),
    })
