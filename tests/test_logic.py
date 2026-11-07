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


if __name__ == "__main__":
    unittest.main()
