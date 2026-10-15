"""
privacy.py - runs on every report right after it is read, BEFORE it is stored,
analyzed, or sent to the configured Gemini API.

- Card numbers (13-19 digits that pass the Luhn check) are masked to the last 4 digits.
- Columns named CVV / CVC / PIN / track data are dropped completely.
"""
import re

import numpy as np
import pandas as pd

CANDIDATE_RE = r"\d(?:[ -]?\d){12}"  # quick pre-filter: 13+ digits in a row
PAN_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
# Real card numbers start with a known issuer prefix (Visa, Mastercard, Amex, JCB, Discover,
# UnionPay, Verve). This avoids masking long reference numbers that merely pass the Luhn check.
IIN_RE = re.compile(r"^(4|5[1-5]|2[2-7]|3[47]|35|6011|65|62|5060|5061)")
CARD_COL_RE = re.compile(r"pan|card", re.I)
SECRET_COL_RE = re.compile(r"\b(cvv2?|cvc2?|cid|pin|pin[ _-]?block|track[ _-]?[12])\b", re.I)


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _mask(digits: str) -> str:
    return "*" * (len(digits) - 4) + digits[-4:]


def _is_pan(digits: str, hint: bool) -> bool:
    return 13 <= len(digits) <= 19 and luhn_ok(digits) and (hint or bool(IIN_RE.match(digits)))


def mask_value(v, hint: bool = False):
    """Return (new_value, number_of_cards_masked). hint=True when the column is named like a card."""
    if isinstance(v, bool):
        return v, 0
    if isinstance(v, (int, np.integer)) or (isinstance(v, float) and v == v and float(v).is_integer()):
        s = str(int(v))
        if _is_pan(s, hint):
            return _mask(s), 1
        return v, 0
    if isinstance(v, str):
        count = 0

        def repl(m):
            nonlocal count
            digits = re.sub(r"\D", "", m.group())
            if _is_pan(digits, hint):
                count += 1
                return _mask(digits)
            return m.group()

        return PAN_RE.sub(repl, v), count
    return v, 0


def sanitize(df: pd.DataFrame):
    """df has string-ish cells (before type conversion). Returns (clean_df, notes)."""
    notes = []
    secret = [c for c in df.columns if SECRET_COL_RE.search(str(c))]
    if secret:
        df = df.drop(columns=secret)
        notes.append("Removed sensitive column(s): " + ", ".join(map(str, secret)))

    masked = 0
    df = df.copy()
    for c in df.columns:
        s = df[c]
        hint = bool(CARD_COL_RE.search(str(c)))
        cand = s.notna() & s.astype(str).str.contains(CANDIDATE_RE, regex=True)
        for i in s.index[cand]:
            new, k = mask_value(s[i], hint)
            if k:
                df.at[i, c] = new
                masked += k
    if masked:
        notes.append(f"Masked {masked:,} card number(s) - only the last 4 digits are kept.")
    return df, notes
