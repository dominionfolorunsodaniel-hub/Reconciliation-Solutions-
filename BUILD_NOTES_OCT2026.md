# Report Analyzer V2 build notes (October 2026)

## Included in this build
- Company Insights visualisation added to the existing historical trends workflow.
- KPI cards for reconciliation runs, matched rows, discrepancy rows, and a clearly defined row match rate.
- Interactive daily activity line chart and matched-versus-discrepancy bar chart, driven by the existing company-scoped trends API.
- Responsive layout for the analytics cards and charts.
- No V1 files or services were changed.

## Validation performed locally
- `python -m py_compile *.py` passed.
- `python billing_test.py` passed (12 tests).
- `python smoke_test.py` passed all checks.
- Inline JavaScript syntax check (`node --check`) passed.

## Not yet verified / remaining work before production
- Live Neon connection and migration behaviour cannot be verified without testing against the user's actual Neon database and Render environment.
- Email verification needs a configured transactional email provider and sender domain.
- Donation checkout, confirmed donation records/badges, and hosting-funding totals require a selected payment provider and server-side webhook verification; they are not claimed as implemented in this build.
- Maintenance announcements/broadcast history and detailed infrastructure diagnostics still require implementation and tests.
- This is a V2 working build; deploy only to the separate V2 service after reviewing the database and environment configuration. Never replace the V1 service.
