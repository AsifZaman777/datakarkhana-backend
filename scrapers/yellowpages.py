import time
import urllib.parse
from typing import Optional, List, Dict, Any
from selenium.webdriver.common.by import By
from scrapers.base import BaseScraper

class YellowPagesScraper(BaseScraper):
    platform_id = "yellowpages"
    platform_name = "YellowPages"
    requires_auth = False
    description = "Scrape verified local & international business directories with phone numbers & addresses"
    icon = "book-open"

    def authenticate(self, credentials: Optional[Dict[str, str]] = None) -> bool:
        # YellowPages public directory search does not require credentials
        return True

    def search_and_extract(self, query: str, max_results: int = 50) -> List[Dict[str, Any]]:
        results = []
        page = 1
        seen_names = set()

        # Parse query for potential location indicators (e.g. "Dentists in New York" or "Lawyers in Texas")
        terms = query
        location = ""
        if " in " in query.lower():
            parts = query.split(" in ", 1)
            terms = parts[0].strip()
            location = parts[1].strip()
        elif ", " in query:
            parts = query.split(", ", 1)
            terms = parts[0].strip()
            location = parts[1].strip()

        encoded_terms = urllib.parse.quote_plus(terms)
        encoded_loc = urllib.parse.quote_plus(location)

        while len(results) < max_results and page <= 5:
            if self.is_stopped():
                break

            url = f"https://www.yellowpages.com/search?search_terms={encoded_terms}&geo_location_terms={encoded_loc}&page={page}"
            self.log(f"🌐 [YellowPages Page {page}] Loading: {url}")
            self.driver.get(url)
            self.push_frame()
            time.sleep(2.5)

            if self.is_stopped():
                break

            # Find listings container / items
            cards = self.driver.find_elements(
                By.CSS_SELECTOR,
                "div.result, div.srp-listing, article.item, .search-results .info"
            )
            self.log(f"🎯 [YellowPages Page {page}] Found {len(cards)} listing items.")
            self.push_frame()

            if not cards:
                # Check if anti-bot or no results
                if "No Results" in self.driver.page_source or "zero results" in self.driver.page_source.lower():
                    self.log("ℹ️ [YellowPages] No further results found for this search term.")
                break

            for i, card in enumerate(cards):
                if self.is_stopped() or len(results) >= max_results:
                    break

                try:
                    # Business Name
                    name = ""
                    try:
                        name_el = card.find_element(By.CSS_SELECTOR, "a.business-name, h2.n a, h3.n a, .info-section h2 a")
                        name = name_el.text.strip()
                        href = name_el.get_attribute("href") or ""
                    except Exception:
                        href = ""

                    if not name or name in seen_names:
                        continue
                    seen_names.add(name)

                    # Phone
                    phone = ""
                    try:
                        phone_el = card.find_element(By.CSS_SELECTOR, "div.phones, .phone, [class*='phone']")
                        phone = self.extract_phone_numbers(phone_el.text)
                    except Exception:
                        # Fallback phone from card raw text
                        phone = self.extract_phone_numbers(card.text)

                    # Address
                    address = ""
                    try:
                        addr_parts = []
                        for sel in ["span.street-address", "span.locality", "span.address", ".adr"]:
                            els = card.find_elements(By.CSS_SELECTOR, sel)
                            for e in els:
                                if e.text.strip():
                                    addr_parts.append(e.text.strip())
                        address = ", ".join(addr_parts)
                    except Exception:
                        pass

                    # Category
                    category = ""
                    try:
                        cat_els = card.find_elements(By.CSS_SELECTOR, "div.categories a, .categories")
                        category = ", ".join([c.text.strip() for c in cat_els if c.text.strip()])
                    except Exception:
                        pass

                    # Website
                    website = ""
                    try:
                        web_el = card.find_element(By.CSS_SELECTOR, "a.track-visit-website, a[href*='website']")
                        website = web_el.get_attribute("href") or ""
                    except Exception:
                        pass

                    lead = self.normalize_lead(
                        name=name,
                        phone=phone,
                        category=category or terms,
                        address=address or location,
                        website=website,
                        profile_url=href,
                        query=query
                    )
                    results.append(lead)

                    phone_badge = f"📞 {phone}" if phone else "⚠️ No phone"
                    self.log(f"✅ [YellowPages #{len(results)}] '{name}' | {phone_badge} | 📍 {address[:30] if address else 'N/A'}")
                    self.emit_progress(
                        len(results),
                        f"Extracted #{len(results)}: {name}",
                        {"name": name, "phone": phone, "category": category, "address": address}
                    )
                    self.push_frame()

                except Exception as ex:
                    self.log(f"⚠️ [YellowPages] Error parsing item #{i+1}: {ex}")
                    continue

            page += 1
            time.sleep(1.5)

        return results
