# Update 2 (on top of NEW_FEATURES_OCT31.md)

- Bank statements: tested against 17 layouts (GTBank, Access, Zenith, UBA, First Bank, OPay, Moniepoint, Kuda, PalmPay and common styles; CSV, Excel and PDF). OPay-style "--" blanks now read correctly.
- Statement Score: cash-flow forecast for the next 3 months (needs 3+ months of history)
- Installable app: "Install app" button, home-screen icon, friendly offline page (nothing private is cached)
- Receipts: nicer branded PDF receipt with transfer reference and plan period; pro-forma invoice with bank details (Plan & Billing -> "Download invoice for this plan")
- Support: in-site conversations between customers and the owner, with bell notifications
- Lender API: keys in Settings -> API access; POST /api/v1/statement-score with header X-API-Key (30 calls/min/key, uses plan checks)
- Paystack is NOT included yet (still being set up).

No new environment variables. New tables are created automatically on startup.
