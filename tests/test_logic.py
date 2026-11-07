"""Offline logic tests. Run:  python -m unittest discover -s tests -v   (needs pandas; no network, no AI)."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
from unittest import mock
os.environ["DATA_DIR"] = tempfile.mkdtemp()   # tests never touch real data
for _m in ("httpx", "dotenv"):  # only for machines without these installed; real deploys have them
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = mock.MagicMock()
import recon


class ReconTests(unittest.TestCase):
    def setUp(self):
        self.a = pd.DataFrame({"RRN": ["1", "2", "3", "4", "4"], "Amount": [100.0, 200.0, 300.0, 400.0, 400.0]})
        self.b = pd.DataFrame({"RRN": ["1", "2", "5", "4"], "Amount": [100.0, 150.0, 500.0, 400.0]})
        self.plan = {"keys": [{"a": "RRN", "b": "RRN"}], "amount": {"a": "Amount", "b": "Amount"}, "tolerance": 0.01}

    def test_statuses_and_totals(self):
        out = recon.run_recon(self.a, self.b, self.plan)
        d = out["detail"]
        self.assertEqual(int((d["Status"] == "Matched").sum()), 2)
        self.assertEqual(int((d["Status"] == "Amount mismatch").sum()), 1)
        self.assertEqual(int((d["Status"] == "Only in A").sum()), 2)   # RRN 3 and the extra 4
        self.assertEqual(int((d["Status"] == "Only in B").sum()), 1)   # RRN 5

    def test_confidence_and_findings(self):
        d = recon.run_recon(self.a, self.b, self.plan)["detail"]
        self.assertIn("Confidence", d.columns)
        self.assertTrue((d.loc[d["Status"] == "Matched", "Confidence"] == "High").all())
        f = d.loc[d["Status"] == "Amount mismatch", "Finding"].iloc[0]
        self.assertIn("partial", f.lower())
        self.assertTrue(d["Finding"].str.contains("duplicate", case=False).any())
        self.assertTrue(d["Finding"].str.contains("Missing", case=False).any())

    def test_summary_matches_detail(self):
        out = recon.run_recon(self.a, self.b, self.plan)
        s = out["summary"].set_index("Status")["Count"]
        for st, n in s.items():
            self.assertEqual(int((out["detail"]["Status"] == st).sum()), int(n))

    def test_quality_and_exposure(self):
        out = recon.run_recon(self.a, self.b, self.plan)
        q = out["quality"]
        self.assertTrue(0 <= q["score"] <= 100); self.assertEqual(len(q["factors"]), 6)
        self.assertEqual(sum(f["max"] for f in q["factors"]), 100)
        calc = [f for f in q["factors"] if f["name"] == "Calculation consistency"][0]
        self.assertEqual(calc["score"], 10)
        self.assertEqual(out["exposure"]["mismatch_value"], 50.0)    # 200 vs 150
        perfect = recon.run_recon(self.a.iloc[:2].copy(), self.a.iloc[:2].copy(), self.plan)
        self.assertEqual(perfect["quality"]["score"], 100)

    def test_reversal_flagged(self):
        a = pd.DataFrame({"RRN": ["1", "2"], "Amount": [10.0, 20.0], "Narration": ["sale", "REVERSAL of sale"]})
        b = pd.DataFrame({"RRN": ["1"], "Amount": [10.0]})
        d = recon.run_recon(a, b, self.plan)["detail"]
        self.assertTrue(d["Finding"].str.contains("reversal", case=False).any())

    def test_grouped_match_and_settlement(self):
        a = pd.DataFrame({"RRN": ["1", "2"], "Amount": [1000.0, 50.0], "Date": ["2026-01-01", "2026-01-01"]})
        b = pd.DataFrame({"RRN": ["1", "1", "2"], "Amount": [600.0, 400.0, 50.0], "Date": ["2026-01-02", "2026-01-02", "2026-01-03"]})
        out = recon.run_recon(a, b, self.plan)
        d = out["detail"]
        self.assertEqual(set(d["Status"]), {"Matched"})
        self.assertEqual(out["grouped_rows"], 2)
        self.assertTrue(d["Finding"].str.contains("Grouped").any())
        st = out["settlement"]
        self.assertEqual(float(st["Expected"].iloc[0]), 1050.0)
        self.assertGreaterEqual(float(st["Longest delay (days)"].iloc[0]), 1.0)

    def test_currency_warning(self):
        a = pd.DataFrame({"RRN": ["1", "2"], "Amount": [1.0, 2.0], "Currency": ["NGN", "USD"]})
        self.assertTrue(recon._currency_warnings(a, a))

    def test_empty_and_bad_columns(self):
        with self.assertRaises(ValueError):
            recon.run_recon(self.a, self.b, {"keys": [{"a": "Nope", "b": "RRN"}]})


class AuditChainTests(unittest.TestCase):
    def test_chain_detects_tampering(self):
        os.environ.pop("DATABASE_URL", None)
        try:
            import db
        except Exception as e:  # missing optional deps in this environment
            self.skipTest(f"db not importable here: {e}")
        if hasattr(db, "init"): db.init()
        for i in range(3): db.audit(None, f"act{i}", None, "x")
        self.assertTrue(db.verify_audit()["ok"])
        with db._db() as con: con.execute("UPDATE audit SET detail='changed' WHERE action='act1'")
        self.assertFalse(db.verify_audit()["ok"])




class CaseTests(unittest.TestCase):
    def test_ageing_and_maker_checker(self):
        os.environ.pop("DATABASE_URL", None)
        try:
            import db, enterprise_features as ef
        except Exception as e:
            self.skipTest(f"not importable here: {e}")
        if hasattr(db, "init"): db.init()
        ts = ef.stamp()
        with db._db() as con:
            con.execute("INSERT INTO discrepancy_cases(company_id,report_a,report_b,discrepancy_type,details_json,created_at,updated_at) VALUES(1,1,2,'x','{}',?,?)", (ts, ts))
            cid = con.execute("SELECT id FROM discrepancy_cases").fetchone()["id"]
        self.assertEqual(ef.case_ageing(1)["buckets"]["0-1 days"], 1)
        os.environ["MAKER_CHECKER"] = "on"
        ef.update_case(cid, 5, "in_review")
        with self.assertRaises(ValueError):
            ef.update_case(cid, 5, "resolved")          # same officer: blocked
        self.assertEqual(ef.update_case(cid, 6, "resolved")["status"], "resolved")  # different officer: allowed
        os.environ.pop("MAKER_CHECKER")




class ResetApprovalTests(unittest.TestCase):
    def test_request_approve_confirm(self):
        try:
            import db
        except Exception as e:
            self.skipTest(f"db not importable here: {e}")
        if hasattr(db, "init"): db.init()
        u = db.create_user("resetme", "OldPassword123", "analyst") if hasattr(db, "create_user") else None
        if u is None: self.skipTest("create_user not available")
        self.assertTrue(db.create_reset_request("resetme", "forgot"))
        self.assertFalse(db.create_reset_request("nobody-here"))
        req = db.list_reset_requests("pending")[0]
        res = db.decide_reset_request(req["id"], True, "admin")
        self.assertRegex(res["code"], r"^\d{6}$")
        with self.assertRaises(ValueError): db.decide_reset_request(req["id"], True, "admin")   # cannot approve twice
        with self.assertRaises(ValueError): db.confirm_password_reset("resetme", "000000" if res["code"] != "000000" else "111111", "NewPassword456")
        self.assertEqual(db.confirm_password_reset("resetme", res["code"], "NewPassword456")["username"], "resetme")


class JobTests(unittest.TestCase):
    def test_lifecycle_dedupe_and_failure(self):
        import asyncio, jobs
        j, fresh = jobs.create("upload", 1, 1, "k1", "a.csv"); self.assertTrue(fresh)
        j2, fresh2 = jobs.create("upload", 1, 1, "k1", "a.csv"); self.assertFalse(fresh2); self.assertEqual(j["id"], j2["id"])
        _, other = jobs.create("upload", 2, 1, "k1", "a.csv"); self.assertTrue(other)   # another user is separate
        seen = []

        async def ok(progress):
            progress("Validating", 20); seen.append(j["status"]); progress("Processing", 60)
            return {"id": 9, "notes": []}
        asyncio.run(jobs.run(j, ok))
        self.assertEqual(j["status"], "Completed"); self.assertEqual(seen, ["Validating"])
        _, again = jobs.create("upload", 1, 1, "k1", "a.csv"); self.assertTrue(again)   # finished jobs don't block a re-upload

        w, _ = jobs.create("upload", 1, 1, "k2")
        async def warn(progress): return {"notes": ["x"]}
        asyncio.run(jobs.run(w, warn)); self.assertEqual(w["status"], "Completed with Warnings")

        f, _ = jobs.create("upload", 1, 1, "k3")
        async def boom(progress): raise ValueError("bad file")
        asyncio.run(jobs.run(f, boom))
        self.assertEqual(f["status"], "Failed"); self.assertIn("bad file", f["error"])


class StatementTests(unittest.TestCase):
    def _df(self):
        rows = [("2026-01-02", "Salary", "R1", None, 250000, 250000), ("2026-01-03", "POS shoprite", "R2", 15000, None, 235000),
                ("2026-01-05", "Transfer from Ade", "R3", None, 40000, 275000), ("2026-01-06", "Rent", "R4", 120000, None, 155000),
                ("2026-01-07", "Transfer from Bola", "R5", None, 30000, 185000), ("2026-01-08", "Airtime", "R6", 2000, None, 183000),
                ("2026-01-09", "Transfer from Chi", "R7", None, 20000, 203000), ("2026-01-10", "Transfer from Chi", "R7", None, 20000, 223000),
                ("2026-01-11", "ATM", "R9", 50000, None, 173000), ("2026-01-12", "Cash deposit", "R10", None, 500000, 673000),
                ("2026-01-13", "Transfer from Dan", "R11", None, 10000, 683000), ("2026-01-14", "Data", "R12", 3000, None, 680000)]
        d = pd.DataFrame(rows, columns=["Date", "Narration", "Reference", "Debit", "Credit", "Balance"]); d["Date"] = pd.to_datetime(d["Date"]); return d

    def test_counts_net_references_duplicates(self):
        import statement
        r = statement.build(self._df()); t = r["totals"]
        self.assertEqual((t["credits_count"], t["debits_count"], t["rows_read"], t["rows_unreadable"]), (7, 5, 12, 0))
        self.assertEqual(t["net_movement"], 680000.0)
        tx = r["transactions"]
        self.assertEqual(list(tx["Reference"][:2]), ["R1", "R2"]); self.assertEqual(tx["Date"][0], "2026-01-02")
        self.assertEqual(int(tx["Flag"].str.contains("duplicate").sum()), 2)
        self.assertIn("NET MOVEMENT", statement.summary_text(r))

    def test_row_with_credit_and_debit_counts_twice(self):
        import statement
        d = self._df(); d.loc[1, "Credit"] = 100   # one row holding money in AND out
        t = statement.build(d)["totals"]
        self.assertEqual((t["credits_count"], t["debits_count"]), (8, 5))


class BankLedgerTests(unittest.TestCase):
    """Test Pack 2 (rebuilt from the expected results): 11 bank, 11 ledger, 8 exact, 2 amount mismatches, 1 bank-only, 1 ledger-only."""
    @classmethod
    def setUpClass(cls):
        import bankrec
        cls.br = bankrec
        fx = os.path.join(os.path.dirname(__file__), "fixtures")
        cls.bank = pd.read_csv(os.path.join(fx, "test_pack2_bank.csv"))
        cls.ledger = pd.read_csv(os.path.join(fx, "test_pack2_ledger.csv"))
        cls.out = bankrec.reconcile_bank_ledger(cls.bank, cls.ledger, 0.01, "bank.csv", "ledger.csv")

    def row(self, ref):
        d = self.out["detail"]
        return d[d["Reference"] == ref].iloc[0]

    def test_counts(self):
        c = self.out["detail"]["Status"].value_counts()
        self.assertEqual(c[self.br.MATCHED], 8)
        self.assertEqual(c[self.br.CREDIT_MISMATCH], 1)
        self.assertEqual(c[self.br.DEBIT_MISMATCH], 1)
        self.assertEqual(c[self.br.BANK_ONLY], 1)
        self.assertEqual(c[self.br.LEDGER_ONLY], 1)
        self.assertEqual(self.out["totals"]["bank"]["rows"], 11)
        self.assertEqual(self.out["totals"]["ledger"]["rows"], 11)

    def test_credit_mismatch_ra2009(self):
        r = self.row("RA2009")
        self.assertEqual((r["Bank amount"], r["Ledger amount"], r["Difference (A-B)"]), (65000, 61000, 4000))

    def test_debit_mismatch_ra2010(self):
        r = self.row("RA2010")
        self.assertEqual(r["Status"], self.br.DEBIT_MISMATCH)
        self.assertEqual((r["Bank amount"], r["Ledger amount"], abs(r["Difference (A-B)"])), (14000, 16000, 2000))

    def test_unmatched_show_real_amounts(self):
        self.assertEqual(self.row("RA2011")["Bank amount"], 33000)
        self.assertTrue(pd.isna(self.row("RA2011")["Ledger amount"]))
        self.assertEqual(self.row("RA2012")["Ledger amount"], 9500)
        self.assertTrue(pd.isna(self.row("RA2012")["Bank amount"]))

    def test_total_discrepancy(self):
        self.assertEqual(self.out["totals"]["total_discrepancy"], 48500)

    def test_report_text_has_everything(self):
        t = self.out["report_text"]
        for needle in ("RA2009", "RA2010", "RA2011", "RA2012", "65,000.00", "61,000.00", "33,000.00", "9,500.00", "48,500.00", "tolerance"):
            self.assertIn(needle, t)
        for i in range(1, 9):
            self.assertIn(f"RA200{i}", t)  # exact matches are listed too, none omitted

    def test_totals_and_averages(self):
        b = self.out["totals"]["bank"]
        self.assertEqual(b["credit_count"] + b["debit_count"], 11)
        self.assertEqual(b["net_movement"], round(b["credit_total"] - b["debit_total"], 2))
        self.assertEqual(b["credit_average"], round(b["credit_total"] / b["credit_count"], 2))  # divided by credits, not all rows
        self.assertEqual(b["debit_highest"]["reference"], "RA2006")

    def test_reference_in_both_is_not_enough(self):
        bank = pd.DataFrame({"Reference": ["X1"], "Credit": [100.0], "Debit": [None]})
        led = pd.DataFrame({"Reference": ["X1"], "Credit": [90.0], "Debit": [None]})
        self.assertEqual(self.br.reconcile_bank_ledger(bank, led)["detail"].iloc[0]["Status"], self.br.CREDIT_MISMATCH)

    def test_direction_and_missing_and_duplicates(self):
        bank = pd.DataFrame({"Reference": ["D1", "M1", "P1", "P1", None], "Credit": [100.0, 50.0, 10.0, 10.0, 7.0], "Debit": [None, None, None, None, None]})
        led = pd.DataFrame({"Reference": ["D1", "M1", "P1"], "Credit": [None, None, 10.0], "Debit": [100.0, None, None]})
        d = self.br.reconcile_bank_ledger(bank, led)["detail"]
        st = lambda ref: set(d[d["Reference"] == ref]["Status"])  # noqa: E731
        self.assertEqual(st("D1"), {self.br.DIRECTION})
        self.assertEqual(st("M1"), {self.br.MISSING_AMT})
        self.assertEqual(st("P1"), {self.br.DUPLICATE})
        self.assertEqual(int((d["Reference"] == "P1").sum()), 3)       # all three rows listed
        self.assertEqual(st("(blank)"), {self.br.NO_REF})               # nothing dropped
        self.assertEqual(len(d), 6)  # D1, M1, three P1 rows, one blank-reference row

    def test_tolerance(self):
        bank = pd.DataFrame({"Reference": ["T"], "Credit": [100.50], "Debit": [None]})
        led = pd.DataFrame({"Reference": ["T"], "Credit": [100.00], "Debit": [None]})
        self.assertEqual(self.br.reconcile_bank_ledger(bank, led, 1.0)["detail"].iloc[0]["Status"], self.br.MATCHED)
        self.assertEqual(self.br.reconcile_bank_ledger(bank, led, 0.1)["detail"].iloc[0]["Status"], self.br.CREDIT_MISMATCH)

    def test_amount_plus_type_layout(self):
        bank = pd.DataFrame({"Ref": ["A", "B"], "Amount": ["1,000.00", "(500.00)"], "Type": ["CR", "DR"]})
        led = pd.DataFrame({"Ref": ["A", "B"], "Amount": [1000, 450], "Type": ["Credit", "Debit"]})
        d = self.br.reconcile_bank_ledger(bank, led)["detail"].set_index("Reference")
        self.assertEqual(d.loc["A", "Status"], self.br.MATCHED)
        self.assertEqual(d.loc["B", "Status"], self.br.DEBIT_MISMATCH)

    def test_excel_sheets_complete(self):
        sh = self.br.sheets(self.out)
        self.assertEqual(len(sh["All records"]), len(self.out["detail"]))
        for name in ("Executive summary", "Exact matches", "Amount mismatches", "Only in bank", "Only in ledger", "Duplicate references", "Financial totals", "Corrective actions", "Methodology"):
            self.assertIn(name, sh)
        self.assertEqual(len(sh["Exact matches"]), 8)


class FullReadTests(unittest.TestCase):
    def test_everything_request_and_description(self):
        import fullread
        self.assertTrue(fullread.wants_everything("Tell me everything in this file"))
        self.assertTrue(fullread.wants_everything("list all transactions"))
        self.assertFalse(fullread.wants_everything("total sales by region"))
        df = pd.DataFrame({"Reference": ["A", "A", "B", None], "Amount": [10.0, 20.0, None, 5.0]})
        t = fullread.describe(df)
        self.assertIn("4 rows", t)
        self.assertIn("1 blank", t)           # blank amount reported, not treated as zero
        self.assertIn("total 35.00", t)
        self.assertIn("average 11.67 (over 3 values)", t)   # divided by values present, not all rows
        self.assertIn("Repeated values in Reference: A", t)


if __name__ == "__main__":
    unittest.main()
