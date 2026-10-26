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

import loaders
import privacy

MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
MAX_FILE_MB = 20
ALLOWED_EXT = {"csv", "tsv", "txt", "text", "dat", "xlsx", "xlsm", "xls", "ods", "pdf", "json", "jsonl", "ndjson",
               "xml", "html", "htm", "docx", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "gif"}
FILE_TYPES_TEXT = "CSV, Excel (.xlsx/.xls), PDF, Word (.docx), JSON, XML, HTML, text, OpenDocument (.ods) or a photo/screenshot (PNG, JPG)"
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

    model = (kwargs.get("model_override") or os.getenv("GEMINI_MODEL", MODEL)).strip() or "gemini-flash-latest"
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
    return _rows_to_raw(loaders.text_rows(loaders.decode_text(data)))


def _ocr_image(data: bytes) -> list[list[str]]:
    """Photos / screenshots of a statement or report: read the text with OCR and rebuild the table."""
    if os.getenv("OCR_ENABLED", "1") == "0":
        raise ValueError("Reading pictures is switched off. Please upload the report as Excel, CSV or PDF.")
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError as e:
        raise ValueError("Reading pictures needs OCR support (pytesseract + Tesseract). "
                         "Please upload the report as Excel, CSV or PDF.") from e
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("L")
        if img.width < 1400:  # small screenshots read much better when enlarged
            scale = 1400 / img.width
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"I couldn't read that picture ({e}). Try a clearer, straight-on photo.") from e
    rows = _ocr_words_to_rows([
        {"left": d["left"][i], "top": d["top"][i], "width": d["width"][i], "height": d["height"][i], "text": d["text"][i]}
        for i in range(len(d["text"])) if float(d["conf"][i]) >= 30])
    if not rows:
        raise ValueError("I couldn't find a table in that picture. Try a clearer photo, or upload Excel/CSV/PDF.")
    return rows


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
    """Return (clean_df, notes, sheet_names). The real file type is detected from the content, not the name."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "csv"
    kind = loaders.sniff(data, ext)
    notes, sheets = [], []

    if kind == "pdf":
        raw, ocr = _pdf_to_raw(data)
        notes.append("🔎 Scanned PDF read with OCR - double-check the numbers against the original."
                     if ocr else "PDF tables are extracted on a best-effort basis - check the preview.")
    elif kind == "image":
        raw = _rows_to_raw(_ocr_image(data))
        notes.append("🔎 Picture read with OCR - please double-check the numbers against the original.")
    elif kind in ("xlsx", "xls", "ods"):
        try:
            xl = pd.ExcelFile(io.BytesIO(data), engine="odf" if kind == "ods" else None)
        except ImportError as e:
            raise ValueError("OpenDocument (.ods) support is not installed on this server. Save it as Excel and re-upload.") from e
        sheets = xl.sheet_names
        chosen = sheet if sheet in sheets else sheets[0]
        raw = _rows_to_raw(xl.parse(chosen, header=None, dtype=object).values.tolist())
        if len(sheets) > 1:
            notes.append(f'Read sheet "{chosen}". Others: {", ".join(sheets)}')
    elif kind == "docx":
        raw = _rows_to_raw(loaders.docx_rows(data))
    elif kind == "json":
        raw = _rows_to_raw(loaders.json_rows(data))
    elif kind == "xml":
        raw = _rows_to_raw(loaders.xml_rows(data))
    elif kind == "html":
        raw = _rows_to_raw(loaders.html_rows(data))
        if ext in ("xls", "xlsx"):
            notes.append("This \"Excel\" file was really a web page; I read the table inside it.")
    elif kind == "zip":
        raise ValueError("That is a zip/archive file. Please unzip it and upload the report inside.")
    else:
        raw = _csv_to_raw(data)

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


# --------------------------------------------------------------------------- #
# Understanding money: credits / debits in any layout
# --------------------------------------------------------------------------- #
CREDIT_WORDS = {"credit", "credits", "cr", "c", "deposit", "deposits", "lodgement", "lodgements", "lodgment",
                "inflow", "inflows", "money in", "received", "receipt", "income"}
DEBIT_WORDS = {"debit", "debits", "dr", "d", "withdrawal", "withdrawals", "withdraw", "outflow", "outflows",
               "money out", "paid", "payment", "expense"}
_CR_NAME = re.compile(r"\b(credit|credits|cr|deposit|deposits|lodgement|lodgements|lodgment|money in|inflow|received)\b", re.I)
_DR_NAME = re.compile(r"\b(debit|debits|dr|withdrawal|withdrawals|money out|outflow|paid)\b", re.I)
_NOT_AMOUNT = re.compile(r"balance|\bbal\b|\bid\b|ref|\bno\b|number|fee|charge|vat|rate|account|acct|terminal|stan|rrn|pan\b", re.I)
CRED_COL, AMT_COL = "Credit/Debit", "Amount (always positive)"
_ALIAS_GROUPS = [CREDIT_WORDS, DEBIT_WORDS]


def _norm_word(v) -> str:
    return re.sub(r"[^a-z ]", "", str(v).lower()).strip()


def add_money_columns(df: pd.DataFrame):
    """Add two helper columns, "Credit/Debit" and "Amount (always positive)", whenever the table shows money
    moving in or out - however it is laid out. Returns (new_df, plain-English note or None). Never raises."""
    try:
        if CRED_COL in df.columns or AMT_COL in df.columns or df.empty:
            return df, None
        nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])]

        def amount_col():
            cands = [c for c in nums if not _NOT_AMOUNT.search(str(c))]
            named = [c for c in cands if re.search(r"amount|value|sum|naira|ngn|total|sales|txn", str(c), re.I)]
            return (named or cands or [None])[0]

        d = df.copy()
        # 1) two separate columns, one for credit and one for debit
        cr = [c for c in nums if _CR_NAME.search(str(c))]
        dr = [c for c in nums if _DR_NAME.search(str(c))]
        if cr and dr:
            c_ = pd.to_numeric(df[cr[0]], errors="coerce").astype("float64").fillna(0)
            d_ = pd.to_numeric(df[dr[0]], errors="coerce").astype("float64").fillna(0)
            d[CRED_COL] = pd.Series(pd.NA, index=d.index, dtype=object)
            d.loc[c_.abs() > 0, CRED_COL] = "Credit"
            d.loc[d_.abs() > 0, CRED_COL] = "Debit"
            d[AMT_COL] = c_.abs().where(c_.abs() > 0, d_.abs())
            return d, (f'Your table has separate "{cr[0]}" (money coming in) and "{dr[0]}" (money going out) columns. '
                       f'I combined them into "{CRED_COL}" and "{AMT_COL}".')
        # 2) a text column that says credit/debit (or CR/DR) for each row
        amt = amount_col()
        for c in df.columns:
            if c == amt or pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_datetime64_any_dtype(df[c]):
                continue
            vals = df[c].dropna().map(_norm_word)
            if vals.empty or vals.nunique() > 8:
                continue
            known = vals.isin(CREDIT_WORDS | DEBIT_WORDS)
            if known.mean() >= 0.8 and vals[known].isin(CREDIT_WORDS).any() and vals[known].isin(DEBIT_WORDS).any() and amt:
                norm = df[c].map(lambda v: "Credit" if _norm_word(v) in CREDIT_WORDS else "Debit" if _norm_word(v) in DEBIT_WORDS else None)
                d[CRED_COL] = norm
                d[AMT_COL] = pd.to_numeric(df[amt], errors="coerce").astype("float64").abs()
                return d, (f'The column "{c}" says whether each row is a credit (money in) or a debit (money out), and "{amt}" holds '
                           f'the amount. I added "{CRED_COL}" and "{AMT_COL}" so questions about credits and debits are exact.')
        # 3) one signed amount column: positive = credit, negative = debit
        if amt is not None:
            s = pd.to_numeric(df[amt], errors="coerce").astype("float64")
            if (s < 0).any() and (s > 0).any():
                d[CRED_COL] = s.map(lambda v: None if pd.isna(v) or v == 0 else "Credit" if v > 0 else "Debit")
                d[AMT_COL] = s.abs()
                return d, (f'"{amt}" has both positive and negative numbers, so I treated positive as a credit (money in) and negative as a '
                           f'debit (money out), and added "{CRED_COL}" and "{AMT_COL}".')
    except Exception:  # noqa: BLE001  (a helper must never break an analysis)
        pass
    return df, None


# --------------------------------------------------------------------------- #
# Forgiving column names (the AI sometimes changes case / spacing)
# --------------------------------------------------------------------------- #
def _key(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def resolve_column(name, columns):
    if name in columns or name == "*":
        return name
    by_key = {_key(c): c for c in columns}
    if _key(name) in by_key:
        return by_key[_key(name)]
    import difflib

    close = difflib.get_close_matches(_key(name), list(by_key), n=1, cutoff=0.82)
    return by_key[close[0]] if close else name


def fix_plan_columns(plan: dict, columns) -> dict:
    cols = list(columns)
    for sp in plan.get("derived") or []:
        sp["columns"] = [resolve_column(c, cols) for c in (sp.get("columns") or ([sp["column"]] if sp.get("column") else []))]
        sp.pop("column", None)
        if sp.get("name"):
            cols.append(sp["name"])
    for f in plan.get("filters") or []:
        if f.get("column"):
            f["column"] = resolve_column(f["column"], cols)
    for g in plan.get("group_by") or []:
        if g.get("column"):
            g["column"] = resolve_column(g["column"], cols)
    for m in plan.get("metrics") or []:
        if m.get("column"):
            m["column"] = resolve_column(m["column"], cols)
    aliases = [m.get("alias") for m in plan.get("metrics") or [] if m.get("alias")]
    if plan.get("select"):
        plan["select"] = [resolve_column(c, cols) for c in plan["select"]]
    for h in plan.get("having") or []:
        h["metric"] = resolve_column(h.get("metric"), aliases + cols)
    if isinstance(plan.get("sort"), dict) and plan["sort"].get("by"):
        plan["sort"]["by"] = resolve_column(plan["sort"]["by"], cols + aliases + ["% of total", "% change vs previous", "Running total"])
    return plan


def _expand_aliases(v):
    """"credit" also means CR, deposit, lodgement...  "debit" also means DR, withdrawal..."""
    if isinstance(v, list):
        out = []
        for x in v:
            e = _expand_aliases(x)
            out.extend(e if isinstance(e, list) else [e])
        return out
    w = _norm_word(v)
    for g in _ALIAS_GROUPS:
        if w in g:
            return sorted(g)
    return v


# --------------------------------------------------------------------------- #
# Gemini: request -> JSON plan, and short written insights
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """You are the analysis brain of a finance reporting tool. You turn ANY request about a table into a JSON plan that
code then runs exactly. Reply with ONE JSON object and nothing else. Schema (keys in this order):
{
  "reasoning": "1-2 short sentences: what the user really wants and which columns answer it",
  "mode": "table" | "rows" | "explain" | "general",
  "clarify": null,
  "assumptions": ["plain-English note for every guess you made"],
  "title": "short title",
  "derived": [{"name": str, "op": "year|month|month_name|weekday|hour|day|date|abs|ratio|diff|sum|product|pct", "columns": [col] or [colA, colB]}],
  "filters": [{"column": str or "*", "op": "==|!=|>|>=|<|<=|contains|in|not_in|between", "value": any}],
  "group_by": [{"column": str, "freq": null or "D|W|M|Q|Y"}],
  "metrics": [{"column": str or "*", "agg": "sum|mean|count|min|max|median|nunique", "alias": str}],
  "having": [{"metric": alias, "op": ">|>=|<|<=|==|!=", "value": number}],
  "percent_of_total": bool, "pct_change": bool, "cumulative": bool,
  "select": [columns to show, only for mode rows],
  "sort": null or {"by": str, "ascending": bool},
  "top_n": null or int,
  "chart": "bar|line|pie|none"
}
Modes: "table" = counts/totals/averages/rankings/trends/comparisons (grouped numbers). "rows" = the user wants to SEE the actual
transactions/records ("show me", "list", "which transactions", "find all", "everything over 50k") - use filters, select, sort, top_n.
"explain" = explain this file, its columns, a result, or finance words in the context of this file. "general" = a general finance /
accounting / business question that the file cannot answer (e.g. "what is a chargeback?") - answer from knowledge.
How to read the user:
- People write fast on phones with bad grammar, typos, slang, Nigerian Pidgin, abbreviations, mixed languages and half-sentences.
  Work out what they MEAN. Never say you cannot understand. Never refuse. Never ask them to rephrase. Set "clarify" to null.
