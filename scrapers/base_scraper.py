import time
import os
import gc
import urllib.parse
from typing import Optional, Callable, List, Dict, Any

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from scraper import (
    setup_driver,
    push_scraper_frame,
    clear_scraper_frame,
    is_job_stopped,
    clear_job_stop,
    publish_scraper_event,
    add_job_log,
)

class BaseSearchPaginationScraper:
    """
    Extensible base class for search-and-pagination e-commerce & directory scrapers
    (e.g., Daraz, Bikroy, BDJobs).
    """

    def __init__(
        self,
        job_id: Optional[int] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        enable_frames: bool = True
    ):
        self.job_id = job_id
        self.log_cb = log_cb or print
        self.enable_frames = enable_frames
        self.driver: Optional[webdriver.Chrome] = None
        self.results: List[Dict[str, Any]] = []

    def log(self, message: str):
        """Log message to in-memory buffer, database callback, and websocket"""
        if self.job_id:
            add_job_log(self.job_id, message)
        try:
            self.log_cb(message)
        except Exception:
            pass

    def is_stopped(self) -> bool:
        """Check if this scraper job has received a stop signal"""
        if not self.job_id:
            return False
        return is_job_stopped(self.job_id)

    def push_frame(self):
        """Send live visual screenshot frame to subscribers"""
        if self.driver and self.job_id and self.enable_frames:
            try:
                push_scraper_frame(self.driver, self.job_id)
            except Exception:
                pass

    def publish_progress(self, count: int, action: str, item: Optional[Dict[str, Any]] = None):
        """Broadcast live progress event to WebSocket subscribers"""
        if self.job_id:
            publish_scraper_event(self.job_id, {
                "type": "progress",
                "count": count,
                "action": action,
                "item": item or {}
            })

    def init_driver(self, headless: bool = True, eager: bool = True) -> webdriver.Chrome:
        """Initialize browser driver with fast page-load strategy and stealth"""
        self.log("🚀 Initializing browser engine...")
        # setup_driver sets up Chrome / Edge with stealth
        driver = setup_driver(headless=headless, log_cb=self.log)
        if eager:
            try:
                driver.execute_cdp_cmd("Network.enable", {})
            except Exception:
                pass
        self.driver = driver
        return driver

    def close(self):
        """Clean up driver and memory"""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        if self.job_id:
            clear_scraper_frame(self.job_id)
        gc.collect()

    # ── Hooks to be implemented by site-specific engines ──

    def navigate_and_search(self, query: str) -> bool:
        """Navigate to portal and submit search query in search bar"""
        raise NotImplementedError

    def get_items_on_page(self, page_num: int) -> List[str]:
        """Collect product/item URLs present on the current catalog/search page"""
        raise NotImplementedError

    def extract_item_details(self, item_url: str, page_num: int) -> Optional[Dict[str, Any]]:
        """Visit item detail page and extract structured product/item fields"""
        raise NotImplementedError

    def go_to_next_page(self, current_page: int, next_page: int, query: str) -> bool:
        """Navigate to next pagination page"""
        raise NotImplementedError

    # ── Main Scraping Workflow Template ──

    def run(
        self,
        query: str,
        max_pages: int = 1,
        max_items: Optional[int] = None,
        headless: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Execute automated search and pagination scraping cycle.
        """
        self.results = []
        try:
            self.init_driver(headless=headless, eager=True)
            self.log(f"🌐 Navigating to site and searching for: '{query}'")

            search_ok = self.navigate_and_search(query)
            self.push_frame()

            if not search_ok:
                self.log(f"⚠️ Search submission failed or returned no results for query '{query}'.")
                return self.results

            for page in range(1, max_pages + 1):
                if self.is_stopped():
                    self.log("⏹️ Interruption signal received. Halting pagination loop...")
                    break

                if max_items and len(self.results) >= max_items:
                    self.log(f"🎯 Target item limit ({max_items}) reached. Stopping scraper.")
                    break

                self.log(f"📄 [PAGE {page}/{max_pages}] Scanning page for items...")
                self.push_frame()

                # Collect item URLs on this page
                item_urls = self.get_items_on_page(page)
                self.log(f"🎯 Found {len(item_urls)} items on page {page}.")

                if not item_urls:
                    self.log(f"⚠️ No items discovered on page {page}. Ending pagination.")
                    break

                for idx, url in enumerate(item_urls):
                    if self.is_stopped():
                        self.log("⏹️ Stop signal received. Halting item extraction...")
                        break

                    if max_items and len(self.results) >= max_items:
                        break

                    item_data = self.extract_item_details(url, page)
                    if item_data:
                        self.results.append(item_data)
                        count = len(self.results)
                        name_snip = item_data.get("Product Name") or item_data.get("Title") or item_data.get("Name") or "Item"
                        price_snip = item_data.get("Sale Price (BDT)") or item_data.get("Price") or ""
                        shop_snip = item_data.get("Shop Name") or item_data.get("Seller") or ""

                        self.log(f"✅ [SAVED #{count}] '{str(name_snip)[:35]}' | {price_snip} | {shop_snip}")
                        self.publish_progress(
                            count=count,
                            action=f"Extracted #{count}: {str(name_snip)[:40]}",
                            item=item_data
                        )

                        # Capture live screenshot periodically to keep UI responsive
                        if idx % 3 == 0 or idx == len(item_urls) - 1:
                            self.push_frame()

                    time.sleep(0.3)

                # If more pages requested, navigate to next page
                if page < max_pages and not self.is_stopped():
                    if max_items and len(self.results) >= max_items:
                        break
                    self.log(f"⏩ Moving to page {page + 1} of {max_pages}...")
                    nav_ok = self.go_to_next_page(page, page + 1, query)
                    if not nav_ok:
                        self.log(f"⚠️ Could not navigate to page {page + 1}. Pagination finished.")
                        break
                    time.sleep(1.5)
                    self.push_frame()

        except Exception as ex:
            self.log(f"⚠️ Scraper encountered an exception: {ex}")
        finally:
            self.close()

        return self.results
