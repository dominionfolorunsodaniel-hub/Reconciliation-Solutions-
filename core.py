"""
core.py - the analysis engine shared by the Telegram bot and the web dashboard.

Pipeline:  file (CSV / Excel / PDF)  ->  clean DataFrame  ->  Gemini turns the
user's plain-language request into a JSON plan  ->  pandas runs the plan
(exact math, no eval)  ->  table + chart + Excel + short written insights.
"""
import asyncio
import contextvars
import csv
import datetime
import io
import json
import os
import re
import statistics
import warnings
from types import SimpleNamespace

import httpx

try:  # read settings from the .env file next to the code (needs python-dotenv)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import pandas as pd

import privacy

MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
MAX_FILE_MB = 20
ALLOWED_EXT = {"csv", "txt", "xlsx", "xlsm", "xls", "pdf"}
ALLOWED_AGGS = {"sum", "mean", "count", "min", "max", "median", "nunique"}
FREQ_NAMES = {"D": "Day", "W": "Week", "M": "Month", "Q": "Quarter", "Y": "Year"}
TOTAL_WORDS = {"total", "grand total", "subtotal", "sub total", "sub-total"}
NUM_RE = r"^\s*\(?-?[$€£₦]?\s*\d[\d,]*\.?\d*\)?\s*%?\s*$"
DATE_RE = re.compile(
    r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}"  # 2026-03-01, 01/03/2026
    r"|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{2,4}"  # 1 March 2026
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{2,4}"  # March 1, 2026
)

_client = None

# Cost control: db.py plugs in these hooks. current_user_id says whose AI request this is.
current_user_id: contextvars.ContextVar = contextvars.ContextVar("current_user_id", default=None)
LIMIT_CHECK = None
USAGE_RECORD = None


class LimitReached(ValueError):
    """A daily AI usage limit has been reached."""


async def _ask(**kwargs):
    """Call Gemini through its REST API while preserving usage limits and existing callers."""
    uid = current_user_id.get()
    if LIMIT_CHECK:
        LIMIT_CHECK(uid)

    api_key = os.getenv("GEMINI_API_KEY", "")
    # Forgive common copy/paste slips: surrounding quotes, spaces, line breaks, or a pasted "GEMINI_API_KEY=" prefix.
    api_key = "".join(api_key.split()).strip("\"'")
    if api_key.upper().startswith("GEMINI_API_KEY="):
        api_key = api_key.split("=", 1)[1].strip("\"'")
    if not api_key:
        raise RuntimeError(
            "AI analysis is not configured. Add a Gemini API key as GEMINI_API_KEY in Render. "
            "Get one from https://aistudio.google.com/."
        )

    model = os.getenv("GEMINI_MODEL", MODEL).strip() or "gemini-flash-latest"
    max_tokens = int(kwargs.get("max_tokens", 800))
    system = str(kwargs.get("system", ""))
    messages = kwargs.get("messages", [])
    contents = []
    for message in messages:
        role = "model" if message.get("role") == "assistant" else "user"
        content = message.get("content", "")
        if isinstance(content, list):
            text = "\n".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        else:
            text = str(content)
        contents.append({"role": role, "parts": [{"text": text}]})

    payload = {
        "systemInstruction": {"parts": [{"text": system}]} if system else None,
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1},
    }
    # The planner and report description must return JSON; summaries should remain plain text.
    if "JSON object" in system or "JSON plan" in system:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    payload = {key: value for key, value in payload.items() if value is not None}
    headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
    # When Google is busy (503/500/timeouts) wait and retry, then fall back to backup models.
    backups = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-2.5-flash,gemini-flash-lite-latest").split(",") if m.strip()]
    plan = [model] * 2 + [m for m in backups if m != model]
    data, last_busy = None, "Gemini is very busy right now. Please try again in a minute."
    for attempt, current in enumerate(plan):
        if attempt:
            await asyncio.sleep(2 * min(attempt, 3))
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current}:generateContent"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0)) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            print(f"[gemini] {current} timed out (attempt {attempt + 1}/{len(plan)})", flush=True)
            continue
        except httpx.RequestError as exc:
            raise RuntimeError("Could not connect to Gemini. Check Render's network and try again.") from exc
        if not response.is_error:
            data = response.json()
            break
        try:
            detail = response.json().get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            detail = ""
        # Do not include the key or full request in error messages/logs.
        if response.status_code in (401, 403):
            raise RuntimeError("Gemini rejected the API key. Check GEMINI_API_KEY in Render.")
        if response.status_code == 400 and "api key" in detail.lower():
            print(f"[gemini] key rejected. Render is sending a key that starts '{api_key[:4]}', ends '{api_key[-4:]}', length {len(api_key)}.", flush=True)
            raise RuntimeError("Gemini says the API key is not valid. The administrator must check GEMINI_API_KEY in Render "
                               "(paste only the key, then save and wait for the redeploy).")
        if response.status_code in (429, 500, 502, 503, 504) or (response.status_code == 404 and attempt >= 2):
            print(f"[gemini] {current} returned HTTP {response.status_code} (attempt {attempt + 1}/{len(plan)})", flush=True)
            if response.status_code == 429:
                last_busy = "Gemini free-tier rate limit reached. Wait a minute and try again."
            continue
        raise RuntimeError(f"Gemini API returned HTTP {response.status_code}: {detail[:300]}")
    if data is None:
        raise RuntimeError(last_busy)

    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback", {}).get("blockReason", "")
        raise RuntimeError(f"Gemini returned no answer{f' (blocked: {feedback})' if feedback else ''}.")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
    if not text.strip():
        raise RuntimeError("Gemini returned an empty answer. Please rephrase the request and retry.")

    usage = data.get("usageMetadata", {})
    if USAGE_RECORD:
        USAGE_RECORD(uid, usage.get("promptTokenCount", 0) or 0,
                     usage.get("candidatesTokenCount", 0) or 0)
    # Keep the response shape expected by _text_of() and the existing analysis code.
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=usage.get("promptTokenCount", 0) or 0,
            output_tokens=usage.get("candidatesTokenCount", 0) or 0,
        ),
    )


