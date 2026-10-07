import unittest
import os
import math
import tempfile
from unittest.mock import patch
import pandas as pd
from fastapi.testclient import TestClient

os.environ["TESTING"] = "1"
from main import app, get_db
from scrapers.generic_ecommerce_scraper import (
    GenericEcommerceScraper,
    detect_platform_from_url,
    clean_price,
    extract_currency,
    extract_json_ld_products,
    save_ecommerce_to_excel,
    SUPPORTED_PLATFORMS
)

client = TestClient(app)

class TestGenericEcommerceEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from config import SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD
        cls.test_email = "ecom_tester@example.com"
        cls.test_password = "Password123!"

        # Register user
        client.post("/api/auth/register", json={
            "full_name": "E-Commerce Tester",
            "email": cls.test_email,
            "password": cls.test_password
        })

        # Ensure user verified and has 60 credits
        conn = get_db()
        conn.execute("UPDATE users SET is_verified = 1, credits = 60 WHERE email = ?", (cls.test_email,))
        conn.commit()

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

    def test_01_platform_detection_and_utilities(self):
        """Test URL platform detection and price normalization"""
        plat, spec = detect_platform_from_url("https://www.ebay.com/sch/i.html?_nkw=watch")
        self.assertEqual(plat, "ebay")
        self.assertIsNotNone(spec)

        plat_pickaboo, _ = detect_platform_from_url("https://www.pickaboo.com/search-result/smartwatch")
        self.assertEqual(plat_pickaboo, "pickaboo")

        plat_amazon, _ = detect_platform_from_url("https://www.amazon.com/s?k=earbuds")
        self.assertEqual(plat_amazon, "amazon")

        plat_startech, _ = detect_platform_from_url("https://www.startech.com.bd/product/search?search=ssd")
        self.assertEqual(plat_startech, "startech")

        plat_ryans, _ = detect_platform_from_url("https://www.ryans.com/search?search=laptop")
        self.assertEqual(plat_ryans, "ryans")

        plat_rokomari, _ = detect_platform_from_url("https://www.rokomari.com/search?term=earphone&search_type=ALL")
        self.assertEqual(plat_rokomari, "rokomari")

        plat_chaldal, _ = detect_platform_from_url("https://chaldal.com/search/milk")
        self.assertEqual(plat_chaldal, "chaldal")

        plat_custom, _ = detect_platform_from_url("https://mystore.myshopify.com/collections/all")
        self.assertEqual(plat_custom, "generic")

        self.assertEqual(extract_currency("$129.99"), "USD")
        self.assertEqual(extract_currency("৳ 1,500"), "BDT")
        self.assertEqual(clean_price("  $ 49.99 \n "), "$ 49.99")

    def test_02_json_ld_schema_parser(self):
        """Verify extraction from Schema.org Product and ItemList JSON-LD"""
        sample_html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org/",
                "@type": "Product",
                "name": "Sony WH-1000XM5 Wireless Headphones",
                "image": "https://example.com/sony.jpg",
                "brand": {"@type": "Brand", "name": "Sony"},
                "aggregateRating": {
                    "@type": "AggregateRating",
                    "ratingValue": "4.9",
                    "reviewCount": "1420"
                },
                "offers": {
                    "@type": "Offer",
                    "priceCurrency": "USD",
                    "price": "398.00",
                    "availability": "https://schema.org/InStock",
                    "url": "https://example.com/sony-headphones"
                }
            }
            </script>
        </head>
        <body><h1>Products</h1></body>
        </html>
        """
        items = extract_json_ld_products(sample_html, base_url="https://example.com")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["Product Name"], "Sony WH-1000XM5 Wireless Headphones")
        self.assertIn("398", items[0]["Sale Price"])
        self.assertEqual(items[0]["Stock Status"], "In Stock")
        self.assertEqual(items[0]["Rating"], "4.9")
        self.assertEqual(items[0]["Review Count"], "1420")

    def test_03_save_ecommerce_to_excel(self):
        """Verify styled Excel export and deduplication"""
        sample_products = [
            {
                "Product Name": "Apple Watch Series 9",
                "Sale Price": "$ 399",
                "Original Price": "$ 429",
                "Discount": "7% OFF",
                "Rating": "4.8",
                "Review Count": "580",
                "Stock Status": "In Stock",
                "Shop Name": "eBay Tech Store",
                "Brand": "Apple",
                "Product URL": "https://www.ebay.com/itm/111",
                "Image URL": "https://example.com/watch.jpg",
                "Platform": "eBay",
                "Search Query": "apple watch",
                "Page Number": 1,
            },
            {
                "Product Name": "Refurbished Smart Watch",
                "Sale Price": "$ 89",
                "Original Price": "$ 150",
                "Discount": "40% OFF",
                "Rating": "4.2",
                "Review Count": "45",
                "Stock Status": "Out of Stock",
                "Shop Name": "Gadget Outlet",
                "Brand": "Generic",
                "Product URL": "https://www.ebay.com/itm/222",
                "Image URL": "",
                "Platform": "eBay",
                "Search Query": "apple watch",
                "Page Number": 1,
            }
        ]

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            saved_count = save_ecommerce_to_excel(sample_products, tmp_path, platform_name="eBay")
            self.assertEqual(saved_count, 2)
            self.assertTrue(os.path.exists(tmp_path))

            df = pd.read_excel(tmp_path)
            self.assertEqual(len(df), 2)
            self.assertIn("Product Name", df.columns)
            self.assertIn("Sale Price", df.columns)
            self.assertIn("Stock Status", df.columns)
            self.assertEqual(df.iloc[0]["Product Name"], "Apple Watch Series 9")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_04_get_platforms_endpoint(self):
        """Test GET /api/scraper/ecommerce/platforms returns configured presets"""
        res = client.get("/api/scraper/ecommerce/platforms")
        self.assertEqual(res.status_code, 200)
        platforms = res.json()
        self.assertIsInstance(platforms, list)
        platform_ids = [p["id"] for p in platforms]
        self.assertIn("ebay", platform_ids)
        self.assertIn("pickaboo", platform_ids)
        self.assertIn("amazon", platform_ids)

    @patch("routers.scraper.run_background_ecommerce_scrape")
    def test_05_trigger_ecommerce_scrape_endpoint(self, mock_task):
        """Test POST /api/scraper/ecommerce/scrape creates job and deducts credits"""
        res = client.post(
            "/api/scraper/ecommerce/scrape",
            headers={"Authorization": f"Bearer {self.user_token}"},
            json={
                "url": "https://www.ebay.com/sch/i.html?_nkw=laptop",
                "platform": "ebay",
                "pages": 1,
                "max_items": 15,
                "headless": True
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIn("job_id", data)
        job_id = data["job_id"]

        self.assertTrue(mock_task.called)

        # Verify job in DB
        conn = get_db()
        job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertIsNotNone(job)
        self.assertEqual(job["scraper_type"], "ecommerce")
        self.assertEqual(job["cost_credits"], 20)

        # Credits deducted (60 - 20 = 40)
        user = conn.execute("SELECT credits FROM users WHERE id = ?", (self.user_id,)).fetchone()
        self.assertEqual(user["credits"], 40)
        conn.close()

    def test_06_get_ecommerce_jobs_endpoint(self):
        """Test GET /api/scraper/ecommerce/jobs returns list of ecommerce jobs"""
        res = client.get(
            "/api/scraper/ecommerce/jobs",
            headers={"Authorization": f"Bearer {self.user_token}"}
        )
        self.assertEqual(res.status_code, 200)
        jobs = res.json()
        self.assertIsInstance(jobs, list)
        self.assertTrue(any(j.get("scraper_type") == "ecommerce" for j in jobs))

if __name__ == "__main__":
    unittest.main()
