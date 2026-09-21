import time
import os
import re
import urllib.parse
from typing import Optional, Callable, List, Dict, Any

import pandas as pd
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from scrapers.base_scraper import BaseSearchPaginationScraper

class DarazScraper(BaseSearchPaginationScraper):
    """
    High-speed Selenium scraper for Daraz Bangladesh (https://www.daraz.com.bd/)
    Extracts products across user-specified pagination, including title, price,
    discount, seller details, stock availability, ratings, and URLs.
    """

    def __init__(
        self,
        job_id: Optional[int] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        enable_frames: bool = True
    ):
        super().__init__(job_id=job_id, log_cb=log_cb, enable_frames=enable_frames)
        self.current_query = ""

    def navigate_and_search(self, query: str) -> bool:
        """Go to Daraz.com.bd, find search box, type query, and submit"""
        self.current_query = query
        url = "https://www.daraz.com.bd/"
        self.log(f"🌐 [NAVIGATE] Loading Daraz homepage: {url}")
        self.driver.get(url)
        time.sleep(2.0)
        self.push_frame()

        # Locate search input
        search_input = None
        selectors = [
            "input[name='q']",
            "input#q",
            "input[type='search']",
            ".search-box__input--O34g",
            "input.search-box__input"
        ]
        for sel in selectors:
            try:
                found = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if found and found[0].is_displayed():
                    search_input = found[0]
                    break
            except Exception:
                pass

        if search_input:
            try:
                self.log(f"⌨️ [SEARCH] Entering query '{query}' into Daraz search bar...")
                search_input.clear()
                search_input.send_keys(query)
                time.sleep(0.5)
                search_input.send_keys(Keys.ENTER)
                time.sleep(2.5)
                self.push_frame()
                self.log(f"📍 [CATALOG] Search results loaded. Current URL: {self.driver.current_url}")
                return True
            except Exception as e:
                self.log(f"⚠️ Search input interaction failed ({e}). Falling back to direct URL...")

        # Fallback to direct catalog search URL
        catalog_url = f"https://www.daraz.com.bd/catalog/?q={urllib.parse.quote_plus(query)}"
        self.log(f"🌐 [FALLBACK] Navigating directly to: {catalog_url}")
        self.driver.get(catalog_url)
        time.sleep(2.5)
        self.push_frame()
        return True

    def get_items_on_page(self, page_num: int) -> List[str]:
        """Scroll page to trigger lazy loaded items and collect product URLs"""
        try:
            # Scroll down to ensure all 40 items in grid render
            self.driver.execute_script("window.scrollTo(0, 600);")
            time.sleep(0.4)
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
            time.sleep(0.4)
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.4)
        except Exception:
            pass

        product_links: List[str] = []
        seen = set()

        # Try specific card links first, then broad /products/
        link_selectors = [
            "div[data-qa-locator='product-item'] a[href*='/products/']",
            "div[data-item-id] a[href*='/products/']",
            "div[data-tracking='product-card'] a[href*='/products/']",
            "a[href*='/products/']"
        ]

        for sel in link_selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for el in elements:
                    href = el.get_attribute("href") or ""
                    if not href or "/products/" not in href:
                        continue
                    # Normalize URL to remove tracking noise while keeping clean product ID
                    clean_url = href.split("?")[0] if "?" in href else href
                    if not clean_url.startswith("http"):
                        clean_url = "https:" + clean_url if clean_url.startswith("//") else "https://www.daraz.com.bd" + clean_url

                    if clean_url not in seen and "-i" in clean_url:
                        seen.add(clean_url)
                        product_links.append(clean_url)
                if len(product_links) >= 10:
                    break
            except Exception:
                pass

        return product_links

    def extract_item_details(self, item_url: str, page_num: int) -> Optional[Dict[str, Any]]:
        """Visit Daraz product detail page and extract structured product intelligence"""
        try:
            self.driver.get(item_url)
            # Short wait for DOM / __moduleData__ ready
            for _ in range(3):
                has_data = self.driver.execute_script("return Boolean(window.__moduleData__ && window.__moduleData__.data);")
                if has_data:
                    break
                time.sleep(0.4)

            # Evaluate rich JS data with fallback to DOM selectors
            raw = self.driver.execute_script("""
                let fields = (window.__moduleData__ && window.__moduleData__.data && window.__moduleData__.data.root && window.__moduleData__.data.root.fields) || {};
                let tracking = window.pdpTrackingData || {};
                let sku = (fields.skuInfos && fields.skuInfos["0"]) || {};
                let seller = fields.seller || {};
                let review = fields.review || {};
                let product = fields.product || {};

                let title = (sku.pdt_name) || (tracking.pdt_name) || (product.title) ||
                            (document.querySelector('.pdp-mod-product-badge-title')?.innerText) ||
                            (document.querySelector('h1')?.innerText) || '';

                let salePrice = (sku.price && sku.price.salePrice && sku.price.salePrice.text) ||
                                (sku.price && sku.price.salePrice && sku.price.salePrice.value) ||
                                (document.querySelector('.pdp-price')?.innerText) || '';

                let origPrice = (sku.price && sku.price.originalPrice && sku.price.originalPrice.text) ||
                                (sku.price && sku.price.originalPrice && sku.price.originalPrice.value) ||
                                (sku.pdt_price) ||
                                (document.querySelector('.pdp-price_type_deleted')?.innerText) || '';

                let discount = (sku.price && sku.price.discount) || (sku.pdt_discount) ||
                               (document.querySelector('.pdp-product-price__discount')?.innerText) || '';

                let sellerName = (seller.name) || (tracking.seller_name) ||
                                 (document.querySelector('.seller-name__detail-name')?.innerText) ||
                                 (document.querySelector('.seller-name')?.innerText) || '';

                let sellerRating = (seller.positiveSellerRating && seller.positiveSellerRating.value) ||
                                   (seller.percentRate) || '';

                let sellerUrl = seller.url ? (seller.url.startsWith('//') ? 'https:' + seller.url : seller.url) : '';

                let brand = (tracking.brand_name) ||
                            (document.querySelector('.pdp-product-brand__brand-link')?.innerText) || 'No Brand';

                let rating = (review.ratings && review.ratings.average) ||
                             (document.querySelector('.score-average')?.innerText) || '';

                let reviewCount = (review.ratings && review.ratings.rateCount) ||
                                  (review.paging && review.paging.totalItems) ||
                                  (document.querySelector('.pdp-review-summary__link')?.innerText) || '';

                let stockMax = (sku.quantity && sku.quantity.limit && sku.quantity.limit.max) || '';
                let isDisabled = (sku.operation && sku.operation.disable === true);
                let stockStatus = isDisabled ? 'Out of Stock' : (stockMax ? `In Stock (Max: ${stockMax})` : 'In Stock');

                let image = (sku.image) || (tracking.pdt_photo) ||
                            (document.querySelector('.gallery-preview-panel__image')?.getAttribute('src')) || '';

                let category = Array.isArray(tracking.pdt_category) ? tracking.pdt_category.join(' > ') : '';

                return {
                    title: title.trim(),
                    salePrice: String(salePrice).trim(),
                    origPrice: String(origPrice).trim(),
                    discount: String(discount).trim(),
                    sellerName: sellerName.trim(),
                    sellerRating: String(sellerRating).trim(),
                    sellerUrl: sellerUrl,
                    brand: brand.trim(),
                    rating: String(rating).trim(),
                    reviewCount: String(reviewCount).replace(/[^0-9]/g, '').trim(),
                    stockStatus: stockStatus,
                    stockMax: String(stockMax),
                    image: image,
                    category: category
                };
            """)

            title = raw.get("title") or self.driver.title.replace(" | Daraz.com.bd", "").strip()
            if not title:
                return None

            sale_price = raw.get("salePrice") or ""
            if sale_price and not sale_price.startswith("৳") and not sale_price.startswith("BDT"):
                sale_price = f"৳ {sale_price}"

            orig_price = raw.get("origPrice") or ""
            if orig_price and not orig_price.startswith("৳") and not orig_price.startswith("BDT"):
                orig_price = f"৳ {orig_price}"

            return {
                "Product Name": title,
                "Sale Price (BDT)": sale_price,
                "Original Price (BDT)": orig_price,
                "Discount": raw.get("discount") or "",
                "Shop Name": raw.get("sellerName") or "Daraz Seller",
                "Shop Rating": raw.get("sellerRating") or "",
                "Brand": raw.get("brand") or "No Brand",
                "Rating": raw.get("rating") or "",
                "Review Count": raw.get("reviewCount") or "0",
                "Stock Status": raw.get("stockStatus") or "In Stock",
                "Max Order Qty": raw.get("stockMax") or "",
                "Product URL": item_url,
                "Image URL": raw.get("image") or "",
                "Shop URL": raw.get("sellerUrl") or "",
                "Category": raw.get("category") or "",
                "Search Query": self.current_query,
                "Page Number": page_num,
            }
        except Exception as e:
            self.log(f"⚠️ Error parsing product page ({item_url[:45]}): {e}")
            return None

    def go_to_next_page(self, current_page: int, next_page: int, query: str) -> bool:
        """Click next pagination button or navigate directly to page URL"""
        try:
            # First attempt: pagination next button click
            next_btns = self.driver.find_elements(By.CSS_SELECTOR, "li.ant-pagination-next button, li.ant-pagination-next a, li[title='Next Page']")
            if next_btns and next_btns[0].is_enabled():
                self.driver.execute_script("arguments[0].click();", next_btns[0])
                time.sleep(2.0)
                return True
        except Exception:
            pass

        # Fallback to direct page URL
        try:
            next_url = f"https://www.daraz.com.bd/catalog/?page={next_page}&q={urllib.parse.quote_plus(query)}"
            self.log(f"🌐 [PAGINATION] Navigating to page {next_page}: {next_url}")
            self.driver.get(next_url)
            time.sleep(2.0)
            return True
        except Exception as e:
            self.log(f"⚠️ Pagination navigation failed: {e}")
            return False

