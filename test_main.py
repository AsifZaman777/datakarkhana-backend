import unittest
import os
os.environ["TESTING"] = "1"
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
        user_row = conn.execute("SELECT id FROM users WHERE email = ?", (self.test_email,)).fetchone()
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
        # Ensure user starts with allow_sync = 0 and max_sync_files = 5, clean old test datasets
        conn.execute("UPDATE users SET allow_sync = 0, max_sync_files = 5 WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM datasets WHERE uploaded_by = ?", (user_id,))
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

    def test_view_private_scrape_job_graceful(self):
        """Verify that viewing a private scrape job (job_{id}) does not crash with 404 if file is missing"""
        # Create a mock scrape job in the database
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO scrape_jobs (user_id, query, division, district, area, status, result_count, result_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (1, "Dentists in Uttara", "Dhaka", "Dhaka", "Uttara", "done", 42, "non_existent_result_file.xlsx")
        )
        job_id = cursor.lastrowid
        conn.commit()
        conn.close()

        # Call GET /api/datasets/job_{job_id}
        res = client.get(f"/api/datasets/job_{job_id}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("dataset", data)
        self.assertEqual(data["dataset"]["id"], f"job_{job_id}")
        self.assertEqual(data["dataset"]["name"], "Dentists in Uttara")
        self.assertTrue(data["unlocked"])
        self.assertIsInstance(data["leads"], list)

    def test_21_supabase_storage_unconfigured_alert(self):
        """Verify that when Supabase Storage is not configured and not testing, endpoints return HTTP 503 alert"""
        from unittest.mock import patch
        conn = get_db()
        user_row = conn.execute("SELECT id FROM users WHERE email = ?", (self.test_email,)).fetchone()
        user_id = user_row["id"]
        conn.execute("UPDATE users SET allow_sync = 1, max_sync_files = 10 WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()

        user_headers = {"Authorization": f"Bearer {self.user_token}"}
        # Temporarily disable TESTING flag and mock unconfigured storage to test production alert behavior
        old_val = os.environ.get("TESTING")
        try:
            if "TESTING" in os.environ:
                del os.environ["TESTING"]
            
            with patch("main.is_supabase_storage_configured", return_value=False):
                # 1. Sync endpoint should return 503
                res_sync = client.post(
                    "/api/datasets/sync",
                    data={"name": "Test Leads", "category": "Leads"},
                    headers=user_headers
                )
                self.assertEqual(res_sync.status_code, 503)
                self.assertIn("Cloud dataset storage (Supabase Storage) is currently not configured", res_sync.json()["detail"])

                # 2. Promotion request endpoint should return 503
                res_promote = client.post(
                    "/api/datasets/promote-request",
                    data={"proposed_name": "Public Leads", "proposed_category": "General"},
                    headers=user_headers
                )
                self.assertEqual(res_promote.status_code, 503)
                self.assertIn("Publishing to the public catalogue requires cloud storage", res_promote.json()["detail"])
        finally:
            if old_val is not None:
                os.environ["TESTING"] = old_val

    def test_22_supabase_storage_client_mock_upload_and_download(self):
        """Test Supabase Storage client module functions with mocked requests"""
        from unittest.mock import patch, MagicMock
        import supabase_storage

        # Test normalize path
        self.assertEqual(
            supabase_storage.normalize_storage_path("supabase://datasets/synced/1/test.xlsx"),
            "synced/1/test.xlsx"
        )

        # Mock configured environment
        with patch.object(supabase_storage, "is_supabase_storage_configured", return_value=True):
            with patch.object(supabase_storage, "ensure_bucket_exists", return_value=True):
                with patch.object(supabase_storage, "get_effective_supabase_url", return_value="https://mockproj.supabase.co"):
                    with patch("supabase_storage.requests.post") as mock_post:
                        # Mock upload response
                        mock_resp = MagicMock()
                        mock_resp.status_code = 200
                        mock_post.return_value = mock_resp

                        ok, uri = supabase_storage.upload_dataset_file(b"dummy_excel_bytes", "synced/1/test.xlsx")
                        self.assertTrue(ok)
                        self.assertTrue(uri.startswith("supabase://"))

                    with patch("supabase_storage.requests.get") as mock_get:
                        # Mock download response
                        mock_resp = MagicMock()
                        mock_resp.status_code = 200
                        mock_resp.content = b"downloaded_bytes"
                        mock_get.return_value = mock_resp

                        ok, content, err = supabase_storage.download_dataset_file("supabase://datasets/synced/1/test.xlsx")
                        self.assertTrue(ok)
                        self.assertEqual(content, b"downloaded_bytes")

    def test_23_supabase_dataset_preview_in_table(self):
        """Test that get_dataset retrieves and parses leads when stored in Supabase path"""
        from unittest.mock import patch
        import pandas as pd
        import io

        # Create a sample DataFrame in bytes
        df_sample = pd.DataFrame([
            {"Name": "Cloud Lead 1", "Phone": "+8801711111111", "Category": "Tech"},
            {"Name": "Cloud Lead 2", "Phone": "+8801722222222", "Category": "Retail"}
        ])
        buf = io.BytesIO()
        df_sample.to_excel(buf, index=False)
        excel_bytes = buf.getvalue()

        # Insert dataset record with supabase:// path
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO datasets (name, category, file_path, row_count, is_active, price_credits)
               VALUES ('Supabase Cloud Test', 'Tech', 'supabase://datasets/synced/1/test_cloud.xlsx', 2, 1, 10)"""
        )
        ds_id = cursor.lastrowid
        conn.commit()
        conn.close()

        # Mock download_dataset_file to return the excel bytes
        with patch("main.download_dataset_file", return_value=(True, excel_bytes, "")):
            with patch("main.is_supabase_storage_configured", return_value=True):
                admin_headers = {"Authorization": f"Bearer {self.admin_token}"}
                res = client.get(f"/api/datasets/{ds_id}", headers=admin_headers)
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(data["dataset"]["name"], "Supabase Cloud Test")
                self.assertEqual(data["total_rows"], 2)
                self.assertEqual(len(data["leads"]), 2)
                self.assertEqual(data["leads"][0]["Name"], "Cloud Lead 1")


    def test_24_admin_storage_overview(self):
        """Test GET /api/admin/storage/overview returns bucket and stats overview"""
        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}
        res = client.get("/api/admin/storage/overview", headers=admin_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("bucket_name", data)
        self.assertIn("supabase_configured", data)
        self.assertIn("total_cloud_datasets", data)
        self.assertIn("total_cloud_rows", data)
        self.assertIn("users", data)

    def test_25_admin_set_user_upload_limit_and_get_datasets(self):
        """Test admin setting max_sync_files limit and inspecting user datasets"""
        conn = get_db()
        user_row = conn.execute("SELECT id FROM users WHERE email = ?", (self.test_email,)).fetchone()
        user_id = user_row["id"]
        conn.close()

        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}
        # Set limit to 2
        res = client.post(f"/api/admin/users/{user_id}/upload-limit", json={"max_sync_files": 2}, headers=admin_headers)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["max_sync_files"], 2)

        # Inspect user datasets
        res_ds = client.get(f"/api/admin/users/{user_id}/datasets", headers=admin_headers)
        self.assertEqual(res_ds.status_code, 200)
        self.assertIn("datasets", res_ds.json())
        self.assertIn("user", res_ds.json())

    def test_26_sync_limit_enforcement(self):
        """Test that syncing enforces max_sync_files limit when quota is exceeded"""
        from unittest.mock import patch
        conn = get_db()
        user_row = conn.execute("SELECT id FROM users WHERE email = ?", (self.test_email,)).fetchone()
        user_id = user_row["id"]
        # Set limit to 1
        conn.execute("UPDATE users SET max_sync_files = 1, allow_sync = 1 WHERE id = ?", (user_id,))
        # Insert 1 existing dataset for user
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO datasets (name, category, file_path, row_count, uploaded_by, is_active)
               VALUES ('Existing Cloud DS', 'Leads', 'supabase://datasets/synced/ex.xlsx', 10, ?, 1)""",
            (user_id,)
        )
        existing_ds_id = cursor.lastrowid
        conn.commit()
        conn.close()

        # Attempt to sync another dataset -> should fail with 403
        user_headers = {"Authorization": f"Bearer {self.user_token}"}
        dummy_file = ("test.xlsx", b"dummy content", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        data = {
            "name": "Over Limit Dataset",
            "category": "Test",
            "row_count": "5"
        }
        res = client.post("/api/datasets/sync", data=data, files={"file": dummy_file}, headers=user_headers)
        self.assertEqual(res.status_code, 403)
        self.assertIn("Cloud upload limit reached", res.json()["detail"])

        # Clean up existing test dataset
        conn = get_db()
        conn.execute("DELETE FROM datasets WHERE id = ?", (existing_ds_id,))
        conn.execute("UPDATE users SET max_sync_files = 5 WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()

    def test_27_desync_dataset(self):
        """Test desyncing a dataset removes cloud dataset record and resets scrape_jobs.is_synced to 0"""
        from unittest.mock import patch
        conn = get_db()
        user_row = conn.execute("SELECT id FROM users WHERE email = ?", (self.test_email,)).fetchone()
        user_id = user_row["id"]

        # Create a mock scrape job
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO scrape_jobs (user_id, query, status, is_synced)
               VALUES (?, 'Desync Test Job', 'done', 1)""",
            (user_id,)
        )
        job_id = cursor.lastrowid

        # Create a matching dataset in cloud
        cursor.execute(
            """INSERT INTO datasets (name, category, file_path, row_count, uploaded_by, is_active, source_job_id)
               VALUES ('Desync Test Cloud DS', 'Leads', 'supabase://datasets/synced/desync_test.xlsx', 15, ?, 1, ?)""",
            (user_id, job_id)
        )
        ds_id = cursor.lastrowid
        conn.commit()
        conn.close()

        with patch("supabase_storage.delete_dataset_file", return_value=(True, "")):
            user_headers = {"Authorization": f"Bearer {self.user_token}"}
            res = client.post(f"/api/datasets/{ds_id}/desync", headers=user_headers)
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["success"])

            # Verify dataset is deleted from DB
            conn = get_db()
            deleted_ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (ds_id,)).fetchone()
            self.assertIsNone(deleted_ds)

            # Verify scrape job is_synced is reset to 0
            job = conn.execute("SELECT is_synced FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
            self.assertEqual(job["is_synced"], 0)
            
            # Clean up job
            conn.execute("DELETE FROM scrape_jobs WHERE id = ?", (job_id,))
            conn.commit()
            conn.close()

    def test_29_inspect_excel_file_raw_vs_formatted(self):
        """Test inspecting an Excel/CSV file with raw vs user-controlled formatted preview"""
        import pandas as pd
        import io

        df = pd.DataFrame([
            {"Name": "  Tech Corp  ", "Phone": "01711223344.0", "City": " Dhaka "},
            {"Name": "", "Phone": "", "City": ""},  # empty row
            {"Name": "Retail Shop", "Phone": "01822334455.0", "City": "Chittagong"}
        ])
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
        excel_bytes = buffer.getvalue()

        user_headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.post(
            "/api/datasets/inspect-file",
            files={"file": ("test_inspect.xlsx", excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"strip_zero": "true", "trim_spaces": "true", "drop_empty_rows": "true"},
            headers=user_headers
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["total_rows"], 2)  # dropped empty row
        # Raw preview should preserve .0 and whitespace
        self.assertIn("01711223344.0", str(data["raw_preview"]))
        # Cleaned preview should have stripped .0 and trimmed whitespace
        self.assertIn("01711223344", str(data["cleaned_preview"]))
        self.assertNotIn("01711223344.0", str(data["cleaned_preview"]))

    def test_30_upload_private_dataset_with_formatting(self):
        """Test uploading arbitrary Excel file to private catalogue with user-controlled formatting"""
        import pandas as pd
        import io

        df = pd.DataFrame([
            {"Company": "Alpha Traders", "Contact": "8801912345678.0", "Category": "Wholesale"},
            {"Company": "Beta Solutions", "Contact": "8801798765432.0", "Category": "IT"}
        ])
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
        excel_bytes = buffer.getvalue()

        user_headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.post(
            "/api/datasets/upload-private",
            files={"file": ("sample_custom_b2b.xlsx", excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={
                "name": "My Custom B2B Leads",
                "category": "Corporate Directory",
                "strip_zero": "true",
                "trim_spaces": "true",
                "drop_empty_rows": "true"
            },
            headers=user_headers
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        ds_id = data["dataset_id"]
        self.assertEqual(data["is_synced"], 0)

        # Verify dataset exists in my-private
        res_priv = client.get("/api/datasets/my-private", headers=user_headers)
        self.assertEqual(res_priv.status_code, 200)
        my_ids = [d["id"] for d in res_priv.json()]
        self.assertIn(ds_id, my_ids)

        # Verify dynamic columns and formatted leads in get_dataset
        res_detail = client.get(f"/api/datasets/{ds_id}", headers=user_headers)
        self.assertEqual(res_detail.status_code, 200)
        detail_data = res_detail.json()
        self.assertIn("columns", detail_data)
        self.assertIn("Company", detail_data["columns"])
        self.assertIn("Contact", detail_data["columns"])
        # Verify .0 stripped from Contact
        self.assertEqual(detail_data["leads"][0]["Contact"], "8801912345678")

    def test_31_upload_private_sync_rule_enforcement(self):
        """Test cloud sync restriction on uploaded private dataset (blocked for starter, allowed for pro/admin)"""
        import pandas as pd
        import io

        conn = get_db()
        user_row = conn.execute("SELECT id FROM users WHERE email = ?", (self.test_email,)).fetchone()
        user_id = user_row["id"]
        # Ensure user has allow_sync = 0
        conn.execute("UPDATE users SET allow_sync = 0 WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()

        df = pd.DataFrame([{"Lead": "Test Sync Lead", "Phone": "01700000000"}])
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
        excel_bytes = buffer.getvalue()

        user_headers = {"Authorization": f"Bearer {self.user_token}"}
        res_up = client.post(
            "/api/datasets/upload-private",
            files={"file": ("sync_test.xlsx", excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"name": "Sync Test Dataset", "category": "General"},
            headers=user_headers
        )
        self.assertEqual(res_up.status_code, 200)
        ds_id = res_up.json()["dataset_id"]

        # Attempt to sync to cloud without Pro/Enterprise/allow_sync -> 403 Forbidden
        res_sync_blocked = client.post(
            "/api/datasets/sync",
            data={"dataset_id": ds_id},
            headers=user_headers
        )
        self.assertEqual(res_sync_blocked.status_code, 403)
        self.assertIn("exclusive feature", res_sync_blocked.json()["detail"])

        # Enable allow_sync for user
        conn = get_db()
        conn.execute("UPDATE users SET allow_sync = 1 WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()

        # Now sync should succeed
        res_sync_ok = client.post(
            "/api/datasets/sync",
            data={"dataset_id": ds_id},
            headers=user_headers
        )
        self.assertEqual(res_sync_ok.status_code, 200)
        self.assertTrue(res_sync_ok.json()["success"])

        # Desync uploaded dataset (retains in local SQLite)
        res_desync = client.post(f"/api/datasets/{ds_id}/desync", headers=user_headers)
        self.assertEqual(res_desync.status_code, 200)

    def test_32_promote_and_publish_uploaded_private_dataset(self):
        """Test Admin publishing an uploaded private dataset directly to the public catalog"""
        import pandas as pd
        import io

        df = pd.DataFrame([{"Service": "Web Development", "Contact": "01711001100"}])
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
        excel_bytes = buffer.getvalue()

        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}
        res_up = client.post(
            "/api/datasets/upload-private",
            files={"file": ("admin_promote_test.xlsx", excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"name": "Admin Private Draft", "category": "Services"},
            headers=admin_headers
        )
        self.assertEqual(res_up.status_code, 200)
        ds_id = res_up.json()["dataset_id"]

        # Admin publishes directly to public catalog
        res_pub = client.post(
            f"/api/datasets/{ds_id}/publish",
            data={"price_credits": 15, "proposed_name": "Public B2B Services"},
            headers=admin_headers
        )
        self.assertEqual(res_pub.status_code, 200)
        self.assertTrue(res_pub.json()["success"])

        # Verify it is active in public datasets catalog
        res_list = client.get("/api/datasets?search=Public B2B Services")
        self.assertEqual(res_list.status_code, 200)
        matching = [d for d in res_list.json() if d["id"] == ds_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["price_credits"], 15)
        self.assertEqual(matching[0]["is_active"], 1)

    def test_33_tier_access_control_and_user_overrides(self):
        """Test Superadmin Tier Permissions Matrix and User-Level Overrides"""
        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}
        user_headers = {"Authorization": f"Bearer {self.user_token}"}

        # 1. Superadmin fetches tier permissions matrix
        res_matrix = client.get("/api/admin/tier-permissions", headers=admin_headers)
        self.assertEqual(res_matrix.status_code, 200)
        tiers = res_matrix.json()
        tier_ids = [t["tier_id"] for t in tiers]
        self.assertIn("starter", tier_ids)
        self.assertIn("pro", tier_ids)
        self.assertIn("enterprise", tier_ids)

        # 2. Superadmin updates tier permissions (e.g. adjust pro quota to 8 and save)
        pro_tier = next(t for t in tiers if t["tier_id"] == "pro")
        pro_tier["max_sync_files"] = 8
        res_save = client.post(
            "/api/admin/tier-permissions",
            json={"tiers": tiers, "apply_to_existing_users": False},
            headers=admin_headers
        )
        self.assertEqual(res_save.status_code, 200)
        self.assertTrue(res_save.json()["success"])

        # 3. Superadmin sets user-level override on test user
        from database import get_db
        conn = get_db()
        user_row = conn.execute("SELECT id FROM users WHERE email = ?", (self.test_email,)).fetchone()
        user_id = user_row["id"]
        conn.close()

        res_override = client.post(
            f"/api/admin/users/{user_id}/permissions",
            json={"allow_sync": 1, "max_sync_files": 12, "allow_download": 1},
            headers=admin_headers
        )
        self.assertEqual(res_override.status_code, 200)
        eff = res_override.json()["effective_permissions"]
        self.assertTrue(eff["allow_sync"])
        self.assertEqual(eff["max_sync_files"], 12)
        self.assertTrue(eff["allow_dataset_download"])

    def test_34_auto_configuration_pro_enterprise_and_tier_resolution(self):
        """Test automatic permission configuration and tier resolution for Pro/Enterprise users"""
        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}

        # Query all users via admin endpoint
        res_users = client.get("/api/admin/users", headers=admin_headers)
        self.assertEqual(res_users.status_code, 200)
        users = res_users.json()

        # Verify plan_tier and purchased_package fields exist on all user objects
        for u in users:
            self.assertIn("plan_tier", u)
            self.assertIn("purchased_package", u)
            self.assertIn("effective_permissions", u)
            # If user has Pro or Enterprise tier, verify allow_sync is automatically 1
            if u["plan_tier"] in ("pro", "enterprise"):
                self.assertEqual(u["allow_sync"], 1)
                self.assertGreaterEqual(u["max_sync_files"], 5)


if __name__ == "__main__":
    unittest.main()


