# Reconciliation engine upgrade (Nov 2026)
- New bankrec.py: Bank vs Ledger by Reference; credit vs credit, debit vs debit; categories Matched, Credit mismatch, Debit mismatch,
  Credit/debit mismatch, Only in bank, Only in ledger, Duplicate reference, Missing amount, Missing reference. Tolerance shown in the report.
- Used automatically when both files have a reference plus credit/debit (or amount+type) columns. Report A = bank, report B = ledger.
  Other file pairs still use the previous general engine.
- PDF/Excel list every row, no caps. "Ask" with "everything / list all" returns the whole file (fullread.py).
- tests/fixtures/test_pack2_*.csv were REBUILT from the expected results; replace with the real Test Pack 2 files when available.