# --------------------------------------------------------------------------- #
# Loading & cleaning
# --------------------------------------------------------------------------- #
def _clean_cell(v):
    if isinstance(v, str):
        v = v.strip()
        return v if v else None
    return v


def _rows_to_raw(rows) -> pd.DataFrame:
    rows = [r for r in rows if r is not None]
    width = max((len(r) for r in rows), default=0)
    if width == 0:
        raise ValueError("The file looks empty.")
    raw = pd.DataFrame([list(r) + [None] * (width - len(r)) for r in rows], dtype=object)
    for c in raw.columns:
        raw[c] = raw[c].map(_clean_cell)
    return raw


def _csv_to_raw(data: bytes) -> pd.DataFrame:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return _rows_to_raw(list(csv.reader(io.StringIO(text), dialect)))


def _ocr_words_to_rows(words: list[dict]) -> list[list[str]]:
    """Group OCR words (left/top/width/height/text) into table rows and cells."""
    words = [w for w in words if str(w["text"]).strip()]
    if not words:
        return []
    words.sort(key=lambda w: (w["top"], w["left"]))
    med_h = statistics.median(w["height"] for w in words)
    lines, cur = [], []
    for w in words:
        cy = w["top"] + w["height"] / 2
        if cur and abs(cy - sum(x["top"] + x["height"] / 2 for x in cur) / len(cur)) > 0.6 * med_h:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    rows = []
    for ln in lines:
        ln.sort(key=lambda w: w["left"])
        cells, prev_right = [str(ln[0]["text"])], ln[0]["left"] + ln[0]["width"]
        for w in ln[1:]:
            if w["left"] - prev_right > 1.2 * med_h:  # a wide gap = next column
                cells.append(str(w["text"]))
            else:
                cells[-1] += " " + str(w["text"])
            prev_right = w["left"] + w["width"]
        if len(cells) >= 2:
            rows.append(cells)
    return rows


