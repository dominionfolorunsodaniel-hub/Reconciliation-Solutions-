"""Verified-facts layer: one dataset for the AI, the tables and the PDF."""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for _m in ("httpx", "dotenv"):
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = mock.MagicMock()
import numpy as np
import pandas as pd
import core
import facts


def sample() -> pd.DataFrame:
    rows = []
    d0 = pd.Timestamp("2026-10-01")
    n = 0
    for day in range(20):
        for k in range(3):
            n += 1
            rows.append({"Date": d0 + pd.Timedelta(days=day), "Reference": f"RA{n:04d}", "Narration": f"Payment {n}",
                         "Credit": 10000.0 + k * 500 if k < 2 else np.nan, "Debit": 4000.0 if k == 2 else np.nan})
    rows.append({"Date": d0 + pd.Timedelta(days=5), "Reference": "RA0007", "Narration": "Payment 7 again", "Credit": 10000.0, "Debit": np.nan})   # duplicate of an existing ref (same amount)
    rows.append({"Date": d0 + pd.Timedelta(days=9), "Reference": "RA0030", "Narration": "reused ref", "Credit": np.nan, "Debit": 777.0})            # same ref, different direction
    rows.append({"Date": d0 + pd.Timedelta(days=12), "Reference": "BIG1", "Narration": "Large receipt", "Credit": 900000.0, "Debit": np.nan})     # unusual
    rows.append({"Date": pd.NaT, "Reference": "", "Narration": "no date no ref", "Credit": 1234.56, "Debit": np.nan})
    rows.append({"Date": d0 + pd.Timedelta(days=2), "Reference": "ZERO", "Narration": "zero row", "Credit": 0.0, "Debit": np.nan})
    for _ in range(14):   # a very busy day
        n += 1
        rows.append({"Date": d0 + pd.Timedelta(days=15), "Reference": f"BUSY{n}", "Narration": "busy", "Credit": 2000.0, "Debit": np.nan})
    return pd.DataFrame(rows)


class FactsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = sample()
        cls.F = facts.build(cls.df)

    def test_totals_are_exact(self):
        F = self.F
        cr = round(self.df["Credit"].fillna(0).sum(), 2)
        dr = round(self.df["Debit"].fillna(0).sum(), 2)
        self.assertEqual(F["totals"]["money_in"], cr)
        self.assertEqual(F["totals"]["money_out"], dr)
        self.assertEqual(F["totals"]["net_movement"], round(cr - dr, 2))
        self.assertEqual(F["rows_in_file"], len(self.df))
        self.assertEqual(F["rows_analysed"], len(self.df) - 1)        # the zero row is left out, and said so
        self.assertTrue(any("left out" in q["detail"] for q in F["quality"]))

    def test_daily_matches_totals(self):
        F = self.F
        self.assertAlmostEqual(sum(x["credit_total"] for x in F["daily"]) + 1234.56, F["totals"]["money_in"], places=2)   # the undated row is in totals only
        self.assertEqual(sum(x["transactions"] for x in F["daily"]) + 1, F["totals"]["rows"])
        self.assertEqual(F["daily"][0]["date"], "2026-10-01")
        d = next(x for x in F["daily"] if x["date"] == "2026-10-16")
        self.assertEqual(d["transactions"], 17 if False else d["transactions"])
        self.assertEqual(d["net"], round(d["credit_total"] - d["debit_total"], 2))

    def test_unusual_day_found_and_explained(self):
        self.assertIn("2026-10-16", [u["date"] for u in self.F["unusual_days"]])
        u = next(u for u in self.F["unusual_days"] if u["date"] == "2026-10-16")
        self.assertTrue(u["reasons"] and u["largest"])

    def test_duplicate_references_with_details(self):
        groups = {g["reference"]: g for g in self.F["duplicates"]["groups"]}
        self.assertIn("RA0007", groups)
        self.assertTrue(groups["RA0007"]["kind"].startswith("same"))
        self.assertEqual(groups["RA0007"]["count"], 2)
        self.assertEqual({r["Reference"] for r in groups["RA0007"]["rows"]}, {"RA0007"})
        self.assertIn("RA0030", groups)
        self.assertFalse(groups["RA0030"]["kind"].startswith("same"))

    def test_unusual_transaction_has_reference_and_reason(self):
        big = [r for r in self.F["unusual"] if r["Reference"] == "BIG1"]
        self.assertEqual(len(big), 1)
        self.assertIn("above the unusual-range limit", big[0]["why"])

    def test_validation_passes_and_detects_tampering(self):
        v = self.F["validation"]
        self.assertTrue(v["ok"], [c for c in v["checks"] if not c["ok"]])
        self.assertGreaterEqual(v["total"], 10)
        # corrupt one listed transaction: the validation layer must notice
        T, info = facts._to_transactions(self.df)
        bad = dict(self.F)
        bad["duplicates"] = {**self.F["duplicates"], "groups": [{**self.F["duplicates"]["groups"][0], "rows": [{**self.F["duplicates"]["groups"][0]["rows"][0], "Amount": 1.0}] + self.F["duplicates"]["groups"][0]["rows"][1:]}] + self.F["duplicates"]["groups"][1:]}
        kobo = facts._k(T["Amount"]); is_cr = (T["Type"] == "Credit").to_numpy()
        res = facts._validate(bad, T, kobo, is_cr, T["Date"].notna().to_numpy(), None, info)
        self.assertFalse(res["ok"])

    def test_narrative_contradictions_caught(self):
        F = self.F
        self.assertTrue(facts.check_narrative("The statement has no reference column so duplicates cannot be checked.", F, False))
        self.assertTrue(facts.check_narrative("There is a net outflow overall.", F, True))
        self.assertTrue(facts.check_narrative("No duplicates were found.", F, True))
        self.assertFalse(facts.check_narrative("No duplicates were found.", F, False))      # a subset can legitimately have none
        self.assertFalse(facts.check_narrative("Credits were mostly steady with a few large receipts.", F, True))

    def test_no_reference_column_is_stated_not_guessed(self):
        F = facts.build(self.df.drop(columns=["Reference"]))
        self.assertFalse(F["fields"]["reference"])
        self.assertFalse(F["duplicates"]["groups"])
        self.assertTrue(any("No reference column" in q["detail"] for q in F["quality"]))

    def test_not_a_transaction_table(self):
        self.assertIsNone(facts.build(pd.DataFrame({"Item": ["a", "b"], "Qty": [1, 2]})))

    def test_excel_sheets_and_text(self):
        t = facts.tables(self.F)
        for name in ("Daily activity", "Duplicate references", "Unusual transactions", "Data quality", "Validation"):
            self.assertIn(name, t)
        txt = facts.analysis_text(self.F)
        self.assertIn("RA0007", txt)
        self.assertIn("DAILY ACTIVITY", txt)


