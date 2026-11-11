# Build 35 - verified analytics

One verified dataset (facts.py) feeds the AI wording, the on-screen tables, Excel and the PDF.
- Totals summed in whole kobo (integers) and recounted with exact decimals; 10+ cross-checks run before reporting (daily sums = totals, every listed transaction matches its uploaded row, duplicates recounted a second way, row accounting).
- Daily summary with unusual days (Tukey far-out rule on count and value; needs 8+ days).
- Duplicate references: all rows, details listed; unusual transactions with reasons; data-quality checks (missing dates/references, rows left out and why).
- AI: given the verified facts through a wrapper (facts.install_grounding) - core.py is NOT edited. Wording that contradicts the facts (says reference/date missing when present, wrong net direction, "no duplicates" for a whole-file answer) is replaced by exact figures.
- Ask answers, Excel (6 new sheets), Statement Score and PDF all use it. New PDF: build_analytics_pdf. Files that are not money in/out behave exactly as before.
- Tests: tests/test_facts.py (12 tests).
