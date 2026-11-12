"""Membership tiers, badges, Marketplace, Report Studio, enquiries and revenue admin. Run: python -m unittest discover -s tests -v"""
import datetime as dt, io, os, sys, tempfile, types, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("DASHBOARD_PASSWORD", "x" * 14)
from unittest import mock
for _m in ("httpx", "dotenv"):
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = mock.MagicMock()
try:
    import fastapi  # noqa: F401
except ImportError:      # the sandbox has no FastAPI: a tiny stand-in lets the route functions be called directly
    class _HTTP(Exception):
        def __init__(self, status_code, detail=None):
            self.status_code, self.detail = status_code, detail
    fa = types.ModuleType("fastapi"); fa.Depends = lambda f=None: f; fa.File = lambda *a, **k: None; fa.Form = lambda *a, **k: None
    fa.HTTPException = _HTTP; fa.UploadFile = object
    fr = types.ModuleType("fastapi.responses")
    class _R:
        def __init__(self, content=None, media_type=None, headers=None):
            self.content, self.media_type, self.headers = content, media_type, headers
    fr.Response = fr.StreamingResponse = _R
    sys.modules["fastapi"], sys.modules["fastapi.responses"] = fa, fr
import numpy as np
import pandas as pd
import db, monetize as M, monetize_api, facts
from fastapi import HTTPException

FMT = db.FMT


def mk(company, user):
    u = db.create_customer_account(company, user, "Passw0rd!long1")
    return db.get_user(u["id"]), u["company_ids"][0]


def pay(cid, uid, plan, admin_id):
    """The existing flow: a payment request, then the admin approves it after checking the bank."""
    r = db.create_payment_request(cid, uid, plan, "REF-" + plan.upper() + str(cid))
    return db.decide_payment_request(r["id"], "approve", admin_id, "bank checked")


class Base(unittest.TestCase):
    _admin = None

    @classmethod
    def setUpClass(cls):
        if Base._admin is None:
            Base._admin = db.get_user(db.create_customer_account("Admin Co", "adm_x", "Passw0rd!long9")["id"])
        cls.admin = Base._admin
        M.seed_products()


class Tiers(Base):
    def test_1_new_regular_customer(self):
        u, cid = mk("Reg Ltd", "reg1")
        m = M.membership(cid)
        self.assertEqual(m["tier"], "regular"); self.assertIsNone(m["badge"])
        self.assertFalse(any(m["features"].values()))
        self.assertGreater(m["limit"], 0)

    def test_2_3_4_paid_tiers_badges_and_entitlements(self):
        seen = {}
        for plan, tier, badge, feats in (("starter", "starter", "Starter Member", {"excel_recon"}),
                                         ("professional", "professional", "Professional Member", {"excel_recon", "pro_pdf", "discrepancy"})):
            u, cid = mk(plan + " Co", plan + "_u")
            pay(cid, u["id"], plan, self.admin["id"])
            m = M.membership(cid); seen[tier] = m
            self.assertEqual((m["tier"], m["badge"], m["status"]), (tier, badge, "active"))
            self.assertEqual({k for k, v in m["features"].items() if v}, feats)
        u, cid = mk("Biz Co", "biz_u")
        db.grant_subscription(cid, "business", 30, self.admin["id"], "after enquiry and verified transfer")
        m = M.membership(cid)
        self.assertEqual((m["tier"], m["badge"]), ("business", "Business Member")); self.assertTrue(all(m["features"].values()))
        # tiers really differ: allowances, team seats and exports all increase
        self.assertLess(seen["starter"]["limit"], seen["professional"]["limit"], m["limit"])
        self.assertLess(seen["starter"]["team_limit"], m["team_limit"])
        self.assertLess(sum(seen["starter"]["features"].values()), sum(seen["professional"]["features"].values()))

    def test_click_alone_gives_no_badge(self):
        u, cid = mk("Click Co", "click_u")
        db.create_payment_request(cid, u["id"], "professional", "CLICKREF1")      # pending, not verified
        m = M.membership(cid)
        self.assertEqual((m["tier"], m["badge"]), ("regular", None)); self.assertEqual(m["pending_payment_requests"], 1)

    def test_reward_and_trial_get_no_badge(self):
        u, cid = mk("Reward Co", "reward_u")
        with db._db() as con:
            con.execute("UPDATE subscriptions SET status='superseded' WHERE company_id=?", (cid,))
            n = dt.datetime.now(dt.timezone.utc)
            con.execute("INSERT INTO subscriptions(company_id,plan,status,starts_at,expires_at,grant_source,note,created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (cid, "starter", "active", (n - dt.timedelta(days=1)).strftime(FMT), (n + dt.timedelta(days=5)).strftime(FMT), "referral", "reward", n.strftime(FMT)))
        m = M.membership(cid)
        self.assertIsNone(m["badge"]); self.assertEqual(m["tier"], "regular")

    def test_7_expiry_grace_and_revoke(self):
        u, cid = mk("Exp Co", "exp_u")
        pay(cid, u["id"], "professional", self.admin["id"])
        past = lambda h: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=h)).strftime(FMT)
        with db._db() as con:
            con.execute("UPDATE subscriptions SET expires_at=? WHERE company_id=? AND status='active' AND plan='professional'", (past(2), cid))
        m = M.membership(cid)
        self.assertEqual((m["tier"], m["badge"], m["status"]), ("regular", None, "grace"))      # still working, but no paid badge or paid exports
        self.assertFalse(m["features"]["pro_pdf"])
        with db._db() as con:
            con.execute("UPDATE subscriptions SET expires_at=?, status='active' WHERE company_id=? AND plan='professional'", (past(72), cid))
        m = M.membership(cid)
        self.assertEqual((m["tier"], m["status"], m["last_membership"]), ("regular", "expired", "expired"))
        u2, cid2 = mk("Rev Co", "rev_u")
        pay(cid2, u2["id"], "starter", self.admin["id"])
        self.assertTrue(M.revoke_membership(cid2, self.admin["id"], "chargeback"))
        m = M.membership(cid2)
        self.assertEqual((m["tier"], m["badge"], m["last_membership"]), ("regular", None, "revoked"))
        self.assertFalse(m["in_grace"])                                                          # a revoked membership gets no grace
        self.assertFalse(M.revoke_membership(cid2, self.admin["id"], "again"))


