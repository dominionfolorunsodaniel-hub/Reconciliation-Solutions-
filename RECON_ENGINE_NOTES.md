# Reconciliation engine upgrade (Nov 2026)
- New bankrec.py: Bank vs Ledger by Reference; credit vs credit, debit vs debit; categories Matched, Credit mismatch, Debit mismatch,
  Credit/debit mismatch, Only in bank, Only in ledger, Duplicate reference, Missing amount, Missing reference. Tolerance shown in the report.
- Used automatically when both files have a reference plus credit/debit (or amount+type) columns. Report A = bank, report B = ledger.
  Other file pairs still use the previous general engine.
- PDF/Excel list every row, no caps. "Ask" with "everything / list all" returns the whole file (fullread.py).
- tests/fixtures/test_pack2_*.csv were REBUILT from the expected results; replace with the real Test Pack 2 files when available.

# Statement investigation report (build 27)
- txaudit.py inspects every row of ONE statement: duplicate references, same-day identical rows, repeated amounts, unusual values (Tukey 3xIQR, method printed),
  round numbers, busy days / 10-minute bursts, amounts just under round limits, pass-through, offsetting pairs, quiet periods (observation only).
- Each finding lists exact No., date, reference, narration, type and amount. Inflows, outflows, net movement, averages per direction, highest/lowest.
- Always states that missing/unmatched transactions cannot be judged without a second file.
- Shown on the Statement check page, in the PDF (all rows) and Excel (Investigation findings + Investigation report sheets).

# Detailed analysis on every Ask answer (build 28)
- deepdive.py appends a deterministic DETAILED ANALYSIS section (group ranking with shares, checks on the underlying rows, outliers with method, repeated amounts, duplicate references, limits) to every Ask/Analytics answer.
