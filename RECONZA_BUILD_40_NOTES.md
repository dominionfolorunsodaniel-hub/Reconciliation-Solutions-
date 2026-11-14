# RECONZA build 40 — analysis engine audit

## Root causes
1. Question path (Ask) used core.add_money_columns on the raw table: abs() of every amount, zero/negative rows counted. Ask and Full Analysis disagreed (credits 4,763,000 / 25 rows vs verified 4,748,000 / 24).
2. fullread.describe summed mixed amount columns (4,899,000) instead of the verified totals.
3. AI-busy fallback returned the generic full-file report instead of answering the question.
4. PDF banner said VERIFIED while data-quality issues remained; verification compared against the parsed table without saying so.
5. Duplicate / unusual listings used internal numbering ("No.") instead of file row numbers; duplicates with excluded rows were not shown.
6. No per-field missing, status or excluded-row sections in the PDF/Excel.

## Files and functions
- facts.py: question_frame (new), AI_WORDED, NO_MONEY_STATUSES, duplicates["with_excluded"], missing/status notes, Pattern findings sheet, verification wording/coverage scope, install_grounding wrapper.
- qa.py (new): answer, headline_table, classify_error, unavailable_message, examples.
- fullread.py: describe(df, F).
- web.py: AI_DOWN_WORDS, _no_ai_answer, /api/analyze (qdf, answer_source, retry, ai_problem), _apply_facts.
- report_pdf.py: build_analytics_pdf (3-state banner, sections 1-9).
- static/index.html: answer-source labels, "Try the AI again" button.
- tests/test_engine_audit.py (44 tests), tests/fixtures/messy_variants.csv.
- core.py untouched.

## Policy (disclosed in the report)
All analysed rows are in gross totals; settled = Completed only; negative, zero, missing-amount and bad-type rows are excluded and listed; missing/unreadable dates stay in totals but not daily; duplicates are never removed.

## Limitations
- Real Stress Test V2 file not received; tests use a reconstructed fixture.
- A column with many non-numeric amounts is not recognised as money by core.py (untouched), so the verified engine declines the file.
- Verification compares against the parsed table, not the original file bytes.
- Ambiguous slash dates are interpreted by core.py.
- HTTP endpoints not run end to end here (fastapi not installed); pipeline tested with a stubbed AI.
