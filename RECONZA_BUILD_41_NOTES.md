# RECONZA build 41 — Savings Calculator ("See what you could save")

Changed file: static/index.html only (the #savings section, its CSS block `szc-css`, and its script). No backend, database or other section touched.

## What changed
- Same three inputs (ids calcHours, calcRate, calcPct, defaults 10 / 3000 / 70) with the new descriptions.
- Formulas: weekly = hours x cost x pct; monthly = weekly x 52 / 12; annual = weekly x 52; hours recovered = hours x pct (x 52 / 12 for monthly, x 52 for annual); workload reduction = pct.
- Annual Cost Savings is the largest figure; Annual Hours Recovered and Reduction in Manual Workload are the two prominent cards; weekly/monthly/annual breakdown, today-vs-RECONZA bars.
- Live updates on every keystroke, Reset button, inline validation (blank/invalid, negative, pct over 100, absurdly large values) with results shown as dashes until fixed.
- Disclaimer text added under results. "Start free" button reuses the existing sign-up handler.
- window.reconzaSavings(hours, rate, pct) exposes the pure calculation for testing.

## Tested
Predeploy check passes. Browser tests (Playwright): 8 input sets vs independent formula, 6 invalid cases, reset, 100% and 0%, huge values, no horizontal overflow at 360 / 768 / 1280 px, no JS errors.
