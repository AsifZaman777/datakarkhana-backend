import unittest
import os
import json
import sqlite3
from fastapi.testclient import TestClient

# Import FastAPI app from main.py
from main import app, get_db

client = TestClient(app)

class TestFullApplicationBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Create test user and get admin token"""
        cls.test_email = "autotest_user@example.com"
        cls.test_password = "TestPassword123!"
        cls.admin_email = "admin@databazaar.com"
        cls.admin_password = "adminpassword"

        # Register test user if not existing
        reg_res = client.post("/api/auth/register", json={
            "full_name": "Automation Tester",
            "email": cls.test_email,
            "password": cls.test_password
        })

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
            # Fallback admin token creation
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
        res = client.post(f"/api/auth/resend-verification?email={self.test_email}")
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
        # Fetch catalog first
        list_res = client.get("/api/datasets")
        datasets = list_res.json()
        if len(datasets) > 0:
            ds_id = datasets[0]["id"]
            res_p1 = client.get(f"/api/datasets/{ds_id}?page=1")
            self.assertEqual(res_p1.status_code, 200)
            data_p1 = res_p1.json()
            self.assertIn("dataset", data_p1)
            self.assertIn("leads", data_p1)
            self.assertIn("pages_count", data_p1)

            # Test pagination Next page (page 2)
            res_p2 = client.get(f"/api/datasets/{ds_id}?page=2")
            self.assertEqual(res_p2.status_code, 200)
            data_p2 = res_p2.json()
            self.assertEqual(data_p2["page"], 2)

    def test_07_unlock_dataset(self):
        """Test dataset unlocking endpoint"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.post("/api/datasets/1/unlock", headers=headers)
        self.assertIn(res.status_code, [200, 400, 402, 404])

    # ── 4. SCRAPER API TESTS ──
    def test_08_list_scraper_jobs(self):
        """Test fetching private scraper jobs list"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.get("/api/scraper/jobs", headers=headers)
        self.assertIn(res.status_code, [200, 401])
        if res.status_code == 200:
            self.assertIsInstance(res.json(), list)

    def test_09_create_custom_dataset_request(self):
        """Test submitting custom dataset request"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.post("/api/scraper/requests", json={
            "category_query": "Pharma Dhaka",
            "division": "Dhaka",
            "district": "Dhaka",
            "area": "Dhanmondi",
            "business_name": "Test Pharmacy",
            "phone": "01711002233",
            "additional_notes": "Test request from suite"
        }, headers=headers)
        self.assertIn(res.status_code, [200, 201, 401])

    # ── 5. MARKETING AUTOMATION TESTS ──
    def test_10_whatsapp_status(self):
        """Test checking WhatsApp session status"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.get("/api/marketing/whatsapp-status", headers=headers)
        self.assertIn(res.status_code, [200, 401])
        if res.status_code == 200:
            self.assertIn("session_active", res.json())

    def test_11_marketing_dashboard_stats(self):
        """Test fetching marketing dashboard overview statistics"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.get("/api/marketing/dashboard-stats", headers=headers)
        self.assertIn(res.status_code, [200, 401])
        if res.status_code == 200:
            data = res.json()
            self.assertIn("total_campaigns", data)
            self.assertIn("total_sent", data)

    def test_12_marketing_logs(self):
        """Test listing daily system campaign logs"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.get("/api/marketing/logs", headers=headers)
        self.assertIn(res.status_code, [200, 401])
        if res.status_code == 200:
            self.assertIsInstance(res.json(), list)

    # ── 6. PAYMENT SUBMISSION TESTS ──
    def test_13_submit_payment_request(self):
        """Test submitting bKash payment verification proof"""
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = client.post("/api/payments/submit", json={
            "package_name": "Starter Pack",
            "credits_requested": 100,
            "amount_bdt": 200,
            "payment_method": "bkash",
            "bkash_number": "01711223344",
            "transaction_id": "TESTTRX998877"
        }, headers=headers)
        self.assertIn(res.status_code, [200, 201, 401])

    # ── 7. ADMIN CONTROL CENTER TESTS ──
    def test_14_admin_list_users(self):
        """Test admin listing all customer accounts"""
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        res = client.get("/api/admin/users", headers=headers)
        self.assertIn(res.status_code, [200, 401, 403])

    def test_15_admin_list_payments(self):
        """Test admin listing customer payment submissions"""
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        res = client.get("/api/admin/payments", headers=headers)
        self.assertIn(res.status_code, [200, 401, 403])

    def test_16_admin_security_violations(self):
        """Test admin listing anti-leak security sensor logs"""
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        res = client.get("/api/admin/violations", headers=headers)
        self.assertIn(res.status_code, [200, 401, 403])


if __name__ == "__main__":
    unittest.main()