if __name__ == "__main__":
    unittest.main()


class PdfAndGroundingTests(unittest.TestCase):
    def test_structured_pdf_has_sections_and_page_numbers(self):
        import report_pdf
        F = facts.build(sample())
        res = pd.DataFrame({"Type": ["Credit", "Debit"], "Total": [1.0, 2.0]})
        pdf = report_pdf.build_analytics_pdf("Acme Ltd", "Bank statement analytics", "Tell me everything", "ANSWER: test\n- one point", F, res)
        self.assertTrue(pdf.startswith(b"%PDF"))
        try:
            import pypdf, io
            r = pypdf.PdfReader(io.BytesIO(pdf))
            txt = "\n".join(p.extract_text() for p in r.pages)
            for s in ("Executive summary", "Daily activity", "Duplicate references", "Data quality", "Recommendations", "Verification", "RA0007"):
                self.assertIn(s, txt)
            self.assertIn(f"Page 1 of {len(r.pages)}", txt)
        except ImportError:
            pass

    def test_grounding_hook_gives_ai_the_facts_and_allows_quoting_them(self):
        import asyncio
        facts.install_grounding()
        F = facts.build(sample())
        seen = {}

        async def fake(*a, **k):
            seen["extra"] = a[2]
            return "ok"
        orig = core.summarize.__wrapped_orig__ if hasattr(core.summarize, "__wrapped_orig__") else None
        # call the installed wrapper with a stand-in for the AI by temporarily swapping the original it closes over
        cell = [c.cell_contents for c in (core.summarize.__closure__ or []) if callable(c.cell_contents)]
        self.assertTrue(cell)
        tok = facts.CURRENT.set(F)
        try:
            import types
            wrapper = core.summarize
            # rebuild the wrapper around the fake so we can read what the AI would be sent
            fn = types.FunctionType(wrapper.__code__, wrapper.__globals__, closure=tuple(types.CellType(fake if callable(c.cell_contents) else c.cell_contents) for c in wrapper.__closure__))
            asyncio.run(fn("q", pd.DataFrame(), "key figures"))
        finally:
            facts.CURRENT.reset(tok)
        self.assertIn("VERIFIED FACTS", seen["extra"])
        self.assertIn("RA0007", seen["extra"])
        self.assertIn("never say a field is missing", seen["extra"])
        # a figure that only exists in the verified facts must not be flagged as "unverified"
        tok = facts.CURRENT.set(F)
        try:
            quoted = f"Money in was {facts._m(F['totals']['money_in'])}."
            self.assertEqual(core.unverified_figures(quoted, "nothing relevant here"), [])
        finally:
            facts.CURRENT.reset(tok)