def _ocr_pdf(data: bytes) -> list[list[str]]:
    """OCR for scanned PDFs. Needs `pip install pytesseract` and the Tesseract program installed."""
    if os.getenv("OCR_ENABLED", "1") == "0":
        raise ValueError("This looks like a scanned PDF and OCR is switched off. Export the report as Excel/CSV.")
    try:
        import pdfplumber
        import pytesseract
    except ImportError as e:
        raise ValueError("This looks like a scanned PDF. Install OCR support (pytesseract + Tesseract) "
                         "or export the report as Excel/CSV.") from e
    rows = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:30]:
                d = pytesseract.image_to_data(page.to_image(resolution=200).original, output_type=pytesseract.Output.DICT)
                rows.extend(_ocr_words_to_rows([
                    {"left": d["left"][i], "top": d["top"][i], "width": d["width"][i], "height": d["height"][i],
                     "text": d["text"][i]}
                    for i in range(len(d["text"])) if float(d["conf"][i]) >= 30]))
    except Exception as e:  # noqa: BLE001  (e.g. Tesseract program not installed)
        raise ValueError(f"OCR failed ({e}). Is Tesseract installed? Or export the report as Excel/CSV.") from e
    return rows


def _pdf_to_raw(data: bytes):
    import pdfplumber

    rows = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                rows.extend(table)
        if not rows:  # no ruled tables: fall back to text laid out in columns
            for page in pdf.pages:
                for line in (page.extract_text(layout=True) or "").splitlines():
                    parts = re.split(r"\s{2,}", line.strip())
                    if len(parts) >= 2:
                        rows.append(parts)
    ocr = False
    if not rows:  # no text layer at all: probably a scan -> try OCR
        rows, ocr = _ocr_pdf(data), True
        if not rows:
            raise ValueError("I couldn't find any table in this PDF. If you can, export the report as Excel/CSV.")
    return _rows_to_raw(rows), ocr


def load_report(data: bytes, filename: str, sheet: str | None = None):
    """Return (clean_df, notes, sheet_names)."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "csv"
    notes, sheets = [], []

    if ext in ("csv", "txt"):
        raw = _csv_to_raw(data)
    elif ext == "pdf":
        raw, ocr = _pdf_to_raw(data)
        notes.append("🔎 Scanned PDF read with OCR - double-check the numbers against the original."
                     if ocr else "PDF tables are extracted on a best-effort basis - check the preview.")
    else:
        xl = pd.ExcelFile(io.BytesIO(data))
        sheets = xl.sheet_names
        chosen = sheet if sheet in sheets else sheets[0]
        raw = _rows_to_raw(xl.parse(chosen, header=None, dtype=object).values.tolist())
        if len(sheets) > 1:
            notes.append(f'Read sheet "{chosen}". Others: {", ".join(sheets)}')

    raw = raw.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if raw.empty:
        raise ValueError("The file looks empty.")

    # Find the header row (reports often have title lines above the table).
    width = raw.notna().sum(axis=1)
    head_rows = width.iloc[:25]
    hdr = next(i for i, w in enumerate(head_rows) if w >= 0.7 * head_rows.max())
    cols, seen = [], {}
    for i, c in enumerate(raw.iloc[hdr]):
        name = str(c).strip() if pd.notna(c) else f"column_{i + 1}"
        seen[name] = seen.get(name, 0) + 1
        cols.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    df = raw.iloc[hdr + 1:].reset_index(drop=True)
    df.columns = cols
    df = df.dropna(how="all")

    # Drop repeated header rows (multi-page PDFs / stacked exports).
    as_text = df.astype(str).apply(lambda col: col.str.strip())
    repeated = (as_text == pd.Series(cols, index=df.columns)).mean(axis=1) >= 0.6
    if repeated.any():
        notes.append(f"Removed {int(repeated.sum())} repeated header row(s).")
        df = df[~repeated]

    # Drop "Total" rows so they don't double-count.
    totals = df.apply(
        lambda col: col.astype(str).str.strip().str.lower().isin(TOTAL_WORDS)
    ).any(axis=1)
    if totals.any():
        notes.append(f"Ignored {int(totals.sum())} total/subtotal row(s).")
        df = df[~totals]

    # Privacy: mask card numbers, drop CVV/PIN columns - before anything is stored or sent to the AI provider.
    df, privacy_notes = privacy.sanitize(df.reset_index(drop=True))
    notes += privacy_notes
    df = coerce_types(df)
    df.attrs["sanitized"] = bool(privacy_notes)
    return df, notes, sheets


def coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        s = df[c]
        nn = s.dropna()
        if nn.empty:
            continue
        if nn.astype(str).str.match(NUM_RE).mean() > 0.9:
            cleaned = (
                s.astype(str)
                .str.replace(r"^\s*\((.*)\)\s*$", r"-\1", regex=True)
                .str.replace(r"[^\d.\-]", "", regex=True)
            )
            num = pd.to_numeric(cleaned, errors="coerce")
            if num.notna().any() and (num.dropna() % 1 == 0).all():
                num = num.astype("Int64")
            df[c] = num
            continue
        sample = nn.head(200)
        looks_like_date = sample.map(
            lambda v: isinstance(v, (datetime.date, datetime.datetime, pd.Timestamp))
            or bool(DATE_RE.search(str(v)))
        )
        if looks_like_date.mean() <= 0.9:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            probe = pd.to_datetime(sample, errors="coerce", format="mixed")
            if probe.notna().mean() > 0.9:
                df[c] = pd.to_datetime(s, errors="coerce", format="mixed")
    return df


def to_clean_csv(df: pd.DataFrame) -> bytes:
    """The sanitized table as CSV - stored instead of the original when card data was found."""
    return df.to_csv(index=False).encode("utf-8")


def describe_schema(df: pd.DataFrame) -> str:
    lines = []
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            lines.append(f'- "{c}" (date): {s.min():%Y-%m-%d} to {s.max():%Y-%m-%d}')
        elif pd.api.types.is_numeric_dtype(s):
            lines.append(f'- "{c}" (number): min {s.min():g}, max {s.max():g}')
        else:
            top = s.dropna().astype(str).value_counts().head(8).index.tolist()
            lines.append(f'- "{c}" (text): {s.nunique()} unique, e.g. {top}')
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Gemini: request -> JSON plan, and short written insights
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """You convert a user's analysis request about a table into a JSON plan.
Reply with ONE JSON object and nothing else. Schema:
{
  "clarify": null or "a short question if the request is impossible/ambiguous",
  "title": "short chart/table title",
  "filters": [{"column": str, "op": "==|!=|>|>=|<|<=|contains|in|not_in|between", "value": any}],
  "group_by": [{"column": str, "freq": null or "D|W|M|Q|Y"}],
  "metrics": [{"column": str or "*", "agg": "sum|mean|count|min|max|median|nunique", "alias": str}],
  "percent_of_total": bool,
  "sort": null or {"by": str, "ascending": bool},
  "top_n": null or int,
  "chart": "bar|line|pie|none"
}
Rules:
- Use ONLY column names that exist exactly as given. Use "freq" only on date columns.
- To count rows use {"column": "*", "agg": "count"}.
- "between" takes value [low, high]; "in"/"not_in" take a list. Dates as YYYY-MM-DD.
- Pick line for time trends, pie for share-of-total with few groups, else bar.
- If the user says things like "now by month instead" or "same but only X", modify the previous plan.
- If no grouping is requested, leave group_by empty (gives overall totals).
- Metrics must be sensible: don't sum text columns.
- Columns "_report" and "_uploaded" exist when several saved reports are combined: use them to
  compare reports (e.g. month over month, branch A in report X vs report Y).
