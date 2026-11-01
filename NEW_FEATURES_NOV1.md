# Update 2 (on top of NEW_FEATURES_OCT31.md)

- Bank statements: tested against 17 layouts (GTBank, Access, Zenith, UBA, First Bank, OPay, Moniepoint, Kuda, PalmPay and common styles; CSV, Excel and PDF). OPay-style "--" blanks now read correctly.
- Statement Score: cash-flow forecast for the next 3 months (needs 3+ months of history)
- Installable app: "Install app" button, home-screen icon, friendly offline page (nothing private is cached)
- Receipts: nicer branded PDF receipt with transfer reference and plan period; pro-forma invoice with bank details (Plan & Billing -> "Download invoice for this plan")
- Support: in-site conversations between customers and the owner, with bell notifications
- Lender API: keys in Settings -> API access; POST /api/v1/statement-score with header X-API-Key (30 calls/min/key, uses plan checks)
- Paystack is NOT included yet (still being set up).

No new environment variables. New tables are created automatically on startup.

# Update 3
- Ask about this: a plain-language "Ask" box on Statement Score results, Reconciliation results, Discrepancy cases and the Help page (any wording; answers only from your data; figures not found in the data are flagged; falls back to the built-in summary/help if the AI is busy). Not charged as a plan check (40 questions per hour per person).
- Reconciliation: the note box now understands normal wording ("match ref with rrn", "ignore differences under 10 naira"); matches columns that mean the same thing under different names; falls back to date + amount when there is no reference; explains how it matched; never shows a technical dead-end message.
- "How to ask" guide next to the question bar and "How to write a note" in Reconciliation.