- Put every guess in "assumptions" in plain words. When a request is vague pick the most useful reading and say which.
- Numbers: "50k" = 50000, "1.5m" = 1500000, "N5,000" or "₦5,000" = 5000. Percent and "half" etc: convert to numbers.
- Dates: use the data's own dates. "last month", "this week", "recently" are relative to the LATEST date in the data (given below),
  not to today, unless the user clearly means today. Use filter "between" with YYYY-MM-DD, or derived month/weekday/hour columns.
  "morning/afternoon/night" = derived hour column with a between filter. "weekends" = derived weekday in ["Saturday","Sunday"].
- Names: users misspell and abbreviate names. Use the REAL values listed in the column info; pick the closest real value.
  Search with "contains" (or column "*" to search every text column) when they give only part of a name or a keyword.
- "highest/biggest/top/most" = sort descending + top_n; "lowest/smallest/least" = ascending; "average/avg/mean"; "how many" = count;
  "total/sum/altogether" = sum; "per/by/each" = group_by; "compare A and B" = group_by the thing that separates A and B;
  "growth/increase/decrease/change" = group by month + pct_change; "running/cumulative" = cumulative;
  "share/percentage/proportion" = percent_of_total; "more than N transactions" = group + having.
- Ratios between two columns (e.g. fee as % of amount) = derived ratio or pct, then aggregate it.
- Do not use a Balance column as an amount unless the user asks about balance. Prefer the column whose name matches their words.
Money words (very important):
- "credit", "CR", "deposit", "lodgement", "money in", "inflow", "received" = money coming IN.
- "debit", "DR", "withdrawal", "money out", "outflow", "paid", "spent", "expenses" = money going OUT.
- If the columns "Credit/Debit" and "Amount (always positive)" exist they were built for this: filter or group by "Credit/Debit"
  (values "Credit"/"Debit") and sum "Amount (always positive)". Use them for any credit/debit question.
