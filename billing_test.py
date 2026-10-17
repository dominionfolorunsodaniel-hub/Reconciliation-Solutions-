"""Offline tests for subscription, manual-payment and monthly quota logic."""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_TEST_DATA = tempfile.TemporaryDirectory(prefix="report-analyzer-billing-test-")
os.environ["DATA_DIR"] = _TEST_DATA.name

import core  # noqa: E402
import db  # noqa: E402


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.company = db.get_or_create_company("Company " + os.urandom(4).hex())
        self.user = db.create_user("user_" + os.urandom(4).hex(), "SafeTestPassword123!",
                                   "analyst", company_ids=[self.company["id"]])
        self.admin = db.create_user("admin_" + os.urandom(4).hex(), "SafeAdminPassword123!", "admin")

    def test_free_quota_and_failed_reservation_release(self):
        cid, uid = self.company["id"], self.user["id"]
        self.assertEqual(db.get_plan_status(cid)["monthly_limit"], 5)
        failed = db.reserve_monthly_usage(cid, uid, "analysis_question")
        db.finish_monthly_usage(failed, False)
        for _ in range(5):
            rid = db.reserve_monthly_usage(cid, uid, "forecast")
            db.finish_monthly_usage(rid, True)
        with self.assertRaises(core.LimitReached):
            db.reserve_monthly_usage(cid, uid, "analysis_question")
        self.assertEqual(db.get_plan_status(cid)["used"], 5)

    def test_concurrent_requests_cannot_exceed_free_quota(self):
        cid, uid = self.company["id"], self.user["id"]
        def attempt(_):
            try:
                rid = db.reserve_monthly_usage(cid, uid, "analysis_question")
                db.finish_monthly_usage(rid, True)
                return True
            except core.LimitReached:
                return False
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(attempt, range(10)))
        self.assertEqual(sum(results), 5)
        self.assertEqual(db.get_plan_status(cid)["used"], 5)

    def test_admin_grant_and_expiry_window(self):
        result = db.grant_subscription(self.company["id"], "starter", 7, self.admin["id"], "test reward")
        self.assertEqual(result["plan"], "starter")
        self.assertEqual(result["monthly_limit"], 50)
        self.assertEqual(result["subscription_source"], "admin")

    def test_payment_is_pending_until_admin_approval(self):
        cid, uid = self.company["id"], self.user["id"]
        req = db.create_payment_request(cid, uid, "enterprise", "REF-" + os.urandom(5).hex())
        self.assertEqual(req["status"], "pending")
        self.assertEqual(req["amount_ngn"], 5000000)
        self.assertEqual(db.get_plan_status(cid)["plan"], "free")
        with self.assertRaises(ValueError):
            db.create_payment_request(cid, uid, "starter", "ANOTHER-" + os.urandom(5).hex())
        db.decide_payment_request(req["id"], "approve", self.admin["id"], "verified in bank")
        self.assertEqual(db.get_plan_status(cid)["plan"], "enterprise")
        self.assertEqual(db.get_plan_status(cid)["team_limit"], 75)
        self.assertEqual(db.get_plan_status(cid)["monthly_limit"], 10000)

    def test_rejected_payment_does_not_activate_plan(self):
        req = db.create_payment_request(self.company["id"], self.user["id"], "starter", "REF-" + os.urandom(5).hex())
        db.decide_payment_request(req["id"], "reject", self.admin["id"], "not found")
        self.assertEqual(db.get_plan_status(self.company["id"])["plan"], "free")

    def test_new_plan_prices_and_terms(self):
        self.assertEqual(db.PLANS["free"]["monthly_limit"], 5)
        self.assertEqual(db.PLANS["starter"]["price_ngn"], 25000)
        self.assertEqual(db.PLANS["enterprise_plus"]["price_ngn"], 50000000)
        self.assertEqual(db.PLANS["enterprise_plus"]["duration_days"], 30)
        self.assertFalse(db.PLANS["enterprise_plus"].get("available", True))
        self.assertNotIn("enterprise_plus", db.PAID_PLANS)
        self.assertEqual(db.PLANS["enterprise"]["price_ngn"], 5000000)
        self.assertEqual(db.PLANS["enterprise"]["duration_days"], 21)
        with self.assertRaises(ValueError):
            db.create_payment_request(self.company["id"], self.user["id"], "enterprise_plus", "HIDDEN-" + os.urandom(5).hex())
        self.assertEqual(db.PLANS["business"]["duration_days"], 10.5)
        self.assertEqual(db.PLANS["free"]["duration_days"], 2)
        self.assertNotIn("lifetime", db.PAID_PLANS)
        cid, uid = self.company["id"], self.user["id"]
        with self.assertRaises(ValueError):
            db.create_payment_request(cid, uid, "enterprise_plus", "TERM-" + os.urandom(5).hex())
        # The old plan definition remains so existing Enterprise Plus subscriptions can still be read.
        self.assertEqual(db.PLANS["enterprise_plus"]["name"], "Enterprise Plus")
        self.assertFalse(db.PLANS["enterprise_plus"].get("available", True))

    def test_all_paid_plan_durations_are_configured(self):
        expected = {
            "starter": (25000, 3), "growth": (100000, 5), "professional": (350000, 7),
            "business": (1000000, 10.5), "corporate": (2500000, 14),
            "enterprise": (5000000, 21),
        }
        for key, (price, days) in expected.items():
            self.assertEqual(db.PLANS[key]["price_ngn"], price)
            self.assertEqual(db.PLANS[key]["duration_days"], days)
            self.assertTrue(db.duration_label(key))

    def test_enterprise_plus_is_hidden_from_new_sales(self):
        self.assertEqual(db.PLANS["enterprise"]["price_ngn"], 5000000)
        self.assertIn("enterprise", db.PAID_PLANS)
        self.assertNotIn("enterprise_plus", db.PAID_PLANS)
        self.assertNotIn("enterprise_plus", db.ADMIN_GRANT_PLANS)
        with self.assertRaises(ValueError):
            db.create_payment_request(self.company["id"], self.user["id"], "enterprise_plus", "HIDDEN-" + os.urandom(5).hex())

    def test_trial_expiry_blocks_usage(self):
        cid, uid = self.company["id"], self.user["id"]
        with db._db() as con:
            con.execute("UPDATE subscriptions SET expires_at='2000-01-01 00:00:00' WHERE company_id=? AND plan='free'", (cid,))
        status = db.get_plan_status(cid)
        self.assertEqual(status["plan"], "expired")
        self.assertEqual(status["monthly_limit"], 0)
        with self.assertRaises(core.LimitReached):
            db.reserve_monthly_usage(cid, uid, "analysis_question")

    def test_half_week_business_term_is_applied(self):
        cid, uid = self.company["id"], self.user["id"]
        req = db.create_payment_request(cid, uid, "business", "HALF-" + os.urandom(5).hex())
        db.decide_payment_request(req["id"], "approve", self.admin["id"], "verified business payment")
        status = db.get_plan_status(cid)
        self.assertEqual(status["plan"], "business")
        expiry = __import__("datetime").datetime.strptime(status["expires_at"], "%Y-%m-%d %H:%M:%S")
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None)
        remaining = (expiry - now).total_seconds() / 86400
        self.assertGreater(remaining, 10.4)
        self.assertLessEqual(remaining, 10.5)

    def test_legacy_lifetime_not_offered_for_new_payment(self):
        with self.assertRaises(ValueError):
            db.create_payment_request(self.company["id"], self.user["id"], "lifetime", "LIFE-" + os.urandom(5).hex())

    def test_business_team_limit(self):
        cid = self.company["id"]
        db.grant_subscription(cid, "business", 30, self.admin["id"], "team cap test")
        for i in range(14):
            db.create_user("member_" + os.urandom(4).hex(), "SafeMemberPassword123!", "analyst", company_ids=[cid])
        with self.assertRaises(ValueError):
            db.create_user("extra_" + os.urandom(4).hex(), "SafeMemberPassword123!", "analyst", company_ids=[cid])


if __name__ == "__main__":
    unittest.main(verbosity=2)
