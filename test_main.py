import unittest
import os
import time
import json
from fastapi.testclient import TestClient

# Import FastAPI app from main.py
from main import app, get_db

client = TestClient(app)

class TestFullApplicationBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Create test user and get admin token"""
        from config import SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD
        from database import init_db
        init_db()
        cls.test_email = "autotest_user@example.com"
        cls.test_password = "TestPassword123!"
        cls.admin_email = SUPERADMIN_EMAIL
        cls.admin_password = SUPERADMIN_PASSWORD

        # Register test user if not existing
        reg_res = client.post("/api/auth/register", json={
            "full_name": "Automation Tester",
            "email": cls.test_email,
            "password": cls.test_password
        })

        # Mark test user as verified and give 100 credits for tests
        conn = get_db()
        conn.execute("UPDATE users SET is_verified = 1, credits = 100 WHERE email = ?", (cls.test_email,))
        conn.commit()
        conn.close()

        # Login test user
        login_res = client.post("/api/auth/login", json={
            "email": cls.test_email,
            "password": cls.test_password
        })
        
        if login_res.status_code == 200:
            cls.user_token = login_res.json()["token"]
        else:
            cls.user_token = ""

        # Login admin user
        admin_login = client.post("/api/auth/login", json={
            "email": cls.admin_email,
            "password": cls.admin_password
        })
        if admin_login.status_code == 200:
            cls.admin_token = admin_login.json()["token"]
        else:
            cls.admin_token = cls.user_token

    # ── 1. AUTHENTICATION TESTS ──
    def test_01_user_profile_me(self):
        """Test fetching logged-in user profile"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.get("/api/auth/me", headers=headers)
        self.assertIn(res.status_code, [200, 401])
        if res.status_code == 200:
            data = res.json()
            self.assertIn("email", data)
            self.assertIn("credits", data)

    def test_02_resend_verification(self):
        """Test resending verification link"""
        res = client.post("/api/auth/resend-verification", json={"email": self.test_email})
        self.assertIn(res.status_code, [200, 400, 404])

    # ── 2. CONFIG & REGIONS TESTS ──
    def test_03_get_regions_config(self):
        """Test loading Bangladesh divisions, districts & categories config"""
        res = client.get("/api/config/regions")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("regions", data)
        self.assertIn("categories", data)
        self.assertIsInstance(data["categories"], list)

    def test_04_get_payment_gateway_config(self):
        """Test loading bKash / Pathao Pay gateway numbers and QR settings"""
        res = client.get("/api/config/payment-gateways")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("bkash_number", data)
        self.assertIn("packages", data)

    # ── 3. DATASETS API TESTS ──
    def test_05_list_public_datasets(self):
        """Test browsing public dataset catalog with category and search filters"""
        res = client.get("/api/datasets?search=coaching")
        self.assertEqual(res.status_code, 200)
        datasets = res.json()
        self.assertIsInstance(datasets, list)

    def test_06_get_dataset_detail_and_pagination(self):
        """Test getting dataset detail with page 1 and page 2 pagination"""
        list_res = client.get("/api/datasets")
        datasets = list_res.json()
        if len(datasets) > 0:
            ds_id = datasets[0]["id"]
            res_p1 = client.get(f"/api/datasets/{ds_id}?page=1")
            if res_p1.status_code == 200:
                data_p1 = res_p1.json()
                self.assertIn("dataset", data_p1)
                self.assertIn("leads", data_p1)
                self.assertIn("pages_count", data_p1)

                res_p2 = client.get(f"/api/datasets/{ds_id}?page=2")
                self.assertEqual(res_p2.status_code, 200)
                data_p2 = res_p2.json()
                self.assertEqual(data_p2["page"], 2)
            else:
                self.assertEqual(res_p1.status_code, 404)

    def test_07_unlock_dataset(self):
        """Test dataset unlocking endpoint"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.post("/api/datasets/1/unlock", headers=headers)
        self.assertIn(res.status_code, [200, 400, 402, 404])

    # ── 4. SCRAPER API TESTS ──
    def test_08_list_scraper_jobs(self):
        """Test listing scraper jobs for user"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.get("/api/scraper/jobs", headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.json(), list)

    def test_09_create_custom_dataset_request(self):
        """Test submitting custom dataset request"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.post("/api/requests", json={
            "category_query": "Real Estate Agents in Uttara",
            "division": "Dhaka",
            "district": "Dhaka",
            "area": "Uttara",
            "business_name": "Test Agency",
            "phone": "+8801700000000",
            "notes": "Urgent test lead order"
        }, headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("success"))

    # ── 5. MARKETING TESTS ──
    def test_10_list_campaigns(self):
        """Test listing marketing campaigns"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.get("/api/marketing/campaigns", headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.json(), list)

    def test_11_recipient_groups(self):
        """Test fetching recipient groups for campaigns"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.get("/api/marketing/recipient-groups", headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.json(), list)

    # ── 6. PAYMENT / BILLING TESTS ──
    def test_12_package_configurations(self):
        """Test loading public package configurations"""
        res = client.get("/api/config/packages")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("packages", data)
        self.assertTrue(len(data["packages"]) > 0)

    def test_13_submit_payment_verification(self):
        """Test user payment request submission with transaction ID"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        trx_id = f"TX{int(time.time())}"
        res = client.post("/api/payments/submit", json={
            "package_name": "Starter Lead Pack",
            "credits_requested": 50,
            "amount_bdt": 350.0,
            "payment_method": "bkash",
            "bkash_number": "01711000000",
            "transaction_id": trx_id
        }, headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("success"))

    # ── 7. ADMIN DASHBOARD TESTS ──
    def test_14_admin_dashboard_metrics(self):
        """Test admin dashboard overview analytics"""
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        res = client.get("/api/admin/overview", headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("user_stats", data)
        self.assertIn("total_users", data["user_stats"])

    def test_15_admin_list_users(self):
        """Test admin user list and credit adjustments"""
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        res = client.get("/api/admin/users", headers=headers)
        self.assertEqual(res.status_code, 200)
        users = res.json()
        self.assertTrue(len(users) > 0)
        self.assertIn("allow_sync", users[0])
        self.assertIn("plan_tier", users[0])

    def test_16_admin_security_violations(self):
        """Test security violation logger"""
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        res = client.get("/api/admin/violations", headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.json(), list)

    def test_17_dataset_export_permissions(self):
        """Test dataset export permissions for regular user vs admin"""
        user_headers = {"Authorization": f"Bearer {self.user_token}"}
        res_user = client.get("/api/datasets/99999/export", headers=user_headers)
        self.assertIn(res_user.status_code, [403, 404])

        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}
        res_admin = client.get("/api/datasets/99999/export", headers=admin_headers)
        self.assertEqual(res_admin.status_code, 404)

    def test_18_promotion_request_workflow(self):
        """Test user promotion request and admin review flow"""
        user_headers = {"Authorization": f"Bearer {self.user_token}"}
        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}

        from database import get_db
        conn = get_db()
        user_row = conn.execute("SELECT id FROM users WHERE role = 'user' ORDER BY id ASC LIMIT 1").fetchone()
        user_id = user_row["id"] if user_row else 1
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO scrape_jobs (user_id, query, division, district, area, status, result_path, result_count, promotion_status)
               VALUES (?, 'Dentists in Gulshan', 'Dhaka', 'Dhaka', 'Gulshan', 'done', 'test_dummy.xlsx', 25, 'none')""",
            (user_id,)
        )
        job_id = cursor.lastrowid
        conn.commit()
        conn.close()

        res_req = client.post(
            f"/api/scraper/jobs/{job_id}/request-promote",
            data={"name": "Gulshan Dental Clinics", "category": "Healthcare"},
            headers=user_headers
        )
        self.assertEqual(res_req.status_code, 200)

        # Admin lists promotion requests
        res_list = client.get("/api/admin/promotion-requests", headers=admin_headers)
        self.assertEqual(res_list.status_code, 200)
        pending_list = res_list.json()
        matching = [p for p in pending_list if p.get("job_id") == job_id]
        self.assertTrue(len(matching) > 0)
        self.assertEqual(matching[0]["status"], "pending")

        # Admin rejects request
        res_rej = client.post(f"/api/admin/promotion-requests/{job_id}/reject", headers=admin_headers)
        self.assertEqual(res_rej.status_code, 200)

        conn = get_db()
        conn.execute("DELETE FROM scrape_jobs WHERE id = ?", (job_id,))
        conn.commit()
        conn.close()

    def test_19_cloud_sync_permissions_and_admin_toggle(self):
        """Test cloud sync restriction, admin allow-sync toggle, and cloud dataset sync"""
        user_headers = {"Authorization": f"Bearer {self.user_token}"}
        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}

        from database import get_db
        conn = get_db()
        user_row = conn.execute("SELECT id FROM users WHERE email = ?", (self.test_email,)).fetchone()
        user_id = user_row["id"]
        # Ensure user starts with allow_sync = 0
        conn.execute("UPDATE users SET allow_sync = 0 WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()

        # User attempts cloud sync without permission -> 403 Forbidden
        res_sync_blocked = client.post(
            "/api/datasets/sync",
            data={"name": "My Blocked Leads", "category": "Leads"},
            headers=user_headers
        )
        self.assertEqual(res_sync_blocked.status_code, 403)
        self.assertIn("exclusive feature", res_sync_blocked.json()["detail"])

        # Admin toggles allow_sync for this customer
        res_allow = client.post(
            f"/api/admin/users/{user_id}/allow-sync",
            json={"allow_sync": 1},
            headers=admin_headers
        )
        self.assertEqual(res_allow.status_code, 200)
        self.assertEqual(res_allow.json()["allow_sync"], 1)

        # Verify /api/auth/me reflects allow_sync = 1
        res_me = client.get("/api/auth/me", headers=user_headers)
        self.assertEqual(res_me.status_code, 200)
        self.assertEqual(res_me.json()["allow_sync"], 1)

        # Now user can sync dataset to cloud PostgreSQL
        res_sync_success = client.post(
            "/api/datasets/sync",
            data={"name": "My Synced Leads", "category": "Private Leads", "row_count": 42},
            headers=user_headers
        )
        self.assertEqual(res_sync_success.status_code, 200)
        ds_id = res_sync_success.json()["dataset_id"]

        # Verify dataset exists in PostgreSQL with is_active = 0 (private)
        conn = get_db()
        ds_row = conn.execute("SELECT * FROM datasets WHERE id = ?", (ds_id,)).fetchone()
        self.assertIsNotNone(ds_row)
        self.assertEqual(ds_row["is_active"], 0)
        self.assertEqual(ds_row["is_synced"], 1)
        self.assertEqual(ds_row["name"], "My Synced Leads")
        conn.close()

    def test_20_promotion_approval_and_rejection_removes_from_postgres(self):
        """Test dataset promotion request, approval to public catalog, and rejection removing from PostgreSQL"""
        user_headers = {"Authorization": f"Bearer {self.user_token}"}
        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}

        # 1. User submits promotion request
        res_promote = client.post(
            "/api/datasets/promote-request",
            data={
                "proposed_name": "Premium Mirpur Restaurants",
                "proposed_category": "Restaurants",
                "division": "Dhaka",
                "district": "Dhaka",
                "area": "Mirpur",
                "row_count": 50
            },
            headers=user_headers
        )
        self.assertEqual(res_promote.status_code, 200)
        ds_id = res_promote.json()["dataset_id"]

        # 2. Admin verifies request in pending list
        res_reqs = client.get("/api/admin/promotion-requests", headers=admin_headers)
        self.assertEqual(res_reqs.status_code, 200)
        reqs = res_reqs.json()
        match = [r for r in reqs if r.get("id") == ds_id or r.get("dataset_id") == ds_id]
        self.assertTrue(len(match) > 0)
        self.assertEqual(match[0]["name"], "Premium Mirpur Restaurants")

        # 3. Admin rejects promotion request
        res_reject = client.post(f"/api/admin/promotion-requests/{ds_id}/reject", headers=admin_headers)
        self.assertEqual(res_reject.status_code, 200)
        self.assertIn("removed from postgresql", res_reject.json()["message"].lower())

        # 4. Verify dataset is completely REMOVED from PostgreSQL
        from database import get_db
        conn = get_db()
        ds_deleted = conn.execute("SELECT * FROM datasets WHERE id = ?", (ds_id,)).fetchone()
        self.assertIsNone(ds_deleted)
        conn.close()

        # 5. Test Approval flow: submit another dataset
        res_promote2 = client.post(
            "/api/datasets/promote-request",
            data={
                "proposed_name": "Gulshan IT Companies",
                "proposed_category": "Technology",
                "row_count": 100
            },
            headers=user_headers
        )
        ds_id2 = res_promote2.json()["dataset_id"]

        # Admin approves
        res_app = client.post(f"/api/admin/promotion-requests/{ds_id2}/approve", headers=admin_headers)
        self.assertEqual(res_app.status_code, 200)

        # Verify dataset is active in PostgreSQL public catalog
        conn = get_db()
        ds_active = conn.execute("SELECT * FROM datasets WHERE id = ?", (ds_id2,)).fetchone()
        self.assertIsNotNone(ds_active)
        self.assertEqual(ds_active["is_active"], 1)
        self.assertEqual(ds_active["promotion_status"], "approved")
        conn.close()


if __name__ == "__main__":
    unittest.main()
