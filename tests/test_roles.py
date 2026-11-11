"""Company roles: owner / admin / analyst / viewer. Run: python -m unittest discover -s tests -v"""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("DASHBOARD_PASSWORD", "x" * 14)
from unittest import mock
for _m in ("httpx", "dotenv"):  # only for machines without these installed; real deploys have them
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = mock.MagicMock()
import db

try:
    from fastapi.testclient import TestClient
    import web
    HAVE_WEB = True
except Exception:  # noqa: BLE001
    HAVE_WEB = False


class RoleDbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = db.create_customer_account("Acme Ltd", "acme_owner", "Passw0rd!long1")
        cls.cid = cls.owner["company_ids"][0]
        db.grant_subscription(cls.cid, "professional", 30, cls.owner["id"], "test")   # a plan with team seats

    def test_creator_is_owner(self):
        u = db.get_user(self.owner["id"])
        self.assertEqual(db.effective_company_role(u), "owner")

    def test_team_roles_and_protection(self):
        try:
            a = db.add_team_member(self.cid, "acme_an", "Passw0rd!long2", "analyst")
        except ValueError as e:
            self.skipTest("plan has no room: " + str(e))
        v = db.add_team_member(self.cid, "acme_vw", "Passw0rd!long3", "viewer")
        self.assertEqual(db.effective_company_role(a), "analyst")
        self.assertEqual(db.effective_company_role(v), "viewer")
        self.assertEqual(v["role"], "viewer")          # site role stays viewer: can never write
        db.set_team_role(self.cid, a["id"], "admin")
        self.assertEqual(db.effective_company_role(db.get_user(a["id"])), "admin")
        self.assertEqual(db.get_user(a["id"])["role"], "analyst")   # an admin of a company is NOT a site admin
        with self.assertRaises(ValueError):
            db.set_team_role(self.cid, self.owner["id"], "viewer")  # owner is fixed
        with self.assertRaises(ValueError):
            db.set_team_role(self.cid, a["id"], "owner")            # no second owner
        with self.assertRaises(ValueError):
            db.remove_team_member(self.cid, self.owner["id"])
        db.remove_team_member(self.cid, v["id"])
        self.assertFalse(db.get_user(v["id"])["active"])
        names = [m["username"] for m in db.list_team(self.cid)]
        self.assertNotIn("acme_vw", names)
        self.assertEqual(db.list_team(self.cid)[0]["company_role"], "owner")

    def test_other_company_untouched(self):
        other = db.create_customer_account("Other Co", "other_owner", "Passw0rd!long4")
        oid = other["company_ids"][0]
        with self.assertRaises(LookupError):
            db.set_team_role(oid, self.owner["id"], "viewer")


@unittest.skipUnless(HAVE_WEB, "web stack not installed here")
class RoleApiTests(unittest.TestCase):
    def test_viewer_cannot_write_but_owner_can_manage(self):
        c = TestClient(web.app)
        u = db.create_customer_account("Api Co", "api_owner", "Passw0rd!long5")
        cid = u["company_ids"][0]
        r = c.post("/api/login", json={"username": "api_owner", "password": "Passw0rd!long5"})
        self.assertEqual(r.status_code, 200, r.text)
        me = c.get("/api/me").json()
        self.assertEqual(me["company_role"], "owner")
        self.assertEqual(c.get(f"/api/companies/{cid}/team").status_code, 200)
        self.assertEqual(c.get("/api/admin/overview").status_code, 403)   # owner is not the website owner
        r = c.post(f"/api/companies/{cid}/team", json={"username": "api_view", "password": "Passw0rd!long6", "role": "viewer"})
        if r.status_code == 400 and "allows up to" in r.text:
            self.skipTest("trial plan has no team seats")
        self.assertEqual(r.status_code, 200, r.text)
        v = TestClient(web.app)
        self.assertEqual(v.post("/api/login", json={"username": "api_view", "password": "Passw0rd!long6"}).status_code, 200)
        self.assertEqual(v.get("/api/me").json()["company_role"], "viewer")
        self.assertEqual(v.get(f"/api/companies/{cid}/team").status_code, 403)
        self.assertEqual(v.post(f"/api/companies/{cid}/team", json={"username": "zzz_x", "password": "Passw0rd!long7", "role": "admin"}).status_code, 403)
        self.assertEqual(v.post("/api/analyze", json={"company_id": cid, "question": "hi"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
