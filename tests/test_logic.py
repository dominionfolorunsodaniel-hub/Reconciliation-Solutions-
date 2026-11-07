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


if __name__ == "__main__":
    unittest.main()
