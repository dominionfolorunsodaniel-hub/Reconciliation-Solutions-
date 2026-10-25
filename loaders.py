"""
loaders.py - turn many file types into a plain list of rows, so core.load_report can treat them all alike.

Handles: JSON / JSON-lines, XML, HTML (also the fake ".xls" files many banks export), Word (.docx) and
plain text in any common encoding. File type is detected from the file's CONTENT first, so a wrong
or missing extension does not matter. Standard library only.
"""
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser

IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"II*\x00", b"MM\x00*")


def sniff(data: bytes, ext: str) -> str:
    """Best guess at the real kind of file: pdf, image, xlsx, xls, ods, docx, json, html, xml or text."""
    head = data[:2048]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(IMAGE_MAGIC) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    if head.startswith(b"PK"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except zipfile.BadZipFile:
            names = set()
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if "word/document.xml" in names:
            return "docx"
        if "content.xml" in names and "mimetype" in names:
            return "ods"
        return "zip"
    text = head.lstrip(b"\xef\xbb\xbf\xff\xfe\xfe\xff \t\r\n\x00")[:300].lower()
    if text.startswith((b"<!doctype html", b"<html", b"<table", b"<head", b"<body", b"<div")) or b"<table" in head.lower():
        return "html"
    if text.startswith(b"<?xml") or text.startswith(b"<"):
        return "xml"
    if text[:1] in (b"{", b"["):
        return "json"
    return {"json": "json", "jsonl": "json", "ndjson": "json", "xml": "xml", "html": "html", "htm": "html"}.get(ext, "text")


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _scalar(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = _scalar(v)
    return out


def _records_to_rows(records):
    recs = [_flatten(r) if isinstance(r, dict) else {"value": _scalar(r)} for r in records]
    cols = []
    for r in recs:
        for k in r:
            if k not in cols:
                cols.append(k)
    return [cols] + [[r.get(c) for c in cols] for r in recs]


def json_rows(data: bytes):
    text = decode_text(data).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = [json.loads(line) for line in text.splitlines() if line.strip()]  # JSON lines
    # Find the biggest list of records anywhere near the top of the document.
    best = []

    def walk(o, depth=0):
        nonlocal best
        if isinstance(o, list) and o and len(o) > len(best):
            if all(isinstance(x, list) for x in o):
                best = o
            elif any(isinstance(x, dict) for x in o) or all(not isinstance(x, (list, dict)) for x in o):
                best = o
        if depth < 3:
            for v in (o.values() if isinstance(o, dict) else o if isinstance(o, list) else []):
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)

    walk(obj)
    if not best:
        if isinstance(obj, dict):
            return _records_to_rows([obj])
        raise ValueError("I couldn't find any table-like list of records in this JSON file.")
    if all(isinstance(x, list) for x in best):
        return [[_scalar(c) for c in r] for r in best]
    return _records_to_rows(best)


def xml_rows(data: bytes):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"This XML file is not well-formed ({e}).") from e

    def tag(e):
        return e.tag.rsplit("}", 1)[-1]

    # The repeated element (e.g. <transaction>) is the row.
    best, best_n = None, 0
    for parent in root.iter():
        counts = {}
        for ch in parent:
            counts[tag(ch)] = counts.get(tag(ch), 0) + 1
        for t, n in counts.items():
            if n > best_n:
                best, best_n = (parent, t), n
    if not best or best_n < 1:
        raise ValueError("I couldn't find repeated records in this XML file.")
    parent, t = best
    records = []
    for el in parent:
        if tag(el) != t:
            continue
        rec = {f"@{k}": v for k, v in el.attrib.items()}
        kids = list(el)
        if kids:
            for k in kids:
                rec[tag(k)] = (k.text or "").strip() or None
                for ak, av in k.attrib.items():
                    rec[f"{tag(k)}@{ak}"] = av
        elif (el.text or "").strip():
            rec["value"] = el.text.strip()
        records.append(rec)
    return _records_to_rows(records)


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._stack, self._cell = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._stack[-1].append([])
        elif tag in ("td", "th") and self._stack and self._stack[-1]:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._stack and self._stack[-1]:
            self._stack[-1][-1].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_rows(data: bytes):
    p = _TableParser()
    p.feed(decode_text(data))
    tables = [t for t in p.tables if sum(len(r) for r in t) >= 4]
    if not tables:
        raise ValueError("I couldn't find a table inside this web page / HTML file.")
    return max(tables, key=lambda t: sum(len(r) for r in t))


def docx_rows(data: bytes):
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))

    def cell_text(tc):
        return " ".join("".join(t.text or "" for t in tc.iter(ns + "t")).split())

    tables = [[[cell_text(tc) for tc in tr.findall(ns + "tc")] for tr in tbl.iter(ns + "tr")] for tbl in root.iter(ns + "tbl")]
    tables = [t for t in tables if sum(len(r) for r in t) >= 4]
    if tables:
        return max(tables, key=lambda t: sum(len(r) for r in t))
    lines = ["".join(t.text or "" for t in p.iter(ns + "t")) for p in root.iter(ns + "p")]
    return text_rows("\n".join(lines))


def text_rows(text: str):
    """Plain text: delimited (comma, semicolon, tab, pipe) or columns lined up with spaces."""
    import csv

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("The file looks empty.")
    sample = "\n".join(lines[:200])
    try:
        dialect = csv.Sniffer().sniff(sample[:20000], delimiters=",;\t|")
        rows = list(csv.reader(io.StringIO("\n".join(lines)), dialect))
        if sum(len(r) >= 2 for r in rows) >= 0.6 * len(rows):
            return rows
    except csv.Error:
        pass
    spaced = [re.split(r"\s{2,}|\t", ln.strip()) for ln in lines]
    if sum(len(r) >= 2 for r in spaced) >= 0.6 * len(spaced):
        return spaced
    return [[ln.strip()] for ln in lines]  # no table structure: one column of text