def save_daraz_to_excel(all_results: List[Dict[str, Any]], filename: str) -> int:
    """
    Save Daraz scraping results to a professionally styled Excel spreadsheet.
    Includes formatted header row, column widths, currency formatting,
    and visual fills for stock status.
    """
    if not all_results:
        return 0

    df = pd.DataFrame(all_results)
    # Deduplicate by Product Name and Product URL if available
    subset = [c for c in ["Product Name", "Product URL"] if c in df.columns]
    if subset:
        df = df.drop_duplicates(subset=subset)

    os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)

    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        sheet_name = "Daraz Products"
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        ws = writer.sheets[sheet_name]

        # Auto-adjust column widths
        for col_idx, col_name in enumerate(df.columns, 1):
            max_len = max(
                len(str(col_name)),
                df[col_name].astype(str).map(len).max() if not df.empty else 0
            )
            col_letter = ws.cell(row=1, column=col_idx).column_letter
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 48)

        # Style header row (Daraz dark blue / slate theme)
        from openpyxl.styles import PatternFill, Font, Alignment
        header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True, size=11)
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        # Soft green fill for in-stock items
        stock_col_idx = None
        for idx, col in enumerate(df.columns, 1):
            if "Stock" in col:
                stock_col_idx = idx
                break

        if stock_col_idx:
            green_fill = PatternFill(start_color="F0FDF4", end_color="F0FDF4", fill_type="solid")
            red_fill = PatternFill(start_color="FEF2F2", end_color="FEF2F2", fill_type="solid")
            for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
                cell_val = str(row[stock_col_idx - 1].value or "")
                if "In Stock" in cell_val:
                    for cell in row:
                        cell.fill = green_fill
                elif "Out of Stock" in cell_val:
                    for cell in row:
                        cell.fill = red_fill

    return len(df)
