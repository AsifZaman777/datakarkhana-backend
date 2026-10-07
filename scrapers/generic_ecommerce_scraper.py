import time
import os
import re
import json
import urllib.parse
from typing import Optional, Callable, List, Dict, Any, Tuple
from bs4 import BeautifulSoup

import pandas as pd
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from scrapers.base_scraper import BaseSearchPaginationScraper

# ── Supported Platform Preset Definitions ──
SUPPORTED_PLATFORMS = {
    "ebay": {
        "name": "eBay",
        "domain": "ebay.com",
        "search_pattern": "https://www.ebay.com/sch/i.html?_nkw={query}&_pgn={page}",
        "card_selector": "li.s-item, div.s-item__wrapper",
        "title_selector": ".s-item__title, span[role='heading']",
        "price_selector": ".s-item__price",
        "orig_price_selector": ".s-item__trending-price, .s-item__discount-price, .strikethrough",
        "rating_selector": ".x-star-rating span, .clipped",
        "reviews_selector": ".s-item__reviews-count span",
        "image_selector": ".s-item__image-img img, img.s-item__image-img",
        "link_selector": "a.s-item__link",
        "next_btn_selector": "a.pagination__next, a[aria-label='Go to next search page']",
    },
    "pickaboo": {
        "name": "Pickaboo",
        "domain": "pickaboo.com",
        "search_pattern": "https://www.pickaboo.com/search-result/{query}?page={page}",
        "card_selector": ".product-item, .product-item-info, div.item",
        "title_selector": ".product-item-name a, a.product-item-link, .product-title",
        "price_selector": ".price-box .special-price .price, .price-box .price, .product-price",
        "orig_price_selector": ".price-box .old-price .price",
        "rating_selector": ".rating-result, [itemprop='ratingValue']",
        "reviews_selector": ".reviews-actions a, [itemprop='reviewCount']",
        "image_selector": "img.product-image-photo, .product-image img",
        "link_selector": "a.product-item-link, .product-item-name a",
        "next_btn_selector": "a.action.next, .pages-item-next a",
    },
    "amazon": {
        "name": "Amazon",
        "domain": "amazon.com",
        "search_pattern": "https://www.amazon.com/s?k={query}&page={page}",
        "card_selector": "div[data-component-type='s-search-result']",
        "title_selector": "h2 a span, h2 span, h2",
        "price_selector": ".a-price .a-offscreen, .a-price-whole",
        "orig_price_selector": ".a-price.a-text-price .a-offscreen",
        "rating_selector": "i[class*='a-star-'] span, span[aria-label*='stars']",
        "reviews_selector": "span[aria-label*='ratings'], a[href*='#customerReviews'] span",
        "image_selector": "img.s-image",
        "link_selector": "h2 a.a-link-normal",
        "next_btn_selector": "a.s-pagination-next",
    },
    "startech": {
        "name": "Star Tech",
        "domain": "startech.com.bd",
        "search_pattern": "https://www.startech.com.bd/product/search?search={query}&page={page}",
        "card_selector": ".p-item",
        "title_selector": ".p-item-name a",
        "price_selector": ".p-item-price span:first-child",
        "orig_price_selector": ".p-item-price .price-old",
        "rating_selector": ".star-rating, .rating",
        "reviews_selector": "",
        "image_selector": ".p-item-img img",
        "link_selector": ".p-item-name a",
        "next_btn_selector": "ul.pagination li a:contains('>')",
    },
    "ryans": {
        "name": "Ryans Computers",
        "domain": "ryans.com",
        "search_pattern": "https://www.ryans.com/search?search={query}&page={page}",
        "card_selector": ".product-box, .cus-col-2, .card",
        "title_selector": ".card-title a, p.card-text a, .product-title",
        "price_selector": ".pr-text, .price",
        "orig_price_selector": ".old-price",
        "rating_selector": "",
        "reviews_selector": "",
        "image_selector": "img.card-img-top, .image-box img",
        "link_selector": ".card-title a, a.card-link",
        "next_btn_selector": "a[rel='next'], .pagination .next a",
    },
    "rokomari": {
        "name": "Rokomari",
        "domain": "rokomari.com",
        "search_pattern": "https://www.rokomari.com/search?term={query}&search_type=ALL&page={page}",
        "card_selector": ".book-list-wrapper, .product-box",
        "title_selector": ".book-title, .product-title",
        "price_selector": ".current-price, .price",
        "orig_price_selector": ".original-price",
        "rating_selector": ".rating-value",
        "reviews_selector": ".total-review",
        "image_selector": "img.book-img, img.product-img",
        "link_selector": "a.book-link, a.product-link",
        "next_btn_selector": ".pagination a.next",
    },
    "chaldal": {
        "name": "Chaldal",
        "domain": "chaldal.com",
        "search_pattern": "https://chaldal.com/search/{query}",
        "card_selector": ".product, .productListing",
        "title_selector": ".name, .product-name",
        "price_selector": ".price span, .price",
        "orig_price_selector": ".discountedPrice span",
        "rating_selector": "",
        "reviews_selector": "",
        "image_selector": "img",
        "link_selector": "a",
        "next_btn_selector": "",
    }
}