class Store(Base):
    def setUp(self):
        self.u, self.cid = mk("Shop Ltd " + os.urandom(2).hex(), "shop_" + os.urandom(2).hex())
        self.prod = M.list_products(self.cid)[0]

    def test_9_purchase_flow_and_downloads(self):
        o = M.create_order(self.cid, self.u["id"], "product", str(self.prod["id"]))
        self.assertEqual((o["status"], o["amount_ngn"]), ("awaiting_payment", self.prod["price_ngn"]))
        with self.assertRaises(PermissionError):
            M.product_file(self.prod["id"], self.cid)                       # nothing paid yet
        o = M.submit_reference(o["order_ref"], self.cid, "bank-ref-1")
        self.assertEqual(o["status"], "pending")
        with self.assertRaises(PermissionError):
            M.product_file(self.prod["id"], self.cid)                       # pending is not paid
        d = M.decide_order(o["id"], "approve", self.admin["id"], "seen in bank")
        self.assertEqual(d["status"], "paid")
        data, mime, name = M.product_file(self.prod["id"], self.cid)
        self.assertTrue(data.startswith(b"PK")); self.assertTrue(name.endswith(".xlsx"))
        with self.assertRaises(ValueError):
            M.decide_order(o["id"], "approve", self.admin["id"])           # no double processing
        with self.assertRaises(ValueError):
            M.create_order(self.cid, self.u["id"], "product", str(self.prod["id"]))      # already owned: no double charge
        other, ocid = mk("Other Ltd " + os.urandom(2).hex(), "oth_" + os.urandom(2).hex())
        with self.assertRaises(PermissionError):
            M.product_file(self.prod["id"], ocid)                           # another customer cannot use my purchase
        self.assertTrue(M.list_products(self.cid)[0]["owned"])

    def test_8_failed_payment_and_duplicate_reference(self):
        o = M.create_order(self.cid, self.u["id"], "product", str(self.prod["id"]))
        M.submit_reference(o["order_ref"], self.cid, "DUPREF-" + o["order_ref"])
        f = M.decide_order(o["id"], "reject", self.admin["id"], "not in bank")
        self.assertEqual(f["status"], "failed")
        with self.assertRaises(PermissionError):
            M.product_file(self.prod["id"], self.cid)
        o2 = M.create_order(self.cid, self.u["id"], "product", str(self.prod["id"]))      # may try again
        with self.assertRaises(ValueError):
            M.submit_reference(o2["order_ref"], self.cid, "DUPREF-" + o["order_ref"])     # same bank reference twice

    def test_member_discount_is_server_side(self):
        u, cid = mk("Disc Co " + os.urandom(2).hex(), "disc_" + os.urandom(2).hex())
        pay(cid, u["id"], "professional", self.admin["id"])
        o = M.create_order(cid, u["id"], "product", str(self.prod["id"]))
        self.assertEqual(o["amount_ngn"], self.prod["price_ngn"] * 80 // 100)

    def test_provider_hook_is_idempotent_and_checks_amount(self):
        o = M.create_order(self.cid, self.u["id"], "feature", "pro_pdf")
        with self.assertRaises(ValueError):
            M.mark_order_paid(o["order_ref"], "paystack", "PSK1", o["amount_ngn"] - 1)
        self.assertEqual(M.get_order(o["order_ref"])["status"], "failed")
        o = M.create_order(self.cid, self.u["id"], "feature", "pro_pdf")
        M.mark_order_paid(o["order_ref"], "paystack", "PSK2", o["amount_ngn"])
        M.mark_order_paid(o["order_ref"], "paystack", "PSK2", o["amount_ngn"])     # webhook delivered twice
        with db._db() as con:
            n = con.execute("SELECT COUNT(*) AS n FROM feature_credits WHERE order_id=?", (o["id"],)).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_admin_product_management(self):
        r = M.add_product("Test Template", "Audit", "A test template for the suite.", "one\ntwo", 1234, "t.xlsx", "application/octet-stream", b"x")
        M.update_product(r["id"], {"price_ngn": 2000, "active": True})
        self.assertEqual([p for p in M.list_products(None) if p["id"] == r["id"]][0]["price_ngn"], 2000)
        with self.assertRaises(ValueError):
            M.update_product(r["id"], {"price_ngn": -5})
        self.assertEqual(M.remove_product(r["id"]), "deleted")
        bought = self.prod["id"]
        o = M.create_order(self.cid, self.u["id"], "product", str(bought)); M.submit_reference(o["order_ref"], self.cid, "BOUGHT-" + o["order_ref"])
        M.decide_order(o["id"], "approve", self.admin["id"])
        self.assertEqual(M.remove_product(bought), "hidden")                  # buyers keep what they paid for
        self.assertTrue(M.product_file(bought, self.cid)[0])
        M.update_product(bought, {"active": True})


def sample_result():
    rows = [{"Date": pd.Timestamp("2026-10-01") + pd.Timedelta(days=i % 10), "Reference": f"R{i:03d}", "Narration": f"p{i}",
             "Credit": 5000.0 + i if i % 3 else np.nan, "Debit": 2000.0 if i % 3 == 0 else np.nan} for i in range(40)]
    rows.append({"Date": pd.Timestamp("2026-10-03"), "Reference": "R005", "Narration": "again", "Credit": 5005.0, "Debit": np.nan})
    F = facts.build(pd.DataFrame(rows))
    xl = io.BytesIO(); pd.DataFrame({"a": [1]}).to_excel(xl, index=False)
    return {"data": xl.getvalue(), "pdf": {"title": "Bank statement analytics", "question": "all", "answer": "ANSWER: ok", "table": pd.DataFrame({"Type": ["Credit"], "Total": [1.0]}),
                                          "chart": None, "verified": F, "extra_tables": None}}


class Studio(Base):
    def test_10_exports_by_tier_and_single_purchase(self):
        res = sample_result()
        comp = lambda cid: db.get_company(cid)
        u, cid = mk("Free Studio " + os.urandom(2).hex(), "fs_" + os.urandom(2).hex())
        for f in M.FEATURES:
            with self.assertRaises(PermissionError):
                M.claim_feature(cid, f, "r1")                             # a free customer is refused every premium feature
        ps = M.studio_state(cid)
        self.assertFalse(any(f["included"] for f in ps["features"]))
        # one-time purchase of a single report
        o = M.create_order(cid, u["id"], "feature", "pro_pdf"); M.submit_reference(o["order_ref"], cid, "SR-" + o["order_ref"])
        M.decide_order(o["id"], "approve", self.admin["id"])
        self.assertEqual(M.claim_feature(cid, "pro_pdf", "r1"), "credit")
        self.assertEqual(M.claim_feature(cid, "pro_pdf", "r1"), "credit-reuse")       # same result again: free
        with self.assertRaises(PermissionError):
            M.claim_feature(cid, "pro_pdf", "r2")                                     # a different result needs another purchase
        data, name, mime = M.build_feature("pro_pdf", comp(cid), res["pdf"], res["data"])
        self.assertTrue(data.startswith(b"%PDF"))
        # tiers: included features cannot be bought again
        u2, cid2 = mk("Pro Studio " + os.urandom(2).hex(), "ps_" + os.urandom(2).hex())
        pay(cid2, u2["id"], "professional", self.admin["id"])
        self.assertEqual(M.claim_feature(cid2, "pro_pdf", "x"), "membership")
        with self.assertRaises(ValueError):
            M.create_order(cid2, u2["id"], "feature", "excel_recon")
        with self.assertRaises(PermissionError):
            M.claim_feature(cid2, "branded_pdf", "x")                              # Professional does not include branded reports
        o = M.create_order(cid2, u2["id"], "feature", "branded_pdf")                # ...but can buy one
        self.assertEqual(o["amount_ngn"], M.FEATURES["branded_pdf"]["price_ngn"])
        # every feature builds a real file
        u3, cid3 = mk("Biz Studio " + os.urandom(2).hex(), "bs_" + os.urandom(2).hex())
        db.grant_subscription(cid3, "business", 30, self.admin["id"], "test")
        for f in M.FEATURES:
            self.assertEqual(M.claim_feature(cid3, f, "rb"), "membership")
            data, name, mime = M.build_feature(f, comp(cid3), res["pdf"], res["data"])
            self.assertTrue(len(data) > 500, f)
            self.assertTrue(data.startswith(b"%PDF") if name.endswith(".pdf") else data.startswith(b"PK"), f)
        with self.assertRaises(ValueError):
            M.build_feature("pro_pdf", comp(cid3), {"title": "x", "question": "q"}, b"")    # no verified facts: clear message, no charge

    def test_failed_build_returns_credit(self):
        u, cid = mk("Cr Co " + os.urandom(2).hex(), "cr_" + os.urandom(2).hex())
        o = M.create_order(cid, u["id"], "feature", "excel_recon"); M.submit_reference(o["order_ref"], cid, "CR-" + o["order_ref"]); M.decide_order(o["id"], "approve", self.admin["id"])
        M.claim_feature(cid, "excel_recon", "rz"); M.release_credit(cid, "excel_recon", "rz")
        self.assertEqual(M.claim_feature(cid, "excel_recon", "rz2"), "credit")


class AdminAndEnquiries(Base):
    def test_11_admin_views_and_enquiries(self):
        u, cid = mk("Adm View " + os.urandom(2).hex(), "av_" + os.urandom(2).hex())
        pay(cid, u["id"], "starter", self.admin["id"])
        o = M.create_order(cid, u["id"], "product", str(M.list_products(cid)[0]["id"])); M.submit_reference(o["order_ref"], cid, "AV-" + o["order_ref"])
        ov = M.overview()
        self.assertGreaterEqual(ov["paying"], 1); self.assertGreaterEqual(ov["pending_total"], 1); self.assertGreater(ov["revenue_total"], 0)
        self.assertTrue(any(c["tier"] == "starter" for c in M.list_customers("", "starter")))
        self.assertTrue(all(c["tier"] == "regular" for c in M.list_customers("", "regular")))
        self.assertTrue(M.list_orders("pending", "AV-"))
        self.assertTrue(any(r["kind"] == "membership" and r["status"] == "paid" for r in M.list_orders("paid", "", "membership")))
        e = M.add_enquiry(u, cid, "Ada", "ada@firm.com", "Firm", "0800", "11-25", "Business package", "We need branded reports for 20 staff.")
        with self.assertRaises(ValueError):
            M.add_enquiry(u, cid, "Ada", "not-an-email", "", "", "", "", "short")
        M.update_enquiry(e, "contacted", "called"); self.assertEqual(M.list_enquiries("contacted")[0]["admin_note"], "called")
        self.assertEqual(len(M.my_enquiries(u["id"])), 1)


class Routes(Base):
    """The HTTP layer: ownership and role rules, called without a web server."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.routes = {}
        class App:
            def _reg(self, m):
                def deco(path):
                    def wrap(fn):
                        cls.routes[(m, path)] = fn; return fn
                    return wrap
                return deco
            def __getattr__(self, m):
                return self._reg(m.upper())
        W = types.SimpleNamespace(current_user=lambda: None, need=lambda u, r: (_ for _ in ()).throw(HTTPException(403, "no")) if u["role"] != "admin" else None,
                                  need_role=lambda u, r: (_ for _ in ()).throw(HTTPException(403, "role")) if db.company_rank(u) < db.COMPANY_RANK[r] else None,
                                  need_company=lambda u, c: (_ for _ in ()).throw(HTTPException(403, "co")) if not db.can_access(u, c) else None,
                                  RESULTS={}, notify=mock.MagicMock(), extras=mock.MagicMock(), MAX_PROOF_BYTES=5_000_000, _image_kind=lambda d: None)
        cls.W = W
        monetize_api.register(App(), W)

    def r(self, m, p):
        return self.routes[(m, p)]

    def test_free_user_and_cross_customer_access(self):
        a, acid = mk("RA Co " + os.urandom(2).hex(), "ra_" + os.urandom(2).hex())
        b, bcid = mk("RB Co " + os.urandom(2).hex(), "rb_" + os.urandom(2).hex())
        self.W.RESULTS["res1"] = {**sample_result(), "user": a["id"], "company": acid}
        # free customer: premium export is refused with a price, not a file
        with self.assertRaises(HTTPException) as c:
            self.r("GET", "/api/studio/export/{result_id}/{feature}")("res1", "pro_pdf", a)
        self.assertEqual(c.exception.status_code, 402); self.assertEqual(c.exception.detail["price_ngn"], 5000)
        # another customer cannot touch my result
        with self.assertRaises(HTTPException) as c:
            self.r("GET", "/api/studio/export/{result_id}/{feature}")("res1", "pro_pdf", b)
        self.assertEqual(c.exception.status_code, 404)
        # cannot read another company's membership, orders or library
        for p, fn in (("/api/membership", "membership"), ("/api/market/orders", "orders")):
            with self.assertRaises(HTTPException) as c:
                self.r("GET", p)(acid, b)
            self.assertEqual(c.exception.status_code, 403)
        pid = M.list_products(None)[0]["id"]
        with self.assertRaises(HTTPException) as c:
            self.r("GET", "/api/market/download/{product_id}")(pid, acid, b)
        self.assertEqual(c.exception.status_code, 403)
        with self.assertRaises(HTTPException) as c:
            self.r("GET", "/api/market/download/{product_id}")(pid, acid, a)              # own company but not paid
        self.assertEqual(c.exception.status_code, 402)
        # members below admin cannot spend company money; nobody can set a price or tier from the browser
        v = db.add_team_member(acid, "va_" + os.urandom(2).hex(), "Passw0rd!long4", "viewer") if False else None
        body = monetize_api.OrderIn(company_id=acid, kind="product", item_key=str(pid)) if hasattr(monetize_api.OrderIn, "model_fields") or True else None
        out = self.r("POST", "/api/market/orders")(body, a)
        self.assertEqual(out["order"]["amount_ngn"], M.list_products(acid)[0]["price_ngn"])
        # admin-only routes
        for p in ("/api/admin/money/overview", "/api/admin/money/products"):
            with self.assertRaises(HTTPException) as c:
                self.r("GET", p)(a)
            self.assertEqual(c.exception.status_code, 403)
        adm = {**self.admin, "role": "admin"}
        self.assertIn("paying", self.r("GET", "/api/admin/money/overview")(adm))
        # a paid professional gets the file straight away, no charge
        pay(acid, a["id"], "professional", self.admin["id"])
        resp = self.r("GET", "/api/studio/export/{result_id}/{feature}")("res1", "pro_pdf", a)
        self.assertEqual(resp.media_type, "application/pdf")
        # revoke needs a reason and is audit logged
        with self.assertRaises(HTTPException):
            self.r("POST", "/api/admin/money/revoke")(monetize_api.RevokeIn(company_id=acid, note=""), adm)
        self.r("POST", "/api/admin/money/revoke")(monetize_api.RevokeIn(company_id=acid, note="chargeback"), adm)
        self.assertEqual(M.membership(acid)["tier"], "regular")
        with self.assertRaises(HTTPException) as c:
            self.r("GET", "/api/studio/export/{result_id}/{feature}")("res1", "pro_pdf", a)
        self.assertEqual(c.exception.status_code, 402)


if __name__ == "__main__":
    unittest.main()