"""

INSIGHT_PROMPT = (
    "You are a careful data analyst. Using ONLY the numbers in the table, write 2-4 short bullet "
    "points answering the user's question: biggest/smallest, notable shares or changes, outliers. "
    "Never invent numbers or causes. Plain text, each bullet starting with '- '."
)


def _text_of(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


async def make_plan(question: str, df: pd.DataFrame, prev_plan: dict | None) -> dict:
    sample = df.head(5).to_string(index=False, max_colwidth=25)
    user_msg = (
        f"Columns:\n{describe_schema(df)}\n\nSample rows:\n{sample}\n\n"
        f"Previous plan: {json.dumps(prev_plan) if prev_plan else 'none'}\n\n"
        f"Request: {question}"
    )
    resp = await _ask(
        model=MODEL, max_tokens=1200, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = _text_of(resp)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("I couldn't understand that request. Try rephrasing.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError("I couldn't understand that request. Try rephrasing.") from e


async def summarize(question: str, res: pd.DataFrame) -> str | None:
    try:
        table = res.head(40).to_string(index=False, max_colwidth=30)
        resp = await _ask(
            model=MODEL, max_tokens=400, system=INSIGHT_PROMPT,
            messages=[{"role": "user", "content": f"Question: {question}\n\nTable:\n{table}"}],
        )
        return _text_of(resp).strip() or None
    except Exception:  # insights are a bonus; never fail the analysis
        return None


# --------------------------------------------------------------------------- #
# Executing a plan with pandas (fixed operations only - no eval)
# --------------------------------------------------------------------------- #
def apply_filter(d: pd.DataFrame, f: dict) -> pd.DataFrame:
    col, op, val = f["column"], f["op"], f.get("value")
    if col not in d.columns:
        raise ValueError(f'Unknown column "{col}"')
    if op == "contains":
        return d[d[col].astype(str).str.contains(str(val), case=False, regex=False, na=False)]

    s = d[col]
    if pd.api.types.is_datetime64_any_dtype(s):
        conv = pd.to_datetime
    elif pd.api.types.is_numeric_dtype(s):
        conv = lambda v: float(str(v).replace(",", ""))  # noqa: E731
    else:
        s = s.astype(str).str.strip().str.lower()
        conv = lambda v: str(v).strip().lower()  # noqa: E731
    v = [conv(x) for x in val] if isinstance(val, list) else conv(val)

    ops = {
        "==": lambda: s == v, "!=": lambda: s != v,
        ">": lambda: s > v, ">=": lambda: s >= v,
        "<": lambda: s < v, "<=": lambda: s <= v,
        "in": lambda: s.isin(v if isinstance(v, list) else [v]),
        "not_in": lambda: ~s.isin(v if isinstance(v, list) else [v]),
        "between": lambda: (s >= v[0]) & (s <= v[1]),
    }
    if op not in ops:
        raise ValueError(f"Unsupported filter operator: {op}")
    return d[ops[op]().fillna(False)]


def run_plan(df: pd.DataFrame, plan: dict):
    d = df.copy()
    for f in plan.get("filters") or []:
        d = apply_filter(d, f)
    if d.empty:
        raise ValueError("No rows match those filters.")

    keys, is_time = [], False
    for g in plan.get("group_by") or []:
        col, freq = g["column"], g.get("freq")
        if col not in d.columns:
            raise ValueError(f'Unknown column "{col}"')
        if freq:
            if not pd.api.types.is_datetime64_any_dtype(d[col]):
                raise ValueError(f'"{col}" is not a date column.')
            label = f"{col} ({FREQ_NAMES.get(freq, freq)})"
            d[label] = d[col].dt.to_period(freq).astype(str)
            keys.append(label)
            is_time = True
        else:
            keys.append(col)

    aggs = {}
    for m in plan.get("metrics") or [{"column": "*", "agg": "count", "alias": "Rows"}]:
        col, agg = m["column"], m["agg"]
        if agg not in ALLOWED_AGGS:
            raise ValueError(f"Unsupported aggregation: {agg}")
        alias = m.get("alias") or f"{agg} of {col}"
        if col == "*":
            d["_rows"] = 1
            aggs[alias] = ("_rows", "sum")
        else:
            if col not in d.columns:
                raise ValueError(f'Unknown column "{col}"')
            aggs[alias] = (col, agg)

    if keys:
        res = d.groupby(keys, dropna=False).agg(**aggs).reset_index()
    else:
        res = d.assign(_all=1).groupby("_all").agg(**aggs).reset_index(drop=True)

    first_metric = list(aggs)[0]
    if plan.get("percent_of_total") and pd.api.types.is_numeric_dtype(res[first_metric]):
        total = res[first_metric].sum()
        if total:
            res["% of total"] = (100 * res[first_metric] / total).round(2)

    sort = plan.get("sort")
    if sort and sort.get("by") in res.columns:
        res = res.sort_values(sort["by"], ascending=bool(sort.get("ascending", False)))
    elif is_time:
        res = res.sort_values(keys)
    elif keys:
        res = res.sort_values(first_metric, ascending=False)
    if plan.get("top_n"):
        res = res.head(int(plan["top_n"]))

    res = res.round(2).reset_index(drop=True)
    return res, d, keys, first_metric


async def analyze(df: pd.DataFrame, question: str, prev_plan: dict | None = None, insights: bool = True):
    """Full pipeline. Returns a dict; contains 'clarify' if the model needs more info."""
    plan = await make_plan(question, df, prev_plan)
    if plan.get("clarify"):
        return {"plan": plan, "clarify": plan["clarify"]}
    res, filtered, keys, metric = await asyncio.to_thread(run_plan, df, plan)
    insight = await summarize(question, res) if insights else None
    return {
        "plan": plan, "res": res, "filtered": filtered, "keys": keys,
        "metric": metric, "title": plan.get("title") or "Result", "insight": insight,
    }


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def chart_payload(res, keys, metric, kind):
    """Chart data as plain lists (used by the web dashboard and the PNG chart)."""
    if kind == "none" or not keys or len(res) < 2:
        return None
    part = res.head(10 if kind == "pie" else 40)
    labels = part[keys].astype(str).agg(" | ".join, axis=1).tolist()
    values = pd.to_numeric(part[metric], errors="coerce").fillna(0).astype(float).tolist()
    return {"type": kind, "labels": labels, "values": values, "metric": metric}


def make_chart_png(res, keys, metric, kind, title):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    p = chart_payload(res, keys, metric, kind)
    if not p:
        return None
    labels, vals = p["labels"], p["values"]
    fig, ax = plt.subplots(figsize=(9, 5))
    if kind == "pie" and all(v >= 0 for v in vals) and sum(vals) > 0:
        ax.pie(vals, labels=labels, autopct="%1.0f%%", startangle=90)
    elif kind == "line":
        ax.plot(range(len(vals)), vals, marker="o")
        ax.set_xticks(range(len(vals)))
        ax.set_xticklabels(labels, rotation=45, ha="right")
        ax.grid(alpha=0.3)
    else:
        ax.bar(range(len(vals)), vals)
        ax.set_xticks(range(len(vals)))
        ax.set_xticklabels(labels, rotation=45, ha="right")
        ax.grid(axis="y", alpha=0.3)
    ax.set_title(title or metric)
    if kind != "pie":
        ax.set_ylabel(metric)
    fig.tight_layout()
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=150)
    plt.close(fig)
    out.seek(0)
    return out


def dfs_to_excel(sheets: dict) -> io.BytesIO:
    """Write several DataFrames to one workbook. Text that starts with '=' is stored as plain text,
    so a malicious cell in an uploaded report can never run as an Excel formula."""
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, frame in sheets.items():
            frame.head(100_000).to_excel(xw, sheet_name=str(name)[:31], index=False)
        for ws in xw.book.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value[:1] == "=":
                        cell.data_type = "s"
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col[:200])
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 45)
    out.seek(0)
    return out


def make_excel(res, filtered, plan, insight=None):
    info = {"plan": [json.dumps(plan, indent=2)]}
    if insight:
        info["insights"] = [insight]
    return dfs_to_excel({
        "Result": res,
        "Filtered data": filtered.drop(columns=["_rows"], errors="ignore"),
        "Plan": pd.DataFrame(info),
    })


# --------------------------------------------------------------------------- #
# First impression: what is this report about?  (shown right after upload)
# --------------------------------------------------------------------------- #
def profile_facts(df: pd.DataFrame) -> dict:
    """Exact numbers computed by code (never by the LLM)."""
    facts = {
        "rows": int(len(df)), "columns": int(len(df.columns)),
        "duplicate_rows": int(df.duplicated().sum()),
        "dates": {}, "numbers": {}, "categories": {}, "missing_pct": {},
    }
    for c in df.columns:
        s = df[c]
        miss = float(s.isna().mean()) * 100
        if miss >= 5:
            facts["missing_pct"][c] = round(miss, 1)
        if s.notna().sum() == 0:
            continue
        if pd.api.types.is_datetime64_any_dtype(s):
            facts["dates"][c] = f"{s.min():%Y-%m-%d} to {s.max():%Y-%m-%d}"
        elif pd.api.types.is_numeric_dtype(s):
            facts["numbers"][c] = {
                "sum": round(float(s.sum()), 2), "avg": round(float(s.mean()), 2),
                "min": float(s.min()), "max": float(s.max()), "negatives": int((s < 0).sum()),
            }
        else:
            facts["categories"][c] = {
                "unique": int(s.nunique()),
                "top": {str(k): int(v) for k, v in s.astype(str).value_counts().head(3).items()},
            }
    return facts


def quality_flags(facts: dict) -> list[str]:
    flags = []
    if facts["duplicate_rows"]:
        flags.append(f"{facts['duplicate_rows']:,} duplicate row(s)")
    for c, pct in facts["missing_pct"].items():
        flags.append(f'"{c}" is {pct:g}% empty')
    for c, n in facts["numbers"].items():
        if n["negatives"]:
            flags.append(f'"{c}" has {n["negatives"]:,} negative value(s) (refunds/reversals?)')
    return flags[:6]


def _default_questions(df: pd.DataFrame) -> list[str]:
    dates = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    cats = [c for c in df.columns if c not in dates + nums and 1 < df[c].nunique() <= 200]
    qs = []
    if nums and cats:
        qs.append(f"Total {nums[0]} by {cats[0]}, top 10")
    if nums and dates:
        qs.append(f"Total {nums[0]} by month")
    if nums and len(cats) > 1:
        qs.append(f"Total {nums[0]} by {cats[0]} and {cats[1]}")
    if cats:
        qs.append(f"Number of rows by {cats[0]}")
    if nums:
        qs.append(f"Overall total, average and maximum of {nums[0]}")
    return qs[:5]


DESCRIBE_PROMPT = """You are a data analyst opening a new report for the first time.
From the exact facts, columns and sample rows, reply with ONE JSON object and nothing else:
{
  "what_it_is": "1-2 plain sentences: what kind of report this looks like (e.g. ATM transaction log, POS sales by terminal, expense ledger) and what one row represents",
  "period": "the time range covered, or null",
  "key_points": ["2-4 short facts, using ONLY numbers that appear in the facts"],
  "suggested_questions": ["4-5 specific questions the user could ask next, using real column names/values"]
}
Never invent numbers. If unsure what the report is, say 'looks like'."""


async def describe_report(df: pd.DataFrame, filename: str) -> dict:
    """Never raises: falls back to a description built from the exact facts."""
    facts = profile_facts(df)
    out = {
        "what_it_is": None, "period": None, "key_points": [], "suggested_questions": [],
        "flags": quality_flags(facts), "alerts": [], "facts": facts,
    }
    try:
        user_msg = (
            f"Filename: {filename}\nFacts: {json.dumps(facts, default=str)[:6000]}\n\n"
            f"Columns:\n{describe_schema(df)}\n\n"
            f"Sample rows:\n{df.head(5).to_string(index=False, max_colwidth=25)}"
        )
        resp = await _ask(
            model=MODEL, max_tokens=800, system=DESCRIBE_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = _text_of(resp)
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        out["what_it_is"] = str(data.get("what_it_is") or "").strip() or None
        out["period"] = str(data["period"]) if data.get("period") else None
        out["key_points"] = [str(x) for x in (data.get("key_points") or [])][:4]
        out["suggested_questions"] = [str(x) for x in (data.get("suggested_questions") or [])][:5]
    except Exception:  # no API key / bad reply: use the fallbacks below
        pass

    if not out["what_it_is"]:
        cols = ", ".join(map(str, df.columns[:6])) + ("…" if len(df.columns) > 6 else "")
        out["what_it_is"] = f"A table with {facts['rows']:,} rows and {facts['columns']} columns ({cols})."
    if not out["period"] and facts["dates"]:
        out["period"] = next(iter(facts["dates"].values()))
    if not out["key_points"]:
        out["key_points"] = [
            f'{c}: total {n["sum"]:,.2f}, average {n["avg"]:,.2f}'
            for c, n in list(facts["numbers"].items())[:3]
        ]
    if len(out["suggested_questions"]) < 3:
        out["suggested_questions"] = (out["suggested_questions"] + _default_questions(df))[:5]
    try:
        from anomaly import scan

        out["alerts"] = scan(df)
    except Exception:  # alerts are a bonus; never block the upload
        out["alerts"] = []
    return out


def description_text(d: dict) -> str:
    parts = [f"📄 {d['what_it_is']}"]
    if d.get("period"):
        parts.append(f"🗓 {d['period']}")
    if d.get("key_points"):
        parts.append("\n".join("• " + p for p in d["key_points"]))
    if d.get("alerts"):
        icon = {"high": "🔴", "medium": "🟠", "info": "ℹ️"}
        parts.append("🚨 Alerts:\n" + "\n".join(f"{icon.get(a['severity'], '•')} {a['text']}" for a in d["alerts"]))
    if d.get("flags"):
        parts.append("⚠️ Worth checking:\n" + "\n".join("• " + f for f in d["flags"]))
    return "\n\n".join(parts)