def detect_platform_from_url(url: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Inspect domain in URL to match against known platform presets."""
    clean_url = url.lower()
    for key, spec in SUPPORTED_PLATFORMS.items():
        if spec["domain"] in clean_url:
            return key, spec
    return "generic", None


def extract_currency(text: str) -> str:
    """Identify currency symbol or code from raw price text."""
    if not text:
        return ""
    if "৳" in text or "tk" in text.lower() or "bdt" in text.lower():
        return "BDT"
    if "$" in text or "usd" in text.lower():
        return "USD"
    if "€" in text or "eur" in text.lower():
        return "EUR"
    if "£" in text or "gbp" in text.lower():
        return "GBP"
    if "₹" in text or "inr" in text.lower():
        return "INR"
    return ""


def clean_price(text: str) -> str:
    """Normalize price string preserving currency."""
    if not text:
        return ""
    text = text.strip()
    # If text is e.g. "$129.99 to $159.99"
    text = re.sub(r"\s+", " ", text)
    return text


def extract_json_ld_products(html: str, base_url: str = "") -> List[Dict[str, Any]]:
    """
    Extract products from Schema.org JSON-LD scripts embedded in the page.
    Very resilient across modern Shopify, WooCommerce, Next.js Commerce, Magento, etc.
    """
    items = []
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")

    for s in scripts:
        try:
            content = s.string or s.text
            if not content:
                continue
            data = json.loads(content.strip())
            # Normalize to list
            candidates = data if isinstance(data, list) else [data]

            for entry in candidates:
                if not isinstance(entry, dict):
                    continue

                entry_type = entry.get("@type")

                # Handle ItemList
                if entry_type == "ItemList" and "itemListElement" in entry:
                    elements = entry.get("itemListElement", [])
                    for elem in elements:
                        item_obj = elem.get("item") if isinstance(elem, dict) and "item" in elem else elem
                        if isinstance(item_obj, dict):
                            parsed = parse_single_json_ld_product(item_obj, base_url)
                            if parsed:
                                items.append(parsed)

                # Handle direct Product
                elif entry_type == "Product":
                    parsed = parse_single_json_ld_product(entry, base_url)
                    if parsed:
                        items.append(parsed)

                # Handle @graph
                elif "@graph" in entry and isinstance(entry["@graph"], list):
                    for sub in entry["@graph"]:
                        if isinstance(sub, dict) and sub.get("@type") == "Product":
                            parsed = parse_single_json_ld_product(sub, base_url)
                            if parsed:
                                items.append(parsed)

        except Exception:
            continue

    return items


def parse_single_json_ld_product(prod: Dict[str, Any], base_url: str = "") -> Optional[Dict[str, Any]]:
    """Helper to convert Schema.org Product object into normalized dictionary."""
    name = prod.get("name") or prod.get("title")
    if not name:
        return None

    # Offers
    offers = prod.get("offers", {})
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if not isinstance(offers, dict):
        offers = {}

    price = str(offers.get("price") or offers.get("lowPrice") or "")
    currency = offers.get("priceCurrency") or ""
    formatted_price = f"{currency} {price}".strip() if price else ""

    availability = offers.get("availability") or ""
    stock_status = "In Stock" if "InStock" in availability else ("Out of Stock" if "OutOfStock" in availability else "Available")

    # Ratings
    agg_rating = prod.get("aggregateRating", {})
    if not isinstance(agg_rating, dict):
        agg_rating = {}
    rating = str(agg_rating.get("ratingValue") or "")
    reviews = str(agg_rating.get("reviewCount") or agg_rating.get("ratingCount") or "")

    # Brand
    brand_val = prod.get("brand")
    brand = brand_val.get("name") if isinstance(brand_val, dict) else (brand_val if isinstance(brand_val, str) else "")

    # Image
    image = prod.get("image")
    if isinstance(image, list) and image:
        image = image[0]
    elif isinstance(image, dict):
        image = image.get("url") or ""
    image = str(image or "")

    # URL
    url = offers.get("url") or prod.get("url") or ""
    if url and not url.startswith("http") and base_url:
        url = urllib.parse.urljoin(base_url, url)

    return {
        "Product Name": str(name).strip(),
        "Sale Price": formatted_price,
        "Original Price": "",
        "Discount": "",
        "Rating": rating,
        "Review Count": reviews,
        "Stock Status": stock_status,
        "Shop Name": brand or "Store",
        "Brand": brand,
        "Product URL": url,
        "Image URL": image,
    }


class GenericEcommerceScraper(BaseSearchPaginationScraper):
    """
    Universal Plug-and-Play E-Commerce Engine.
    Capable of crawling any e-commerce catalog, search results, or single product URL
    (e.g., eBay, Pickaboo, Amazon, Star Tech, Ryans, Chaldal, Rokomari, or any custom online store).
    Extracts price, original price, discount, ratings, reviews, stock availability, and images.
    """

    def __init__(
        self,
        job_id: Optional[int] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        enable_frames: bool = True
    ):
        super().__init__(job_id=job_id, log_cb=log_cb, enable_frames=enable_frames)
        self.target_url: str = ""
        self.current_query: str = ""
        self.platform_key: str = "generic"
        self.platform_spec: Optional[Dict[str, Any]] = None
        self.current_page: int = 1

    def resolve_target_url(self, raw_input_url: Optional[str], query: Optional[str], platform: Optional[str]) -> str:
        """
        Build or sanitize target start URL.
        Accepts:
        - Full search or category link (e.g. https://www.ebay.com/sch/i.html?_nkw=drone)
        - Homepage with query (e.g. https://www.pickaboo.com with query="earbuds")
        - Platform preset name + query (e.g. platform="ebay", query="shoes")
        """
        url = (raw_input_url or "").strip()
        q = (query or "").strip()
        plat = (platform or "").lower().strip()

        # If platform preset requested without URL
        if plat in SUPPORTED_PLATFORMS and not url:
            spec = SUPPORTED_PLATFORMS[plat]
            if spec.get("search_pattern") and q:
                return spec["search_pattern"].format(query=urllib.parse.quote_plus(q), page=1)
            return f"https://{spec['domain']}"

        if not url:
            if plat and plat in SUPPORTED_PLATFORMS:
                spec = SUPPORTED_PLATFORMS[plat]
                return f"https://{spec['domain']}"
            return "https://www.google.com"

        # Ensure scheme
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        # Auto-detect platform from URL
        detected_key, detected_spec = detect_platform_from_url(url)
        if detected_spec:
            self.platform_key = detected_key
            self.platform_spec = detected_spec

        # If it's a bare domain and a query was given, apply known search pattern
        parsed = urllib.parse.urlparse(url)
        is_homepage = parsed.path in ("", "/") and not parsed.query

        if is_homepage and q:
            if self.platform_spec and self.platform_spec.get("search_pattern"):
                return self.platform_spec["search_pattern"].format(query=urllib.parse.quote_plus(q), page=1)
            # Generic search param injection
            return f"{url.rstrip('/')}/search?q={urllib.parse.quote_plus(q)}"

        return url

    def navigate_to_start(self, start_url: str, query: str = "") -> bool:
        """Load start URL, wait for dynamic hydration, and push initial frame."""
        self.target_url = start_url
        self.current_query = query
        self.log(f"🌐 [START] Navigating to target portal: {start_url}")

        self.driver.get(start_url)
        time.sleep(2.5)
        self.push_frame()

        # If user supplied a query, but the URL is still a homepage, attempt on-page search input
        parsed = urllib.parse.urlparse(self.driver.current_url)
        if query and parsed.path in ("", "/") and not parsed.query:
            self.log(f"🔍 [AUTO-SEARCH] Attempting to submit query '{query}' via portal search box...")
            search_selectors = [
                "input[name='q']", "input[name='_nkw']", "input[name='search']",
                "input[type='search']", "input#search", "input#twotabsearchtextbox",
                "input.search-box__input", "input.search-input"
            ]
            for sel in search_selectors:
                try:
                    found = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    if found and found[0].is_displayed():
                        box = found[0]
                        box.clear()
                        box.send_keys(query)
                        time.sleep(0.4)
                        box.send_keys(Keys.ENTER)
                        time.sleep(2.5)
                        self.push_frame()
                        self.log(f"📍 [NAVIGATED] Active URL after search submission: {self.driver.current_url}")
                        return True
                except Exception:
                    pass

        return True

    def smooth_scroll_page(self):
        """Scroll through the page smoothly to trigger lazy-loaded cards and images."""
        try:
            total_height = self.driver.execute_script("return document.body.scrollHeight;")
            steps = [0.25, 0.5, 0.75, 1.0]
            for s in steps:
                target = int(total_height * s)
                self.driver.execute_script(f"window.scrollTo(0, {target});")
                time.sleep(0.4)
        except Exception:
            pass

    def extract_items_from_current_page(self, page_num: int) -> List[Dict[str, Any]]:
        """
        Extract structured items from current page using multi-layered extraction:
        1. JSON-LD schema parsing (instant, accurate, structured)
        2. Platform-specific selectors (if matched)
        3. Universal DOM card heuristics (finds repeating product blocks)
        """
        self.smooth_scroll_page()
        page_html = self.driver.page_source
        current_url = self.driver.current_url

        # Layer 1: JSON-LD Product extraction
        json_ld_products = extract_json_ld_products(page_html, base_url=current_url)
        if json_ld_products and len(json_ld_products) >= 3:
            self.log(f"⚡ [PARSER] Discovered {len(json_ld_products)} structured items via JSON-LD Microdata.")
            for p in json_ld_products:
                p["Page Number"] = page_num
                p["Platform"] = self.platform_spec["name"] if self.platform_spec else "E-Commerce"
                p["Search Query"] = self.current_query or current_url
            return json_ld_products

        # Layer 2: Platform-specific selectors or Layer 3: Universal DOM Heuristics
        items = self.extract_dom_items(page_html, current_url, page_num)
        return items

    def extract_dom_items(self, html: str, base_url: str, page_num: int) -> List[Dict[str, Any]]:
        """Extract items using BeautifulSoup for high-speed, crash-resilient parsing."""
        soup = BeautifulSoup(html, "html.parser")
        results: List[Dict[str, Any]] = []
        seen_titles = set()

        spec = self.platform_spec

        # Determine candidate card selector
        card_selectors = []
        if spec and spec.get("card_selector"):
            card_selectors.append(spec["card_selector"])

        # Universal fallback card selectors
        card_selectors.extend([
            "div[data-component-type='s-search-result']",
            "li.s-item",
            ".product-item",
            ".product-card",
            ".p-item",
            ".product-box",
            "div.item-card",
            "article.product",
            "div[class*='product-item']",
            "div[class*='product_item']",
            "div[class*='productCard']",
            "div[class*='ProductCard']",
            "div[class*='product-card']",
            "div[class*='item-card']",
            "div[class*='goods-item']",
            "div[data-product-id]",
            "div[data-qa-locator='product-item']"
        ])

        cards = []
        for selector in card_selectors:
            try:
                found = soup.select(selector)
                if len(found) >= 2:
                    cards = found
                    break
            except Exception:
                continue

        # If specific card containers not found, look for repeating elements with price and link
        if not cards:
            cards = self.find_generic_product_containers(soup)

        platform_name = spec["name"] if spec else "E-Commerce"

        for card in cards:
            try:
                # ── Title ──
                title = ""
                if spec and spec.get("title_selector"):
                    t_el = card.select_one(spec["title_selector"])
                    if t_el:
                        title = t_el.get_text(strip=True)

                if not title:
                    for t_sel in ["h2 a", "h3 a", "h2", "h3", ".title a", ".title", ".product-name a", ".name a", "a[class*='title']", "a[class*='name']"]:
                        t_el = card.select_one(t_sel)
                        if t_el and len(t_el.get_text(strip=True)) > 4:
                            title = t_el.get_text(strip=True)
                            break

                if not title or len(title) < 3 or title.lower() in ("shop on ebay", "sponsored", "results"):
                    continue

                if title in seen_titles:
                    continue
                seen_titles.add(title)

                # ── Price ──
                price = ""
                if spec and spec.get("price_selector"):
                    p_el = card.select_one(spec["price_selector"])
                    if p_el:
                        price = clean_price(p_el.get_text(strip=True))

                if not price:
                    for p_sel in [
                        "[class*='price']:not([class*='old']):not([class*='original'])",
                        "[data-price]",
                        ".price", ".sale-price", ".special-price", ".current-price",
                        "span[class*='price']", "div[class*='price']"
                    ]:
                        p_el = card.select_one(p_sel)
                        if p_el:
                            candidate_price = p_el.get_text(strip=True)
                            if re.search(r"[\$৳€£₹]|\d+", candidate_price):
                                price = clean_price(candidate_price)
                                break

                # Regex fallback for price in text
                if not price:
                    m = re.search(r"([\$৳€£₹]|BDT|USD|Tk\.?)\s*([\d,]+(?:\.\d{2})?)", card.get_text())
                    if m:
                        price = f"{m.group(1)} {m.group(2)}".strip()

                # ── Original Price & Discount ──
                orig_price = ""
                if spec and spec.get("orig_price_selector"):
                    op_el = card.select_one(spec["orig_price_selector"])
                    if op_el:
                        orig_price = clean_price(op_el.get_text(strip=True))

                if not orig_price:
                    for op_sel in [".old-price", ".original-price", "del", ".strikethrough", "[class*='strike']", "[class*='discount-price']"]:
                        op_el = card.select_one(op_sel)
                        if op_el:
                            orig_price = clean_price(op_el.get_text(strip=True))
                            break

                discount = ""
                disc_match = re.search(r"-?\b(\d{1,2}%|\d{1,2}\s*off)\b", card.get_text(), re.IGNORECASE)
                if disc_match:
                    discount = disc_match.group(1).upper()

                # ── Rating & Reviews ──
                rating = ""
                reviews = ""
                if spec and spec.get("rating_selector"):
                    r_el = card.select_one(spec["rating_selector"])
                    if r_el:
                        rating = r_el.get_text(strip=True)

                if spec and spec.get("reviews_selector"):
                    rev_el = card.select_one(spec["reviews_selector"])
                    if rev_el:
                        reviews = rev_el.get_text(strip=True)

                # Heuristic rating
                if not rating:
                    rating_match = re.search(r"(\b[1-5](?:\.\d)?\b)\s*(?:out of 5|stars|★|/5)", card.get_text(), re.IGNORECASE)
                    if rating_match:
                        rating = rating_match.group(1)

                if not reviews:
                    rev_match = re.search(r"\(?(\d[\d,]*)\)?\s*(?:reviews|ratings|sold|feedback)", card.get_text(), re.IGNORECASE)
                    if rev_match:
                        reviews = rev_match.group(1).replace(",", "")

                # ── Link ──
                link = ""
                if spec and spec.get("link_selector"):
                    a_el = card.select_one(spec["link_selector"])
                    if a_el and a_el.get("href"):
                        link = a_el["href"]

                if not link:
                    a_el = card.find("a", href=True)
                    if a_el:
                        link = a_el["href"]

                if link and not link.startswith("http"):
                    link = urllib.parse.urljoin(base_url, link)

                # Filter tracking noise in URL
                clean_link = link.split("?")[0] if ("?" in link and "ebay" not in link) else link

                # ── Image ──
                image = ""
                if spec and spec.get("image_selector"):
                    img_el = card.select_one(spec["image_selector"])
                    if img_el:
                        image = img_el.get("src") or img_el.get("data-src") or ""

                if not image:
                    img_el = card.find("img")
                    if img_el:
                        image = img_el.get("src") or img_el.get("data-src") or img_el.get("data-lazy") or ""

                if image and image.startswith("//"):
                    image = "https:" + image
                elif image and not image.startswith("http"):
                    image = urllib.parse.urljoin(base_url, image)

                # ── Stock Status ──
                card_text = card.get_text().lower()
                stock_status = "In Stock"
                if "out of stock" in card_text or "sold out" in card_text or "স্টক শেষ" in card_text:
                    stock_status = "Out of Stock"
                elif "pre-order" in card_text or "preorder" in card_text:
                    stock_status = "Pre-Order"

                results.append({
                    "Product Name": title,
                    "Sale Price": price or "Check Store",
                    "Original Price": orig_price,
                    "Discount": discount,
                    "Rating": rating,
                    "Review Count": reviews or "0",
                    "Stock Status": stock_status,
                    "Shop Name": platform_name,
                    "Brand": "",
                    "Product URL": clean_link or base_url,
                    "Image URL": image,
                    "Platform": platform_name,
                    "Search Query": self.current_query or base_url,
                    "Page Number": page_num,
                })

            except Exception:
                continue

        return results

    def find_generic_product_containers(self, soup: BeautifulSoup) -> List[Any]:
        """Find repeating DOM structures that contain a heading, link, and price."""
        candidates = []
        for tag in ["div", "li", "article"]:
            elements = soup.find_all(tag)
            valid = []
            for el in elements:
                # Must contain an anchor tag and a currency sign / digit
                has_link = bool(el.find("a", href=True))
                text = el.get_text()
                has_price = bool(re.search(r"[\$৳€£₹]\s*[\d,]+", text))
                if has_link and has_price and 20 < len(text) < 1200:
                    valid.append(el)
            if len(valid) >= 3:
                candidates = valid
                break
        return candidates[:60]

    def go_to_next_page(self, current_page: int, next_page: int, query: str) -> bool:
        """
        Advance pagination using:
        1. Clicking identified 'Next' button or pagination link.
        2. Modifying URL page parameter (page=X, p=X, _pgn=X, offset=X).
        """
        # Strategy A: Try clicking next page button in DOM
        spec = self.platform_spec
        next_selectors = []
        if spec and spec.get("next_btn_selector"):
            next_selectors.append(spec["next_btn_selector"])

        next_selectors.extend([
            "a[rel='next']",
            "a.pagination__next",
            "a.action.next",
            "a.next",
            "button[aria-label*='next' i]",
            "a[aria-label*='next' i]",
            "li.next a",
            "li.ant-pagination-next button",
            "a.s-pagination-next"
        ])

        for sel in next_selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if elements and elements[0].is_enabled():
                    self.log(f"👉 [PAGINATION] Clicking next page button ({sel})...")
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", elements[0])
                    time.sleep(0.5)
                    self.driver.execute_script("arguments[0].click();", elements[0])
                    time.sleep(2.5)
                    return True
            except Exception:
                pass

        # Strategy B: Manipulate URL query parameter directly
        try:
            curr_url = self.driver.current_url
            parsed = urllib.parse.urlparse(curr_url)
            params = urllib.parse.parse_qs(parsed.query)

            # Common page parameter keys
            page_param_keys = ["page", "p", "_pgn", "page_num", "pg"]
            matched_key = None
            for k in page_param_keys:
                if k in params:
                    matched_key = k
                    break

            if not matched_key:
                # Check platform spec
                if self.platform_key == "ebay":
                    matched_key = "_pgn"
                else:
                    matched_key = "page"

            params[matched_key] = [str(next_page)]
            new_query = urllib.parse.urlencode(params, doseq=True)
            next_url = urllib.parse.urlunparse((
                parsed.scheme, parsed.netloc, parsed.path,
                parsed.params, new_query, parsed.fragment
            ))

            self.log(f"🌐 [PAGINATION] Direct navigation to page {next_page}: {next_url}")
            self.driver.get(next_url)
            time.sleep(2.5)
            return True
        except Exception as ex:
            self.log(f"⚠️ Pagination navigation encountered exception: {ex}")
            return False

    def run_ecommerce(
        self,
        url: Optional[str] = None,
        query: Optional[str] = None,
        platform: Optional[str] = "generic",
        max_pages: int = 1,
        max_items: Optional[int] = None,
        headless: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Execute automated Universal E-Commerce scraping cycle.
        """
        self.results = []
        try:
            start_url = self.resolve_target_url(url, query, platform)
            self.init_driver(headless=headless, eager=True)

            self.log(f"🚀 Launching Universal E-Commerce Scraper Engine (Target: {start_url})...")
            nav_ok = self.navigate_to_start(start_url, query or "")
            self.push_frame()

            if not nav_ok:
                self.log(f"⚠️ Failed to reach portal URL: {start_url}")
                return self.results

            for page in range(1, max_pages + 1):
                self.current_page = page
                if self.is_stopped():
                    self.log("⏹️ Interruption signal received. Halting pagination loop...")
                    break

                if max_items and len(self.results) >= max_items:
                    self.log(f"🎯 Target item limit ({max_items}) reached. Halting scraper.")
                    break

                self.log(f"📄 [PAGE {page}/{max_pages}] Scanning portal catalog for products...")
                self.push_frame()

                items = self.extract_items_from_current_page(page)
                self.log(f"🎯 Found {len(items)} product records on page {page}.")

                if not items:
                    self.log(f"⚠️ No product items detected on page {page}. Ending crawl cycle.")
                    break

                for idx, item in enumerate(items):
                    if self.is_stopped():
                        self.log("⏹️ Stop signal received. Halting product processing...")
                        break

                    if max_items and len(self.results) >= max_items:
                        break

                    self.results.append(item)
                    count = len(self.results)
                    name_snip = item.get("Product Name") or "Product"
                    price_snip = item.get("Sale Price") or ""
                    rating_snip = f"⭐ {item.get('Rating')}" if item.get("Rating") else ""

                    self.log(f"✅ [SAVED #{count}] '{str(name_snip)[:35]}' | {price_snip} | {rating_snip}")
                    self.publish_progress(
                        count=count,
                        action=f"Extracted #{count}: {str(name_snip)[:40]}",
                        item=item
                    )

                    if idx % 4 == 0 or idx == len(items) - 1:
                        self.push_frame()

                # Pagination
                if page < max_pages and not self.is_stopped():
                    if max_items and len(self.results) >= max_items:
                        break
                    self.log(f"⏩ Navigating to page {page + 1} of {max_pages}...")
                    nav_ok = self.go_to_next_page(page, page + 1, query or "")
                    if not nav_ok:
                        self.log(f"⚠️ Could not load page {page + 1}. Pagination complete.")
                        break
                    time.sleep(1.5)
                    self.push_frame()

        except Exception as ex:
            self.log(f"⚠️ Universal E-Commerce Scraper encountered an exception: {ex}")
        finally:
            self.close()

        return self.results


def save_ecommerce_to_excel(
    all_results: List[Dict[str, Any]],
    filename: str,
    platform_name: str = "E-Commerce"
) -> int:
    """
    Save Universal E-Commerce results to a styled Excel spreadsheet.
    Includes formatted Indigo header row, column widths, currency display,
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
        sheet_name = f"{platform_name[:25]} Products"
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

        # Style header row (Modern deep indigo/slate theme)
        from openpyxl.styles import PatternFill, Font, Alignment
        header_fill = PatternFill(start_color="1E1B4B", end_color="1E1B4B", fill_type="solid")
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
                if "In Stock" in cell_val or "Available" in cell_val:
                    for cell in row:
                        cell.fill = green_fill
                elif "Out of Stock" in cell_val or "Sold Out" in cell_val:
                    for cell in row:
                        cell.fill = red_fill

    return len(df)
