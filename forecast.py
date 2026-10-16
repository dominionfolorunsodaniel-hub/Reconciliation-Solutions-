"""
forecast.py - ATM cash demand forecast and refill dates. Pure pandas (no AI), so it is exact and repeatable.

Method: per ATM, daily withdrawals -> for each future day, the average of the same weekday over the last
4 weeks (falls back to the last-7-day average when history is short). Failed transactions are ignored.
"""
import re
from datetime import timedelta

import pandas as pd

from anomaly import AMOUNT_RE, FAIL_RE

ENTITY_RE = re.compile(r"atm|terminal|machine|device|tid|site|location|branch", re.I)
BALANCE_RE = re.compile(r"balance|remaining|cash.?(left|on.?hand|level)|cassette|loaded", re.I)


def detect(df: pd.DataFrame) -> dict:
    dates = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    texts = [c for c in df.columns if c not in dates and c not in nums]
    if not dates:
        raise ValueError("I need a date column to forecast.")
    balance = next((c for c in nums if BALANCE_RE.search(str(c))), None)
    amount = next((c for c in nums if c != balance and AMOUNT_RE.search(str(c)) and not BALANCE_RE.search(str(c))), None)
    if amount is None:
        raise ValueError("I couldn't find the withdrawal amount column.")
    cands = [c for c in texts if 2 <= df[c].nunique() <= max(len(df) / 5, 2)]
    entity = next((c for c in cands if ENTITY_RE.search(str(c))), cands[0] if cands else None)
    if entity is None:
        raise ValueError("I couldn't find an ATM/terminal column.")
    status = next((c for c in texts if c != entity and 2 <= df[c].nunique() <= 20
                   and df[c].dropna().astype(str).str.contains(FAIL_RE).any()), None)
    kind = next((c for c in texts if c not in (entity, status) and 2 <= df[c].nunique() <= 20
                 and df[c].dropna().astype(str).str.contains("withdraw", case=False).any()), None)
    return {"date": dates[0], "amount": amount, "entity": entity, "status": status, "kind": kind, "balance": balance}


def forecast(df: pd.DataFrame, horizon: int = 7, cash: float | None = None, buffer: float = 0.15):
    """Return (table, info). `cash` = cash loaded per ATM; if the report has a balance column that wins."""
    cols = detect(df)
    d = df.dropna(subset=[cols["date"], cols["entity"]]).copy()
    stock = None
    if cols["balance"]:
        stock = d.sort_values(cols["date"]).groupby(cols["entity"])[cols["balance"]].last()
    if cols["status"]:
        d = d[~d[cols["status"]].astype(str).str.contains(FAIL_RE, na=False)]
    if cols["kind"]:
        w = d[cols["kind"]].astype(str).str.contains("withdraw", case=False, na=False)
        d = d[w] if w.any() else d
    d["_day"] = d[cols["date"]].dt.normalize()
    daily = d.groupby([cols["entity"], "_day"])[cols["amount"]].sum().astype(float).unstack(0).fillna(0.0)
    if daily.empty:
        raise ValueError("No successful withdrawals found to forecast from.")
    daily = daily.reindex(pd.date_range(daily.index.min(), daily.index.max()), fill_value=0.0)
    if len(daily) < 7:
        raise ValueError("I need at least 7 days of history to forecast.")
    recent, last_day = daily.tail(28), daily.index.max()

    rows = []
    for atm in daily.columns:
        s = recent[atm]

        def day_fc(day, s=s):
            same = s[s.index.dayofweek == day.dayofweek]
            return float(same.tail(4).mean()) if len(same) >= 2 else float(s.tail(7).mean())

        fc = [day_fc(last_day + timedelta(days=i)) for i in range(1, horizon + 1)]
        row = {
            cols["entity"]: atm,
            "Avg daily (last 7d)": round(float(s.tail(7).mean()), 2),
            f"Forecast next {horizon}d": round(sum(fc), 2),
            f"Suggested cash (+{buffer:.0%})": round(sum(fc) * (1 + buffer), 2),
        }
        on_hand = float(stock[atm]) if stock is not None and pd.notna(stock.get(atm)) else cash
        if on_hand is not None:
            used, lasts = 0.0, None
            for i in range(1, 31):
                used += day_fc(last_day + timedelta(days=i))
                if used >= on_hand:
                    lasts = i - 1
                    break
            row["Cash on hand"] = round(on_hand, 2)
            row["Days of cash left"] = lasts if lasts is not None else ">30"
            row["Cash lasts until"] = (f"{last_day + timedelta(days=lasts):%Y-%m-%d}" if lasts is not None else ">30 days")
        rows.append(row)

    out = pd.DataFrame(rows)
    if "Days of cash left" in out:
        order = pd.to_numeric(out["Days of cash left"], errors="coerce").fillna(99)
        out = out.loc[order.sort_values().index]
    else:
        out = out.sort_values(f"Forecast next {horizon}d", ascending=False)
    info = (f"Based on data up to {last_day:%Y-%m-%d}: ATM column '{cols['entity']}', amount '{cols['amount']}'"
            + (f", balance '{cols['balance']}'" if cols["balance"] else "") + ".")
    return out.reset_index(drop=True), info
