"""Role unlocks, join-company requests, welcome coupon, coupon on orders. Run: python -m unittest discover -s tests -v"""
import os, sys, tempfile, types, unittest
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
except ImportError:
    class _HTTP(Exception):
        def __init__(self, status_code, detail=None):
            self.status_code, self.detail = status_code, detail
    fa = types.ModuleType("fastapi"); fa.Depends = lambda f=None: f; fa.File = lambda *a, **k: None; fa.Form = lambda *a, **k: None
    fa.HTTPException = _HTTP; fa.UploadFile = object; fa.Request = object
    fr = types.ModuleType("fastapi.responses")
    class _R:
        def __init__(self, content=None, media_type=None, headers=None):
            self.content, self.media_type, self.headers = content, media_type, headers
    fr.Response = fr.StreamingResponse = _R
    sys.modules["fastapi"], sys.modules["fastapi.responses"] = fa, fr
import db, extras, monetize as M, seats, seats_api
from fastapi import HTTPException

PW = "Passw0rd!long1"


def mk(company, user, plan="professional"):
    u = db.create_customer_account(company, user, PW)
    cid = u["company_ids"][0]
    if plan:
        db.grant_subscription(cid, plan, 30, u["id"], "test")      # a plan with team seats
    return db.get_user(u["id"]), cid


def tag():
    return os.urandom(3).hex()


