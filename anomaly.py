"""
anomaly.py - rule-based checks that need no AI (so results are exact and repeatable):
failure-rate spikes per terminal/branch, terminals that went quiet, extreme amounts,
days with no or unusually low activity.
"""
import re

import pandas as pd

FAIL_RE = re.compile(r"fail|declin|error|reject|revers|timeout|denied|unsuccess|refus|void", re.I)
AMOUNT_RE = re.compile(r"amount|amt|value|total|fee|price|balance|cash|sum|qty|quantity", re.I)
SEVERITY_RANK = {"high": 0, "medium": 1, "info": 2}


def scan(df: pd.DataFrame, max_alerts: int = 8) -> list[dict]:
    """Return a list of {"severity": high|medium|info, "text": str}."""
    n = len(df)
    if n < 20:
        return []
    alerts: list[dict] = []
    dates = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    texts = [c for c in df.columns if c not in dates and c not in nums]

    # "Entity" columns = terminals / ATMs / branches / merchants (groups average >= 10 rows)
    entities = [c for c in texts if 2 <= df[c].nunique() <= n / 10]

    # Status column = a small text column containing failure-like words
    status = None
    for c in texts:
        vals = df[c].dropna().astype(str)
        if 2 <= vals.nunique() <= 20 and vals.str.contains(FAIL_RE).any():
            status = c
            break

    if status:
        fail = df[status].astype(str).str.contains(FAIL_RE, na=False)
        overall = float(fail.mean())
        if overall > 0:
            alerts.append({"severity": "info",
                           "text": f"Overall failure rate: {overall:.1%} ({int(fail.sum()):,} of {n:,})."})
        for e in entities:
            if e == status:
                continue
            g = pd.DataFrame({"k": df[e], "f": fail}).groupby("k")["f"].agg(["sum", "count"])
            g = g[g["count"] >= 20]
            if g.empty:
                continue
            g["rate"] = g["sum"] / g["count"]
            bad = g[g["rate"] > max(2 * overall, overall + 0.10)].sort_values("rate", ascending=False).head(3)
            for name, row in bad.iterrows():
                alerts.append({
                    "severity": "high",
                    "text": f'{e} "{name}": {row["rate"]:.0%} failures ({int(row["sum"]):,} of '
                            f'{int(row["count"]):,}) vs {overall:.0%} overall.',
                })

    for c in [c for c in nums if AMOUNT_RE.search(str(c))][:2]:
        x = pd.to_numeric(df[c], errors="coerce").dropna().astype(float)
        if len(x) < 20:
            continue
        med = float(x.median())
        mad = float((x - med).abs().median())
        if mad == 0:
            continue
        extreme = x[(0.6745 * (x - med) / mad).abs() > 8]
        if len(extreme):
            alerts.append({
                "severity": "medium",
                "text": f'"{c}": {len(extreme):,} extreme value(s); largest {extreme.abs().max():,.2f} '
                        f'vs typical {med:,.2f}.',
            })

    if dates:
        d = dates[0]
        dd = df[d].dropna()
        if len(dd) and (dd.max() - dd.min()).days >= 7:
            end = dd.max()
            for e in entities[:3]:
                last = df.groupby(e)[d].max()
                cnt = df.groupby(e)[d].count()
                quiet = last[(cnt >= 10) & (last < end - pd.Timedelta(days=3))].sort_values().head(3)
                for name, ts in quiet.items():
                    alerts.append({
                        "severity": "medium",
                        "text": f'{e} "{name}" went quiet: last activity {ts:%Y-%m-%d}, '
                                f'{(end - ts).days} days before the report ends.',
                    })
            daily = dd.dt.normalize().value_counts().sort_index()
            med = float(daily.median())
            full = pd.date_range(daily.index.min(), daily.index.max(), freq="D")
            missing = full.difference(daily.index)
            if len(missing) and med >= 5 and len(full) >= 7:
                eg = ", ".join(f"{m:%Y-%m-%d}" for m in missing[:3])
                alerts.append({"severity": "info",
                               "text": f"No activity on {len(missing)} day(s) inside the period, e.g. {eg}."})
            if med >= 10:
                for day, c in daily[daily < 0.3 * med].sort_values().head(2).items():
                    alerts.append({"severity": "info",
                                   "text": f"Unusually low volume on {day:%Y-%m-%d}: {int(c):,} rows vs ~{med:,.0f}/day."})

    alerts.sort(key=lambda a: SEVERITY_RANK[a["severity"]])
    return alerts[:max_alerts]


def push_text(company: str, filename: str, alerts: list[dict]) -> str | None:
    """Message for Telegram push notifications. Only sent when something is high severity."""
    high = [a for a in alerts if a["severity"] == "high"]
    if not high:
        return None
    lines = [f"🚨 {company} - new report {filename}"] + [f"• {a['text']}" for a in high[:5]]
    return "\n".join(lines)
