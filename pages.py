"""Public SEO pages (/for/<niche>) and the changelog page (/whats-new). Plain server-rendered HTML, no user input."""
from __future__ import annotations

import html
import json
from pathlib import Path

_STATIC = Path(__file__).parent / "static"

NICHES = {
    "pos-agents": {
        "name": "POS agents",
        "h1": "Reconcile your POS and bank records without the headache",
        "intro": "If you run a POS or mobile-money point, the money in your bank or wallet statement should match what you recorded. Finding the difference by eye takes hours.",
        "points": ["Upload your POS or wallet report and your bank statement, then compare them in one step.",
                   "See which transactions are missing, doubled or different in amount.",
                   "Ask questions in plain words, such as \"total failed transactions this week\".",
                   "Download a PDF with your own business name and logo."],
    },
    "atm-operators": {
        "name": "ATM operators",
        "h1": "ATM reports, settlement and cash planning in one place",
        "intro": "ATM operators deal with switch reports, bank settlement and cash loading. Differences between them are hard to trace by hand.",
        "points": ["Compare ATM transaction reports against settlement files.",
                   "Ask for totals by ATM, branch, day or failed transaction type.",
                   "Get an estimate of cash needed for the coming days from past withdrawals.",
                   "Keep every difference as a case you can mark resolved."],
    },
    "petrol-stations": {
        "name": "petrol stations",
        "h1": "Match pump sales to what actually reached the bank",
        "intro": "Petrol stations take cash, transfers and POS payments all day. At the end of the month the totals often do not agree.",
        "points": ["Compare your sales report with your bank or POS statement.",
                   "Spot days where the money received is lower than sales recorded.",
                   "Break totals down by day, attendant or payment type when your file has those columns.",
                   "Share a read-only link with your accountant."],
    },
    "supermarkets": {
        "name": "supermarkets and shops",
        "h1": "Month-end checks for shops with many payment channels",
        "intro": "Shops collect from tills, POS machines and transfers. Month-end reconciliation means comparing several files that never look the same.",
        "points": ["Upload CSV, Excel or PDF reports from different sources.",
                   "Reconcile till sales against bank credits and see what is left over.",
                   "Download a month-end pack with a PDF and an Excel file.",
                   "Use it from your phone or computer."],
    },
}

_CSS = """*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,sans-serif;color:#08153a;background:#f5f8ff;line-height:1.6}
.w{max-width:820px;margin:0 auto;padding:24px 18px}a{color:#0b5ce6}header a.b{font-weight:800;text-decoration:none;color:#08153a}
h1{font-size:clamp(28px,5vw,42px);line-height:1.15;margin:28px 0 12px}.card{background:#fff;border:1px solid #dbe3f2;border-radius:16px;padding:22px;margin:18px 0}
li{margin:8px 0}.btn{display:inline-block;background:#0b5ce6;color:#fff;padding:14px 22px;border-radius:999px;font-weight:700;text-decoration:none}
footer{color:#5b6785;font-size:14px;padding:30px 18px;text-align:center}.mute{color:#5b6785}"""


def _page(title: str, desc: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><meta name="description" content="{html.escape(desc)}">'
            '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">'
            f'<style>{_CSS}</style></head><body><div class="w"><header><a class="b" href="/">Reconciliation Solutions</a></header>{body}</div>'
            '<footer>&copy; Reconciliation Solutions &middot; <a href="/">Home</a> &middot; <a href="/whats-new">What\'s new</a> &middot; '
            '<a href="https://t.me/COLDFX26">Telegram</a></footer></body></html>')


def niche_page(slug: str) -> str | None:
    n = NICHES.get(slug)
    if not n:
        return None
    items = "".join(f"<li>{html.escape(p)}</li>" for p in n["points"])
    body = (f'<h1>{html.escape(n["h1"])}</h1><p class="mute">For {html.escape(n["name"])}</p><div class="card"><p>{html.escape(n["intro"])}</p>'
            f'<ul>{items}</ul><p><a class="btn" href="/">Start free</a></p></div>'
            '<p class="mute">Reconciliation Solutions compares reports and statements you upload. It does not move money or connect to your bank.</p>')
    return _page(f'Reconciliation for {n["name"]} | Reconciliation Solutions', n["intro"], body)


def changelog_page() -> str:
    try:
        items = json.loads((_STATIC / "changelog.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        items = []
    rows = ""
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        pts = "".join(f"<li>{html.escape(str(p))}</li>" for p in it.get("changes", []))
        rows += f'<div class="card"><b>{html.escape(str(it.get("date", "")))}</b><h3 style="margin:6px 0">{html.escape(str(it.get("title", "")))}</h3><ul>{pts}</ul></div>'
    body = "<h1>What's new</h1><p class=\"mute\">Recent improvements to Reconciliation Solutions.</p>" + (rows or "<p>Nothing yet.</p>")
    return _page("What's new | Reconciliation Solutions", "Recent improvements to Reconciliation Solutions.", body)
