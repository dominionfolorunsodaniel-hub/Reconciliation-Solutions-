"""'Tell me everything in this file': a complete, deterministic read-out. No AI, no sampling, no row left out."""
from __future__ import annotations

import re

import pandas as pd

WANTS_ALL = re.compile(r"\b(everything|all (of )?(the )?(rows|records|transactions|data|entries|details)|every (row|record|transaction|entry)|"
                       r"list (all|every)|show (me )?(all|every)|full (list|file|content|detail|report|breakdown)|whole file|entire file|"
                       r"what('?s| is) (in|inside) (this|the) (file|report))\b", re.I)


def wants_everything(question: str) -> bool:
    return bool(WANTS_ALL.search(question or ""))


def describe(df: pd.DataFrame) -> str:
    d = df.drop(columns=["_rows"], errors="ignore")
    lines = [f"This file has {len(d):,} rows and {len(d.columns)} columns: {', '.join(map(str, d.columns))}. Every row is listed in the table below."]
    for c in d.columns:
        s = d[c]
        blank = int(s.isna().sum() + (s.astype(str).str.strip() == "").sum() - s.isna().sum() * 0)
        blank = int(s.isna().sum()) + int((s.dropna().astype(str).str.strip() == "").sum())
        part = f"- {c}: {len(d) - blank:,} filled, {blank:,} blank"
        num = pd.to_numeric(s, errors="coerce") if not pd.api.types.is_datetime64_any_dtype(s) else None
        if num is not None and num.notna().sum() and num.notna().sum() >= 0.8 * max(len(d) - blank, 1):
            part += f"; total {num.sum():,.2f}; lowest {num.min():,.2f}; highest {num.max():,.2f}; average {num.mean():,.2f} (over {int(num.notna().sum()):,} values)"
        elif pd.api.types.is_datetime64_any_dtype(s) and s.notna().any():
            part += f"; from {s.min():%Y-%m-%d} to {s.max():%Y-%m-%d}"
        else:
            part += f"; {s.nunique(dropna=True):,} different values"
        lines.append(part)
        if re.search(r"ref|rrn|txn|trx|\bid\b", str(c), re.I):
            dup = s[s.notna() & s.astype(str).str.strip().ne("") & s.duplicated(keep=False)]
            if len(dup):
                lines.append(f"  Repeated values in {c}: " + ", ".join(map(str, sorted(set(dup.astype(str))))))
    return "\n".join(lines)