class Seats(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.create_user("site_admin_" + tag(), PW, role="admin")
        cls.admin = db.get_user(db.list_users()[0]["id"]) if hasattr(db, "list_users") else None
        with db._db() as con:
            cls.admin = db._user(con.execute("SELECT * FROM users WHERE role='admin' LIMIT 1").fetchone())

    def pay(self, cid, buyer_id, kind, key):
        o = M.create_order(cid, buyer_id, kind, key)
        M.submit_reference(o["order_ref"], cid, "T-" + o["order_ref"])
        return o, M.decide_order(o["id"], "approve", self.admin["id"])

    # ------------------------------------------------------------------ owner tools
    def test_new_owner_is_locked_and_old_owner_keeps_tools(self):
        o, cid = mk("Lock Co " + tag(), "lock_" + tag())
        self.assertEqual(db.effective_company_role(o), "owner")
        self.assertFalse(db.has_admin_tools(o))
        with db._db() as con:
            con.execute("UPDATE users SET tools_unlocked = NULL WHERE id = ?", (o["id"],))
        self.assertTrue(db.has_admin_tools(db.get_user(o["id"])))       # owners from before the unlock existed

    def test_owner_unlocks_admin_tools_by_paying(self):
        o, cid = mk("Own Co " + tag(), "own_" + tag())
        with self.assertRaises(ValueError):
            seats.seat_target(cid, o["id"], "analyst")                  # the owner already works as Analyst
        order, paid = self.pay(cid, o["id"], "seat", f"admin:{o['id']}")
        self.assertEqual(order["amount_ngn"], 150000)
        u = db.get_user(o["id"])
        self.assertTrue(db.has_admin_tools(u))
        self.assertEqual(db.effective_company_role(u), "owner")         # still the owner
        with self.assertRaises(ValueError):
            M.create_order(cid, o["id"], "seat", f"admin:{o['id']}")    # nothing left to buy

    # ------------------------------------------------------------------ joining
    def test_join_flow(self):
        o, cid = mk("Join Co " + tag(), "jo_" + tag())
        code = seats.get_join_code(cid)
        self.assertTrue(code.startswith("RZJ-"))
        self.assertEqual(seats.get_join_code(cid), code)                 # stable until regenerated
        with self.assertRaises(ValueError):
            seats.submit_join_request("RZJ-WRONGCODE", "nobody" + tag(), PW, None)
        name = "joiner_" + tag()
        seats.submit_join_request(code, name, PW, "j@example.com")
        with self.assertRaises(ValueError):
            seats.submit_join_request(code, name, PW, None)              # same username still waiting
        reqs = seats.list_join_requests(cid)
        self.assertEqual([r["username"] for r in reqs], [name])
        with db._db() as con:
            self.assertIsNone(con.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone())    # no account until approved
        r = seats.decide_join_request(cid, reqs[0]["id"], "approve", o)
        u = db.get_user(r["user_id"])
        self.assertEqual(db.effective_company_role(u), "viewer")
        self.assertEqual(u["role"], "viewer")
        self.assertTrue(db.verify_login(name, PW))
        with self.assertRaises(ValueError):
            seats.decide_join_request(cid, reqs[0]["id"], "approve", o)  # only once
        new = seats.regenerate_join_code(cid)
        self.assertNotEqual(new, code)
        with self.assertRaises(ValueError):
            seats.submit_join_request(code, "late_" + tag(), PW, None)   # the old code stops working

    def test_decline_and_seat_limit(self):
        o, cid = mk("Full Co " + tag(), "full_" + tag(), plan=None)      # trial: one seat only
        code = seats.get_join_code(cid)
        name = "wait_" + tag()
        seats.submit_join_request(code, name, PW, None)
        rid = seats.list_join_requests(cid)[0]["id"]
        with self.assertRaises(ValueError) as cm:
            seats.decide_join_request(cid, rid, "approve", o)
        self.assertIn("team member", str(cm.exception))
        self.assertEqual(seats.decide_join_request(cid, rid, "decline", o)["status"], "declined")
        self.assertIsNone(db.verify_login(name, PW))

    def test_other_company_cannot_decide(self):
        o, cid = mk("A Co " + tag(), "a_" + tag())
        o2, cid2 = mk("B Co " + tag(), "b_" + tag())
        seats.submit_join_request(seats.get_join_code(cid), "x_" + tag(), PW, None)
        rid = seats.list_join_requests(cid)[0]["id"]
        with self.assertRaises(LookupError):
            seats.decide_join_request(cid2, rid, "approve", o2)

    # ------------------------------------------------------------------ paid ladder
    def test_viewer_to_analyst_to_admin(self):
        o, cid = mk("Ladder Co " + tag(), "lad_" + tag())
        v = db.add_team_member(cid, "v_" + tag(), PW, "viewer")
        with self.assertRaises(ValueError):
            seats.seat_target(cid, v["id"], "admin")                    # Analyst comes first
        a, paid = self.pay(cid, o["id"], "seat", f"analyst:{v['id']}")
        self.assertEqual(a["amount_ngn"], 40000)
        self.assertEqual(db.effective_company_role(db.get_user(v["id"])), "analyst")
        self.assertEqual(db.get_user(v["id"])["role"], "analyst")
        self.assertFalse(db.has_admin_tools(db.get_user(v["id"])))
        order2, _ = self.pay(cid, o["id"], "seat", f"admin:{v['id']}")
        u = db.get_user(v["id"])
        self.assertEqual(db.effective_company_role(u), "admin")
        self.assertTrue(db.has_admin_tools(u))
        self.assertEqual(u["role"], "analyst")                          # never a site admin
        self.assertEqual(order2["title"], f"Unlock Admin access for {u['username']}")

    def test_cannot_unlock_someone_in_another_company(self):
        o, cid = mk("X Co " + tag(), "x_" + tag())
        o2, cid2 = mk("Y Co " + tag(), "y_" + tag())
        v = db.add_team_member(cid2, "vy_" + tag(), PW, "viewer")
        with self.assertRaises(LookupError):
            M.create_order(cid, o["id"], "seat", f"analyst:{v['id']}")

    def test_reject_leaves_role_unchanged(self):
        o, cid = mk("Rej Co " + tag(), "rej_" + tag())
        v = db.add_team_member(cid, "vr_" + tag(), PW, "viewer")
        order = M.create_order(cid, o["id"], "seat", f"analyst:{v['id']}")
        M.submit_reference(order["order_ref"], cid, "R-" + order["order_ref"])
        M.decide_order(order["id"], "reject", self.admin["id"])
        self.assertEqual(db.effective_company_role(db.get_user(v["id"])), "viewer")

    def test_site_admin_grant(self):
        o, cid = mk("Gr Co " + tag(), "gr_" + tag())
        v = db.add_team_member(cid, "vg_" + tag(), PW, "viewer")
        with db._db() as con:
            seats.apply_seat_unlock(con, cid, v["id"], "analyst")
        self.assertEqual(db.effective_company_role(db.get_user(v["id"])), "analyst")

    # ------------------------------------------------------------------ welcome coupon
    def test_welcome_coupon_single_use_one_company(self):
        o, cid = mk("Wel Co " + tag(), "wel_" + tag())
        o2, cid2 = mk("Wel2 Co " + tag(), "wel2_" + tag())
        c = seats.issue_welcome_coupon(cid, o["username"], "Wel Co", None)
        self.assertEqual(c["value"], 30)
        self.assertIsNone(seats.issue_welcome_coupon(cid, o["username"], "Wel Co", None))   # a company only ever gets one
        self.assertEqual(extras.percent_discount(c["code"], cid, 100000), (c["code"], 30000))
        with self.assertRaises(ValueError):
            extras.percent_discount(c["code"], cid2, 100000)             # not usable by another company
        extras.use_coupon(c["code"], cid, o["id"])
        with self.assertRaises(ValueError):
            extras.percent_discount(c["code"], cid, 100000)              # once, then never again

    def test_registration_approval_creates_owner_and_coupon(self):
        import portal
        name = "reg_" + tag()
        portal.submit_registration("Reg Co " + tag(), name, PW, None, "reg@example.com")
        rid = portal.list_registration_requests("pending")[0]["id"]
        r = portal.decide_registration(rid, "approve", self.admin)
        u = db.get_user(r["user_id"])
        self.assertEqual(db.effective_company_role(u), "owner")          # not a bare analyst
        self.assertFalse(db.has_admin_tools(u))
        with db._db() as con:
            n = con.execute("SELECT COUNT(*) AS n FROM coupons WHERE company_id=?", (r["company_id"],)).fetchone()["n"]
        self.assertEqual(n, 1)

    # ------------------------------------------------------------------ coupon on an order
    def test_coupon_on_order_and_release(self):
        o, cid = mk("Cpn Co " + tag(), "cpn_" + tag(), plan=None)
        c = seats.issue_welcome_coupon(cid, o["username"], "Cpn Co", None)
        order = M.create_order(cid, o["id"], "pack", "10")
        self.assertEqual(order["amount_ngn"], 7000)
        priced = M.apply_order_coupon(order["order_ref"], cid, o["id"], c["code"])
        self.assertEqual((priced["amount_ngn"], priced["discount_ngn"], priced["coupon_code"]), (4900, 2100, c["code"]))
        with self.assertRaises(ValueError):
            M.apply_order_coupon(order["order_ref"], cid, o["id"], c["code"])    # one coupon per order
        again = M.create_order(cid, o["id"], "pack", "10")
        self.assertEqual(again["amount_ngn"], 4900)                              # re-opening keeps the coupon price
        other = M.create_order(cid, o["id"], "feature", "pro_pdf")
        with self.assertRaises(ValueError):
            M.apply_order_coupon(other["order_ref"], cid, o["id"], c["code"])    # already spent
        M.cancel_order(order["order_ref"], cid)                                  # cancelling gives it back
        back = M.apply_order_coupon(other["order_ref"], cid, o["id"], c["code"])
        self.assertEqual(back["amount_ngn"], 3500)
        M.submit_reference(back["order_ref"], cid, "C-" + back["order_ref"])
        done = M.decide_order(back["id"], "approve", self.admin["id"])
        self.assertEqual(done["status"], "paid")

    def test_coupon_not_taken_when_membership_discount_is_better(self):
        o, cid = mk("Best Co " + tag(), "best_" + tag(), plan=None)
        c = extras.create_coupon("percent", 5, max_uses=1, days_valid=10, company_id=cid)
        db.grant_subscription(cid, "professional", 30, o["id"], "t")             # 20% marketplace discount
        pid = M.add_product("Disc " + tag(), "Audit", "A test product for the discount rule", "", 10000, "f.txt", "text/plain", b"x")["id"]
        order = M.create_order(cid, o["id"], "product", str(pid))
        self.assertEqual(order["amount_ngn"], 8000)
        with self.assertRaises(ValueError):
            M.apply_order_coupon(order["order_ref"], cid, o["id"], c["code"])
        extras.percent_discount(c["code"], cid, 1000)                            # still unused

    def test_pending_counts_per_section(self):
        o, cid = mk("Cnt Co " + tag(), "cnt_" + tag())
        before = M.overview()["pending_by_kind"]
        order = M.create_order(cid, o["id"], "pack", "25")
        M.submit_reference(order["order_ref"], cid, "N-" + order["order_ref"])
        after = M.overview()["pending_by_kind"]
        self.assertEqual(after["pack"], before["pack"] + 1)
        self.assertEqual(after["seat"], before["seat"])


class Routes(unittest.TestCase):
    """The HTTP layer, called without a web server."""
    @classmethod
    def setUpClass(cls):
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
        def need_role(u, r):
            if db.company_rank(u) < db.COMPANY_RANK[r]:
                raise HTTPException(403, "role")
        cls.W = types.SimpleNamespace(
            current_user=lambda: None, need=lambda u, r: None if u["role"] == "admin" else (_ for _ in ()).throw(HTTPException(403, "no")),
            need_role=need_role, need_spender=lambda u: None,
            need_company=lambda u, c: None if db.can_access(u, c) else (_ for _ in ()).throw(HTTPException(403, "co")),
            client_ip=lambda r: "1.2.3.4", _email_verify_required=lambda: False, extras=mock.MagicMock(), notify=mock.MagicMock())
        seats_api.register(App(), cls.W)

    def r(self, m, p):
        return self.routes[(m, p)]

    def test_join_and_unlock_routes(self):
        o, cid = mk("Rt Co " + tag(), "rt_" + tag())
        info = self.r("GET", "/api/companies/{company_id}/join")(cid, o)
        code = info["code"]
        with self.assertRaises(HTTPException) as cm:
            self.r("POST", "/api/register/join")(seats_api.JoinIn(join_code="RZJ-NOPENOPE", username="zz_" + tag(), password=PW), types.SimpleNamespace())
        self.assertEqual(cm.exception.status_code, 400)
        name = "rj_" + tag()
        res = self.r("POST", "/api/register/join")(seats_api.JoinIn(join_code=code, username=name, password=PW), types.SimpleNamespace())
        self.assertEqual(res["status"], "pending")
        rid = self.r("GET", "/api/companies/{company_id}/join")(cid, o)["requests"][0]["id"]
        d = self.r("POST", "/api/companies/{company_id}/join/{request_id}")(cid, rid, seats_api.JoinDecisionIn(decision="approve"), o)
        self.assertEqual(d["status"], "approved")
        member = d["user_id"]
        v = db.get_user(member)
        with self.assertRaises(HTTPException) as cm:                     # a viewer cannot see the join code or buy
            self.r("GET", "/api/companies/{company_id}/join")(cid, v)
        self.assertEqual(cm.exception.status_code, 403)
        out = self.r("POST", "/api/companies/{company_id}/team/{member_id}/unlock")(cid, member, seats_api.UnlockIn(role="analyst"), o)
        self.assertEqual(out["order"]["amount_ngn"], 40000)
        with self.assertRaises(HTTPException) as cm:
            self.r("POST", "/api/companies/{company_id}/team/{member_id}/unlock")(cid, member, seats_api.UnlockIn(role="admin"), o)
        self.assertEqual(cm.exception.status_code, 400)                  # Analyst first
        self.r("POST", "/api/team/ask-upgrade")(seats_api.AskIn(company_id=cid, role="analyst"), v)

    def test_catalogue_is_public_and_matches_prices(self):
        cat = self.r("GET", "/api/roles/catalogue")()
        self.assertEqual(cat["prices"], {"analyst": 40000, "admin": 150000})
        self.assertEqual([x["key"] for x in cat["roles"]], ["viewer", "analyst", "admin", "owner"])


if __name__ == "__main__":
    unittest.main()