- If the table has no credit/debit information, still answer with the closest columns and say so in "assumptions".
Rules:
- Use ONLY column names that exist exactly as given (or names you define in "derived"). "freq" only on date columns.
- To count rows use {"column": "*", "agg": "count"}. Never sum a text column. "between" takes [low, high]; "in"/"not_in" a list.
- If the user says "now by month instead" or "same but only X", modify the previous plan.
- If no grouping is requested, leave group_by empty (overall totals).
- Columns "_report" and "_uploaded" exist when several saved reports are combined: use them to compare reports.
Examples (request -> key parts of the plan):
- "wetin be my biggest 5 expenses" -> mode rows, filters Credit/Debit == Debit, sort Amount desc, top_n 5, select Date/Narration/Amount.
- "how much POS dey enter per month" -> table, filters Narration contains POS and Credit/Debit == Credit, group_by Date freq M, sum Amount.
- "transactions between 10k and 50k" -> rows, filter Amount between [10000, 50000].
- "which day of week do i spend most" -> derived weekday from Date, filters Debit, group_by weekday, sum Amount, sort desc.
- "customers that paid more than 3 times" -> group_by customer, count *, having count > 3.
- "is my spending going up" -> table, Debit, group_by Date freq M, sum Amount, pct_change true, chart line.
"""

GENERAL_PROMPT = """You are a patient, careful finance and business teacher. The user asked a general question that their file cannot answer
by itself. You are given the question, a short profile of their file and any notes. Write a plain-text answer (no markdown symbols like
** or #) with these sections, each on its own line followed by its content:
ANSWER: a direct answer in one to three sentences.
BREAKDOWN: 4 to 10 lines starting with '- ' explaining the idea step by step with a simple example.
WHAT IT MEANS FOR YOU: 2 to 4 lines connecting it to their file where that is genuinely useful (use only numbers from the profile), plus what they can ask next.
WORDS EXPLAINED: one line per technical term. Skip if none.
Be honest about what is general knowledge versus what is in their file; say "From general knowledge:" before general facts, and say when rules differ by country or bank.
Reply in the language the user wrote in. Short, friendly sentences. Never invent figures about their file."""

INSIGHT_PROMPT = """You are a patient, careful finance and data teacher explaining results to a business owner who may not be technical.
You are given: the user's question, the exact result table (computed by code), key figures (computed by code), how the result
was worked out, and any assumptions. Write a complete plain-text breakdown (no markdown symbols like ** or #).
Use exactly these sections, each on its own line followed by its content:
ANSWER: one or two sentences that directly answer the question with the exact figures.
BREAKDOWN: 4 to 10 lines starting with '- ' going through the result item by item (every row if there are 12 or fewer,
otherwise the biggest, the smallest and any that stand out), with exact figures and shares.
WHAT IT MEANS: 2 to 4 lines in everyday words about what the numbers say and anything worth checking.
WORDS EXPLAINED: one line per finance or technical term you used (e.g. credit = money coming in; debit = money going out;
reconciliation = matching two records to find differences). Skip this section if you used no such term.
Rules for accuracy:
- Use ONLY numbers that appear in the table, the key figures or the method. Copy them exactly as written (keep the commas and
  decimals). Do not round, abbreviate (no "1.2M") and do not do your own arithmetic. Never invent reasons or causes.
- If the data cannot fully answer the question, say exactly what is missing and what the closest answer is.
- Reply in the same language the user wrote the question in (simple English if mixed). Short sentences. Friendly and clear.
"""

EXPLAIN_PROMPT = """You are a patient, careful finance and data teacher. The user asked something about their uploaded file that needs an
explanation rather than a calculation. You are given the user's question, an exact profile of the file computed by code
(columns, types, totals, date ranges, counts), a few sample rows and any helper notes. Write a plain-text answer (no markdown symbols
like ** or #) with these sections, each on its own line followed by its content:
ANSWER: a direct answer to what they asked, in one to three sentences.
BREAKDOWN: 4 to 12 lines starting with '- ' explaining the file piece by piece: what it is, what each important column means, the
period it covers, key totals, and anything unusual.
WHAT IT MEANS: 2 to 4 lines in everyday words, including what the user can ask next.
WORDS EXPLAINED: one line per finance or technical term used. Skip if none.
Rules: use ONLY facts and numbers from the profile and samples, copied exactly; never guess causes; if something is not in the file, say so.
Reply in the language the user wrote in. Short, friendly sentences.
"""


def _text_of(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _parse_plan(text: str):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        plan = json.loads(text[start:end + 1])
        return plan if isinstance(plan, dict) else None
    except json.JSONDecodeError:
        return None


async def make_plan(question: str, df: pd.DataFrame, prev_plan: dict | None, hint: str | None = None,
                    feedback: str | None = None) -> dict:
    sample = df.head(8).to_string(index=False, max_colwidth=25)
    dates = [f'"{c}" runs {df[c].min():%Y-%m-%d} to {df[c].max():%Y-%m-%d}' for c in df.columns
             if pd.api.types.is_datetime64_any_dtype(df[c]) and df[c].notna().any()]
    user_msg = (
        f"Today's date: {datetime.date.today():%Y-%m-%d}. Table has {len(df):,} rows.\n"
        + (("Date info: " + "; ".join(dates) + ". 'Latest date in the data' = the end of the main date column.\n") if dates else "")
        + f"\nColumns:\n{describe_schema(df)}\n\nSample rows:\n{sample}\n\n"
        + (f"Helper note: {hint}\n\n" if hint else "")
        + f"Previous plan: {json.dumps(prev_plan) if prev_plan else 'none'}\n\n"
        f"Request (may contain spelling or grammar mistakes - understand the intent): {question}"
        + (f"\n\nIMPORTANT - your previous attempt did not work:\n{feedback}\nFix the plan." if feedback else "")
    )
    plan = None
    for attempt in range(2):
        resp = await _ask(
            model=MODEL, model_override=os.getenv("GEMINI_PLAN_MODEL", "").strip() or None, max_tokens=2500, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg + ("\n\nReply with the JSON object only." if attempt else "")}],
        )
        plan = _parse_plan(_text_of(resp))
        if plan:
            break
    if not plan:  # never dead-end the user: fall back to a plain explanation of the file
        plan = {"mode": "explain", "title": "Explanation", "assumptions": ["I explained the whole file because I could not turn the request into a calculation."]}
    plan["clarify"] = None
    if plan.get("mode") not in ("table", "rows", "explain", "general"):
        plan["mode"] = "table"
    return plan


def key_figures(res: pd.DataFrame, keys: list, metric: str) -> list[str]:
    """Exact headline numbers computed by code, so the written breakdown never has to do arithmetic."""
    out = []
    try:
        if metric in res.columns and pd.api.types.is_numeric_dtype(res[metric]):
            vals = res[metric]
            out.append(f"Rows in the result table: {len(res):,}")
            if keys and len(res) > 1:
                out.append(f"Total of {metric} across all rows shown: {vals.sum():,.2f}")
                hi, lo = res.loc[vals.idxmax()], res.loc[vals.idxmin()]
                out.append(f"Highest: {' | '.join(str(hi[k]) for k in keys)} = {hi[metric]:,.2f}")
                out.append(f"Lowest: {' | '.join(str(lo[k]) for k in keys)} = {lo[metric]:,.2f}")
                out.append(f"Average of {metric} per row shown: {vals.mean():,.2f}")
    except Exception:  # noqa: BLE001
        pass
    return out


def _num_tokens(text: str) -> list[str]:
    return [t.rstrip(".,") for t in re.findall(r"\d[\d,]*\.?\d*", text)]


def _to_float(tok: str):
    try:
        return float(tok.replace(",", "").rstrip("."))
    except ValueError:
        return None


def unverified_figures(text: str, allowed_text: str) -> list[str]:
    """Numbers in the written breakdown that cannot be found in the code-computed material."""
    allowed = set()
    for tok in _num_tokens(allowed_text):
        v = _to_float(tok)
        if v is not None:
            allowed.update({round(v, 2), round(v, 1), round(v)})
    bad = []
    for tok in _num_tokens(text):
        v = _to_float(tok)
        if v is None or (v < 100 and "," not in tok and "." not in tok.rstrip(".")):
            continue  # small whole numbers (list numbers, "2 rows") are not figures
        if not ({round(v, 2), round(v, 1), round(v)} & allowed) and tok not in bad:
            bad.append(tok)
    return bad[:6]


async def summarize(question: str, res: pd.DataFrame, extra: str = "", system: str = None) -> str | None:
    try:
        table = res.head(60).to_string(index=False, max_colwidth=40)
        resp = await _ask(
            model=MODEL, max_tokens=2200, system=system or INSIGHT_PROMPT,
            messages=[{"role": "user", "content": f"Question: {question}\n\nResult table (exact):\n{table}\n\n{extra}"}],
        )
        return _text_of(resp).strip() or None
    except Exception:  # insights are a bonus; never fail the analysis
        return None


# --------------------------------------------------------------------------- #
# Executing a plan with pandas (fixed operations only - no eval)
# --------------------------------------------------------------------------- #
import re as _re

DERIVED_DATE_OPS = {"year", "month", "month_name", "weekday", "hour", "day", "date"}
DERIVED_MATH_OPS = {"abs", "ratio", "diff", "sum", "product", "pct"}


def describe_schema(df: pd.DataFrame) -> str:
    lines = []
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            lines.append(f'- "{c}" (date): {s.min():%Y-%m-%d} to {s.max():%Y-%m-%d}' if s.notna().any() else f'- "{c}" (date, empty)')
        elif pd.api.types.is_numeric_dtype(s):
            if s.notna().any():
                lines.append(f'- "{c}" (number): min {s.min():g}, max {s.max():g}, {int(s.isna().sum())} blank')
            else:
                lines.append(f'- "{c}" (number, empty)')
        else:
            vc = s.dropna().astype(str).value_counts()
            vals = vc.index.tolist() if len(vc) <= 30 else vc.head(15).index.tolist()
            more = "" if len(vc) <= 30 else f" (+{len(vc) - 15} more)"
            lines.append(f'- "{c}" (text): {s.nunique()} unique values: {[v[:40] for v in vals]}{more}')
    return "\n".join(lines)


def apply_derived(d: pd.DataFrame, specs) -> pd.DataFrame:
    """Add computed columns (date parts, ratios, differences...) from a fixed list of safe operations."""
    for sp in specs or []:
        op = str(sp.get("op", "")).lower()
        cols = sp.get("columns") or ([sp["column"]] if sp.get("column") else [])
        name = sp.get("name") or f"{op} of {' and '.join(map(str, cols))}"
        for c in cols:
            if c not in d.columns:
                raise ValueError(f'Unknown column "{c}" in derived column "{name}"')
        if op in DERIVED_DATE_OPS:
            s = d[cols[0]]
            if not pd.api.types.is_datetime64_any_dtype(s):
                s = pd.to_datetime(s, errors="coerce", format="mixed")
                if s.notna().mean() < 0.8:
                    raise ValueError(f'"{cols[0]}" is not a date column, so I cannot take its {op}.')
            d[name] = {"year": lambda: s.dt.year, "month": lambda: s.dt.month, "month_name": lambda: s.dt.month_name(),
                       "weekday": lambda: s.dt.day_name(), "hour": lambda: s.dt.hour, "day": lambda: s.dt.day,
                       "date": lambda: s.dt.strftime("%Y-%m-%d")}[op]()
        elif op in DERIVED_MATH_OPS:
            nums = [pd.to_numeric(d[c], errors="coerce").astype("float64") for c in cols]
            if op == "abs":
                d[name] = nums[0].abs()
            else:
                if len(nums) < 2:
                    raise ValueError(f'"{name}" needs two columns.')
                a, b = nums[0], nums[1]
                d[name] = {"ratio": lambda: a / b.replace(0, pd.NA).astype("float64"), "diff": lambda: a - b, "sum": lambda: a + b,
                           "product": lambda: a * b, "pct": lambda: 100 * a / b.replace(0, pd.NA).astype("float64")}[op]()
        else:
            raise ValueError(f'Unsupported derived-column operation "{op}". Use one of: '
                             + ", ".join(sorted(DERIVED_DATE_OPS | DERIVED_MATH_OPS)))
    return d


def _num(v):
    return float(_re.sub(r"[^\d.\-]", "", str(v).replace(",", "")) or "nan")


def _fuzzy_values(s: pd.Series, targets: list[str]):
    """When an exact text match finds nothing: try 'contains', then the closest spelling. Returns (values, notes)."""
    import difflib

    uniq = s.dropna().astype(str).str.strip().unique().tolist()
    low = {u.lower(): u for u in uniq}
    found, notes = [], []
    for t in targets:
        tl = str(t).strip().lower()
        hits = [u for k, u in low.items() if tl and (tl in k or k in tl)]
        if not hits:
            close = difflib.get_close_matches(tl, list(low), n=1, cutoff=0.75)
            hits = [low[close[0]]] if close else []
        if hits:
            found += hits[:5]
            notes.append(f'I matched "{t}" to "{", ".join(hits[:3])}" in your file.')
    return found, notes


def apply_filter(d: pd.DataFrame, f: dict, notes: list | None = None) -> pd.DataFrame:
    col, op, val = f["column"], f["op"], f.get("value")
    if col == "*" and op == "contains":  # search every text column ("anything mentioning Shoprite")
        text_cols = [c for c in d.columns if not pd.api.types.is_numeric_dtype(d[c]) and not pd.api.types.is_datetime64_any_dtype(d[c])]
        mask = pd.Series(False, index=d.index)
        for c in text_cols:
            mask |= d[c].astype(str).str.contains(str(val), case=False, regex=False, na=False)
        return d[mask]
    if col not in d.columns:
        raise ValueError(f'Unknown column "{col}"')
    if op == "contains":
        return d[d[col].astype(str).str.contains(str(val), case=False, regex=False, na=False)]

    s = d[col]
    is_text = not pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_datetime64_any_dtype(s)
    if pd.api.types.is_datetime64_any_dtype(s):
        conv = pd.to_datetime
    elif pd.api.types.is_numeric_dtype(s):
        conv = _num
    else:
        s = s.astype(str).str.strip().str.lower()
        conv = lambda v: str(v).strip().lower()  # noqa: E731
    if op in ("==", "!=", "in", "not_in") and is_text:
        val = _expand_aliases(val)
        if isinstance(val, list) and op in ("==", "!="):
            op = "in" if op == "==" else "not_in"
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
    out = d[ops[op]().fillna(False)]
    if out.empty and is_text and op in ("==", "in"):  # forgive spelling / partial names
        targets = v if isinstance(v, list) else [v]
        vals, n = _fuzzy_values(d[col], targets)
        if vals:
            lowered = {x.lower() for x in vals}
            out = d[s.isin(lowered)]
            if notes is not None and not out.empty:
                notes.extend(n)
    return out


def filter_hints(df: pd.DataFrame, plan: dict) -> str:
    """Real values of the columns a failed plan filtered on, so the AI can correct itself."""
    out = []
    for f in plan.get("filters") or []:
        c = f.get("column")
        if c in df.columns and not pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_datetime64_any_dtype(df[c]):
            out.append(f'Values that really exist in "{c}": {df[c].dropna().astype(str).value_counts().head(40).index.tolist()}')
        elif c in df.columns and pd.api.types.is_datetime64_any_dtype(df[c]):
            out.append(f'"{c}" covers {df[c].min():%Y-%m-%d} to {df[c].max():%Y-%m-%d}')
        elif c in df.columns and pd.api.types.is_numeric_dtype(df[c]):
            out.append(f'"{c}" ranges from {df[c].min():g} to {df[c].max():g}')
    return "\n".join(out)


def run_plan(df: pd.DataFrame, plan: dict, notes: list | None = None):
    d = apply_derived(df.copy(), plan.get("derived"))
    for f in plan.get("filters") or []:
        d = apply_filter(d, f, notes)
    if d.empty:
        raise ValueError("No rows match those filters.")

    if plan.get("mode") == "rows":  # "show me the transactions over 50,000"
        sel = [c for c in (plan.get("select") or []) if c in d.columns] or [c for c in d.columns if c != "_rows"]
        sort = plan.get("sort")
        if sort and sort.get("by") in d.columns:
            d = d.sort_values(sort["by"], ascending=bool(sort.get("ascending", False)))
        limit = min(int(plan.get("top_n") or 200), 500)
        res = d[sel].head(limit).reset_index(drop=True)
        num = [c for c in sel if pd.api.types.is_numeric_dtype(res[c])]
        return res, d, [], (num[0] if num else sel[0])

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
            if agg in ("sum", "mean", "median") and not pd.api.types.is_numeric_dtype(d[col]):
                raise ValueError(f'"{col}" is not a number column, so it cannot be used with "{agg}". '
                                 'Pick a number column, or use count / nunique for text.')
            aggs[alias] = (col, agg)

    if keys:
        res = d.groupby(keys, dropna=False).agg(**aggs).reset_index()
    else:
        res = d.assign(_all=1).groupby("_all").agg(**aggs).reset_index(drop=True)

    first_metric = list(aggs)[0]
    numeric_first = pd.api.types.is_numeric_dtype(res[first_metric])
    if (plan.get("pct_change") or plan.get("cumulative")) and keys and numeric_first:
        res = res.sort_values(keys).reset_index(drop=True)
        if plan.get("pct_change"):
            res["% change vs previous"] = (res[first_metric].pct_change() * 100).round(2)
        if plan.get("cumulative"):
            res["Running total"] = res[first_metric].cumsum()
    if plan.get("percent_of_total") and numeric_first:
        total = res[first_metric].sum()
        if total:
            res["% of total"] = (100 * res[first_metric] / total).round(2)

    for h in plan.get("having") or []:  # filter AFTER grouping: "customers with more than 5 transactions"
        mcol = h.get("metric")
        if mcol not in res.columns:
            raise ValueError(f'"having" refers to "{mcol}" which is not a result column. Result columns: {list(res.columns)}')
        hv, hop = _num(h.get("value")), h.get("op")
        cond = {">": res[mcol] > hv, ">=": res[mcol] >= hv, "<": res[mcol] < hv, "<=": res[mcol] <= hv, "==": res[mcol] == hv, "!=": res[mcol] != hv}.get(hop)
        if cond is None:
            raise ValueError(f"Unsupported 'having' operator: {hop}")
        res = res[cond]
    if res.empty:
        raise ValueError("No groups are left after applying the 'having' condition.")

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


def explain_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s) and s.notna().any():
            kind, rng = "date", f"{s.min():%Y-%m-%d} to {s.max():%Y-%m-%d}"
        elif pd.api.types.is_numeric_dtype(s) and s.notna().any():
            kind, rng = "number", f"{s.min():,.2f} to {s.max():,.2f} (total {s.sum():,.2f})"
        else:
            kind, rng = "text", ", ".join(map(str, s.dropna().astype(str).value_counts().head(3).index))
        rows.append({"Column": str(c), "Type": kind, "Filled": int(s.notna().sum()), "Unique": int(s.nunique()), "Examples / range": rng})
    return pd.DataFrame(rows)


def method_lines(df: pd.DataFrame, plan: dict, res: pd.DataFrame, filtered: pd.DataFrame, keys: list, metric: str,
                 match_notes: list | None = None) -> list[str]:
    """A plain-English, code-generated account of exactly what was done to the data."""
    lines = [f"Started with {len(df):,} rows from your file."]
    for sp in plan.get("derived") or []:
        lines.append(f'Worked out a new column "{sp.get("name")}" ({sp.get("op")} of {", ".join(map(str, sp.get("columns") or []))}).')
    cur = apply_derived(df.copy(), plan.get("derived")) if plan.get("derived") else df
    for f in plan.get("filters") or []:
        try:
            cur = apply_filter(cur, f)
            lines.append(f'Kept only rows where "{f["column"]}" {f["op"]} {f.get("value")}: {len(cur):,} rows left.')
        except Exception:  # noqa: BLE001
            break
    for n in match_notes or []:
        lines.append(n)
    if plan.get("mode") == "rows":
        lines.append(f"Showing {len(res):,} of the {len(filtered):,} matching rows" + (f", sorted by {plan['sort']['by']}." if (plan.get("sort") or {}).get("by") else "."))
        return lines
    if keys:
        lines.append("Grouped the rows by " + ", ".join(f'"{k}"' for k in keys) + f" - {len(res):,} group(s).")
    for m in plan.get("metrics") or []:
        what = {"sum": "added up", "mean": "averaged", "count": "counted", "min": "took the smallest of",
                "max": "took the largest of", "median": "took the middle value of", "nunique": "counted the different values in"}.get(m.get("agg"), m.get("agg"))
        lines.append(f'For each group I {what} {"the rows" if m.get("column") == "*" else chr(34) + str(m.get("column")) + chr(34)}.')
        col = m.get("column")
        if col in filtered.columns and filtered[col].isna().any():
            lines.append(f'Note: {int(filtered[col].isna().sum()):,} row(s) had a blank "{col}" and were not included in that calculation.')
    for h in plan.get("having") or []:
        lines.append(f'Kept only groups where "{h.get("metric")}" {h.get("op")} {h.get("value")}.')
    if plan.get("pct_change"):
        lines.append("Worked out the % change from each row to the next, in time order.")
    if plan.get("cumulative"):
        lines.append("Added a running total down the table.")
    # Independent cross-check: the groups must add back up to the overall total.
    try:
        first = (plan.get("metrics") or [{}])[0]
        simple = not plan.get("top_n") and not plan.get("having")
        if first.get("agg") == "sum" and first.get("column") in filtered.columns and simple:
            direct = float(pd.to_numeric(filtered[first["column"]], errors="coerce").sum())
            grouped = float(res[metric].sum())
            if abs(direct - grouped) <= max(0.01, abs(direct) * 1e-9):
                lines.append(f"Cross-check passed: the groups add up to {grouped:,.2f}, exactly the same as adding the whole column directly.")
            else:
                lines.append(f"WARNING: the groups add up to {grouped:,.2f} but adding the column directly gives {direct:,.2f}. Please check the file.")
        elif first.get("agg") == "count" and simple and keys:
            if int(res[metric].sum()) == len(filtered):
                lines.append(f"Cross-check passed: the group counts add up to {len(filtered):,} rows.")
    except Exception:  # noqa: BLE001
        pass
    return lines


def _result_problem(res: pd.DataFrame, plan: dict, metric: str) -> str | None:
    """Deterministic sanity check: does the result look like a real answer?"""
    if res.empty:
        return "The result table is empty."
    if plan.get("mode") != "rows" and metric in res.columns and res[metric].isna().all():
        return f'Every value of "{metric}" is blank, so the calculation produced nothing. The column may be the wrong one.'
    return None


async def analyze(df: pd.DataFrame, question: str, prev_plan: dict | None = None, insights: bool = True):
    """Full pipeline. Always tries to answer. If the AI's plan fails or gives an empty/blank result, the real error and the
    real column values are sent back to the AI so it can correct itself (up to 3 tries)."""
    df, money_note = add_money_columns(df)
    plan, feedback, ran = None, None, None
    for attempt in range(3):
        plan = await make_plan(question, df, prev_plan, money_note, feedback)
        plan = fix_plan_columns(plan, df.columns)
        if plan.get("mode") in ("explain", "general"):
            break
        match_notes: list = []
        try:
            res, filtered, keys, metric = await asyncio.to_thread(run_plan, df, plan, match_notes)
            problem = _result_problem(res, plan, metric)
        except (ValueError, KeyError, TypeError) as e:
            res = None
            problem = f"{type(e).__name__}: {e}"
            if "No rows match" in problem or "No groups are left" in problem:
                problem += "\n" + filter_hints(df, plan)
        if problem is None:
            ran = (res, filtered, keys, metric, match_notes)
            break
        feedback = f"{problem}\nThe plan was: {json.dumps(plan, default=str)[:1500]}\nAvailable columns: {list(df.columns)}"
    notes = list(plan.get("assumptions") or [])
    if money_note:
        notes.insert(0, money_note)

    if ran is None and plan.get("mode") not in ("explain", "general"):
        notes.append("I could not turn this into an exact calculation, so I explained the file instead. Try asking it a slightly different way.")
        plan = {"mode": "explain", "title": "About this file", "assumptions": notes}

    if plan.get("mode") in ("explain", "general"):
        res = explain_table(df)
        facts = json.dumps(profile_facts(df), default=str)[:6000]
        extra = (f"File profile (exact): {facts}\n\nSample rows:\n{df.head(6).to_string(index=False, max_colwidth=25)}\n\n"
                 + "\n".join(f"Helper note: {n}" for n in notes))
        text = await summarize(question, res, extra, GENERAL_PROMPT if plan.get("mode") == "general" else EXPLAIN_PROMPT) if insights else None
        if not text:
            text = (f"ANSWER: This table has {len(df):,} rows and {len(df.columns)} columns. The list below explains each column.\n"
                    "(The written explanation is not available right now; the exact column facts are in the table.)")
        plan.update({"chart": "none", "title": plan.get("title") or "About this file"})
        tail = ("\n\nMY ASSUMPTIONS:\n" + "\n".join("- " + n for n in notes)) if notes else ""
        return {"plan": plan, "res": res, "filtered": df, "keys": [], "metric": "Filled",
                "title": plan["title"], "insight": text + tail}

    res, filtered, keys, metric, match_notes = ran
    if plan.get("mode") == "rows":
        plan["chart"] = "none"
    method = method_lines(df, plan, res, filtered, keys, metric, match_notes)
    figures = key_figures(res, keys, metric)
    if plan.get("mode") == "rows":
        figures = [f"Matching rows found: {len(filtered):,} (showing {len(res):,})"] + figures
    notes += [n for n in match_notes if n not in notes]
    text = None
    if insights:
        extra = ("Key figures (exact, computed by code):\n" + "\n".join(figures) + "\n\nHow it was worked out:\n" + "\n".join(method)
                 + ("\n\nAssumptions:\n" + "\n".join(notes) if notes else ""))
        text = await summarize(question, res, extra)
        if text:
            allowed = res.head(60).to_string(index=False) + " " + extra + " " + question + " " + json.dumps(plan, default=str)
            bad = unverified_figures(text, allowed)
            if bad:
                text += "\n\nPlease double-check these figures, I could not match them directly to the result table: " + ", ".join(bad) + "."
    if not text:  # AI unavailable or off: still give the exact numbers
        text = "ANSWER: " + (f"{len(res):,} result row(s). " if len(res) else "") + "The exact figures are in the table below.\n"
        if figures:
            text += "BREAKDOWN:\n" + "\n".join("- " + f for f in figures)
    tail = "\n\nHOW THIS WAS WORKED OUT:\n" + "\n".join("- " + m for m in method)
    if notes:
        tail += "\n\nMY ASSUMPTIONS:\n" + "\n".join("- " + n for n in notes)
    return {
        "plan": plan, "res": res, "filtered": filtered, "keys": keys,
        "metric": metric, "title": plan.get("title") or "Result", "insight": text + tail,
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
