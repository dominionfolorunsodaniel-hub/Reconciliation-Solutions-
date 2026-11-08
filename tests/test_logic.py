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


class TxAuditTests(unittest.TestCase):
    def frame(self):
        import core
        rows = [  # date, ref, narration, type, amount
            ("2026-10-01", "R1", "Salary", "Credit", 500000), ("2026-10-02", "R2", "Rent", "Debit", 200000),
            ("2026-10-03", "R3", "POS", "Debit", 5000), ("2026-10-03", "R3", "POS", "Debit", 5000),      # duplicate ref + same day
            ("2026-10-04", "R4", "Airtime", "Debit", 1000), ("2026-10-05", "R5", "Airtime", "Debit", 1000), ("2026-10-06", "R6", "Airtime", "Debit", 1000),
            ("2026-10-07", "R7", "Transfer", "Credit", 90000), ("2026-10-07", "R8", "Transfer out", "Debit", 89000),   # pass-through
            ("2026-10-08", "R9", "Shop", "Debit", 3000), ("2026-10-09", "R10", "Shop", "Debit", 4000), ("2026-10-10", "R11", "Shop", "Debit", 3500),
            ("2026-10-11", "R12", "Shop", "Debit", 4500), ("2026-10-12", "R13", "Shop", "Debit", 3800), ("2026-10-13", "R14", "Shop", "Debit", 4200),
            ("2026-10-14", "R15", "Big payment", "Debit", 4000000),                                         # outlier
            ("2026-11-20", "R16", "Late", "Credit", 7000),                                                  # long quiet gap
        ]
        return pd.DataFrame({"Date": pd.to_datetime([r[0] for r in rows]), "Reference": [r[1] for r in rows], "Narration": [r[2] for r in rows],
                             core.CRED_COL: [r[3] for r in rows], core.AMT_COL: [float(r[4]) for r in rows]})

    def test_findings_and_totals(self):
        import txaudit
        a = txaudit.audit(self.frame())
        t = a["totals"]
        self.assertEqual(t["rows"], 17)
        self.assertEqual(t["money_in"], 597000.0)
        self.assertEqual(t["money_out"], 4000000 + 200000 + 10000 + 3000 + 89000 + 3000 + 4000 + 3500 + 4500 + 3800 + 4200)
        self.assertEqual(t["net_movement"], round(t["money_in"] - t["money_out"], 2))
        self.assertEqual(t["average_in"], round(597000 / 3, 2))          # over credits only
        kinds = {f["kind"]: f for f in a["findings"]}
        self.assertEqual(kinds["duplicate_reference_same"]["count"], 2)
        self.assertEqual({r["Reference"] for r in kinds["duplicate_reference_same"]["rows"]}, {"R3"})
        self.assertEqual(kinds["repeated_amount"]["count"], 3)
        self.assertIn("R15", [r["Reference"] for r in kinds["unusual_debit"]["rows"]])
        self.assertIn("pass_through", kinds)
        self.assertIn("quiet_period", kinds)

    def test_every_row_is_in_the_text_and_no_missing_claims(self):
        import txaudit
        a = txaudit.audit(self.frame())
        for needle in ("R3", "R15", "4,000,000.00", "2026-10-03", "Net movement", "Total inflows"):
            self.assertIn(needle, a["text"])
        self.assertIn("CANNOT TELL YOU", a["text"])
        self.assertIn("second file", a["text"])
        self.assertNotIn("missing from your records", a["text"].split("CANNOT TELL YOU")[0])
        self.assertEqual(len(a["findings_table"]), sum(f["count"] for f in a["findings"]))   # nothing dropped

    def test_clean_statement_has_no_false_findings(self):
        import core, txaudit
        x = pd.DataFrame({"Date": pd.to_datetime(["2026-10-01", "2026-10-05", "2026-10-09"]), "Reference": ["A", "B", "C"], "Narration": ["x", "y", "z"],
                          core.CRED_COL: ["Credit", "Debit", "Credit"], core.AMT_COL: [1234.0, 567.0, 8910.0]})
        a = txaudit.audit(x)
        self.assertEqual(a["findings"], [])
        self.assertIn("None of the checks", a["text"])


class DeepDiveTests(unittest.TestCase):
    def test_detail_lists_exact_rows(self):
        import deepdive
        df = pd.DataFrame({"Reference": [f"R{i}" for i in range(1, 11)] + ["R5"], "Date": pd.to_datetime(["2026-10-%02d" % (i + 1) for i in range(11)]),
                           "Region": ["N", "S"] * 5 + ["N"], "Amount": [100, 120, 110, 90, 105, 95, 115, 100, 9000, 1000, 105]})
        res = df.groupby("Region", as_index=False)["Amount"].sum()
        out = {"res": res, "keys": ["Region"], "metric": "Amount", "filtered": df}
        t = deepdive.detail(out, 11)
        for needle in ("BREAKDOWN", "Highest", "Unusually high values", "ref R9", "9,000.00", "DUPLICATE CHECK ON Reference", "R5", "LIMITS OF THIS ANALYSIS"):
            self.assertIn(needle, t)
        self.assertIn("(over 2 groups)", t)
        self.assertEqual(deepdive.detail({"res": None, "keys": [], "filtered": None}), "")


if __name__ == "__main__":
    unittest.main()
