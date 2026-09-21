import unittest
import os
import math
import tempfile
import pandas as pd
from fastapi.testclient import TestClient

os.environ["TESTING"] = "1"
from main import app, get_db
from schemas.scraper import DarazScrapeRequest
from scrapers.daraz_scraper import save_daraz_to_excel

client = TestClient(app)

class TestDarazScraperEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from config import SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD
        cls.test_email = "daraz_tester@example.com"
        cls.test_password = "Password123!"

        # Register user
        client.post("/api/auth/register", json={
            "full_name": "Daraz Tester",
            "email": cls.test_email,
            "password": cls.test_password
        })

        # Ensure user is verified and has 50 credits
        conn = get_db()
        conn.execute("UPDATE users SET is_verified = 1, credits = 50 WHERE email = ?", (cls.test_email,))
        conn.commit()

        # Fetch user id
        user_row = conn.execute("SELECT id FROM users WHERE email = ?", (cls.test_email,)).fetchone()
        cls.user_id = user_row["id"]
        conn.close()

        # Login test user
        login_res = client.post("/api/auth/login", json={
            "email": cls.test_email,
            "password": cls.test_password
        })
        cls.user_token = login_res.json()["token"]

        # Login admin user
        admin_login = client.post("/api/auth/login", json={
            "email": SUPERADMIN_EMAIL,
            "password": SUPERADMIN_PASSWORD
        })
        cls.admin_token = admin_login.json()["token"]

    def test_01_credit_economics_calculation(self):
        """Verify 20 credits per 10 pages formula"""
        self.assertEqual(math.ceil(1 / 10.0) * 20, 20)
        self.assertEqual(math.ceil(5 / 10.0) * 20, 20)
        self.assertEqual(math.ceil(10 / 10.0) * 20, 20)
        self.assertEqual(math.ceil(11 / 10.0) * 20, 40)
        self.assertEqual(math.ceil(20 / 10.0) * 20, 40)

    def test_02_save_daraz_to_excel(self):
        """Test formatting and generation of Daraz Excel spreadsheet"""
        sample_data = [
            {
                "Product Name": "HP W10 Wireless Bluetooth Mouse",
                "Sale Price (BDT)": "৳ 299",
                "Original Price (BDT)": "৳ 550",
                "Discount": "-46%",
                "Shop Name": "Aysha Tech",
                "Shop Rating": "95%",
                "Brand": "HP",
                "Rating": "4.8",
                "Review Count": "304",
                "Stock Status": "In Stock",
                "Max Order Qty": "15",
                "Product URL": "https://www.daraz.com.bd/products/hp-mouse-i123.html",
                "Image URL": "https://bd-live-21.slatic.net/test.jpg",
                "Category": "Computers > Accessories > Mice",
                "Search Query": "mouse",
                "Page Number": 1,
            },
            {
                "Product Name": "RGB Gaming Mouse",
                "Sale Price (BDT)": "৳ 180",
                "Original Price (BDT)": "৳ 300",
                "Discount": "-40%",
                "Shop Name": "Gadget Store",
                "Shop Rating": "90%",
                "Brand": "No Brand",
                "Rating": "4.5",
                "Review Count": "50",
                "Stock Status": "Out of Stock",
                "Max Order Qty": "0",
                "Product URL": "https://www.daraz.com.bd/products/rgb-mouse-i456.html",
                "Image URL": "",
                "Category": "Computers > Accessories",
                "Search Query": "mouse",
                "Page Number": 1,
            }
        ]

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            count = save_daraz_to_excel(sample_data, tmp_path)
            self.assertEqual(count, 2)
            self.assertTrue(os.path.exists(tmp_path))
            df = pd.read_excel(tmp_path)
            self.assertEqual(len(df), 2)
            self.assertIn("Product Name", df.columns)
            self.assertIn("Sale Price (BDT)", df.columns)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    from unittest.mock import patch

    @patch("routers.scraper.run_background_daraz_scrape")
    def test_03_trigger_daraz_scrape_endpoint(self, mock_task):
        """Test POST /api/scraper/daraz/scrape creates job and deducts credits"""
        res = client.post(
            "/api/scraper/daraz/scrape",
            headers={"Authorization": f"Bearer {self.user_token}"},
            json={
                "query": "wireless mouse",
                "pages": 1,
                "max_items": 10,
                "headless": True
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIn("job_id", data)
        job_id = data["job_id"]

        # Verify background task dispatched
        self.assertTrue(mock_task.called)

        # Verify job recorded with scraper_type = 'daraz'
        conn = get_db()
        job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertIsNotNone(job)
        self.assertEqual(job["scraper_type"], "daraz")
        self.assertEqual(job["cost_credits"], 20)

        # Check credits deducted (50 - 20 = 30)
        user = conn.execute("SELECT credits FROM users WHERE id = ?", (self.user_id,)).fetchone()
        self.assertEqual(user["credits"], 30)
        conn.close()

    def test_04_get_daraz_jobs_endpoint(self):
        """Test GET /api/scraper/daraz/jobs returns Daraz jobs"""
        res = client.get(
            "/api/scraper/daraz/jobs",
            headers={"Authorization": f"Bearer {self.user_token}"}
        )
        self.assertEqual(res.status_code, 200)
        jobs = res.json()
        self.assertIsInstance(jobs, list)
        self.assertTrue(any(j.get("scraper_type") == "daraz" for j in jobs))

    def test_05_download_permission_restriction(self):
        """Test raw Excel download restriction: Free user blocked, Admin allowed"""
        # Create a mock completed Daraz job
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = tmp.name
        save_daraz_to_excel([{"Product Name": "Test Product", "Sale Price (BDT)": "৳ 100"}], tmp_path)

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO scrape_jobs (user_id, query, scraper_type, status, result_path, result_count) VALUES (?, 'test', 'daraz', 'done', ?, 1)",
            (self.user_id, tmp_path)
        )
        conn.commit()
        job_id = cursor.lastrowid
        conn.close()

        # Free user attempts to download raw Daraz file -> 403 Forbidden
        free_dl_res = client.get(
            f"/api/scraper/jobs/{job_id}/download",
            headers={"Authorization": f"Bearer {self.user_token}"}
        )
        self.assertEqual(free_dl_res.status_code, 403)
        self.assertIn("Pro and Enterprise", free_dl_res.json()["detail"])

        # Free user can still view parsed data via /data endpoint
        data_res = client.get(
            f"/api/scraper/jobs/{job_id}/data",
            headers={"Authorization": f"Bearer {self.user_token}"}
        )
        self.assertEqual(data_res.status_code, 200)
        self.assertEqual(data_res.json()["count"], 1)

        # Admin user can download successfully
        admin_dl_res = client.get(
            f"/api/scraper/jobs/{job_id}/download",
            headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(admin_dl_res.status_code, 200)

        # Cleanup
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        conn = get_db()
        conn.execute("DELETE FROM scrape_jobs WHERE id = ?", (job_id,))
        conn.commit()
        conn.close()

if __name__ == "__main__":
    unittest.main()
