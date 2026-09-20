import time
from typing import Optional, List, Dict, Any
from selenium.webdriver.common.by import By
from scrapers.base import BaseScraper

class GoogleMapsScraper(BaseScraper):
    platform_id = "google_maps"
    platform_name = "Google Maps"
    requires_auth = False
    description = "Scrape local businesses, places, reviews, and contact info worldwide"
    icon = "map-pin"

    def authenticate(self, credentials: Optional[Dict[str, str]] = None) -> bool:
        # Google Maps public search does not require login
        return True

    def _scroll_results(self, scroll_times: int = 6):
        try:
            panel = self.driver.find_element(By.CSS_SELECTOR, 'div[role="feed"]')
            for i in range(scroll_times):
                if self.is_stopped():
                    break
                self.driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight", panel)
                self.log(f"📜 [Google Maps] Feed scroll {i+1}/{scroll_times} loading more places...")
                self.push_frame()
                time.sleep(1.0)
        except Exception as e:
            self.log(f"⚠️ [Google Maps] Feed scroll skipped: {e}")

    def search_and_extract(self, query: str, max_results: int = 50) -> List[Dict[str, Any]]:
        results = []
        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        self.log(f"🌐 [Google Maps] Navigating to URL: {url}")
        self.driver.get(url)
        self.push_frame()
        time.sleep(2.5)

        if self.is_stopped():
            return results

        # Dismiss cookies / dialog if present
        try:
            consent_btn = self.driver.find_element(By.CSS_SELECTOR, 'button[aria-label*="Accept all"], form[action*="consent"] button')
            consent_btn.click()
            time.sleep(1.0)
        except Exception:
            pass

        self.log(f"📍 [Google Maps] Loaded page for query: '{query}'")
        self.push_frame()

        self._scroll_results(scroll_times=8)
        self.push_frame()

        listings = self.driver.find_elements(By.CSS_SELECTOR, 'a[href*="/maps/place/"]')
        self.log(f"🎯 [Google Maps] Discovered {len(listings)} place links. Beginning extraction...")

        seen_names = set()

        for i, listing in enumerate(listings):
            if self.is_stopped() or len(results) >= max_results:
                break

            try:
                name = listing.get_attribute("aria-label") or ""
                href = listing.get_attribute("href") or ""

                if not name or name in seen_names:
                    continue
                seen_names.add(name)

                self.log(f"🔍 [Google Maps #{len(results)+1}] Inspecting '{name}'...")
                self.driver.execute_script("arguments[0].click();", listing)
                self.push_frame()
                time.sleep(1.2)
                self.push_frame()

                phone = ""
                address = ""
                website = ""
                rating = ""
                category = ""

                # Phone Extraction
                try:
                    phone_els = self.driver.find_elements(
                        By.CSS_SELECTOR,
                        'button[data-item-id*="phone"], button[aria-label*="phone"], [data-tooltip*="Copy phone"]'
                    )
                    for el in phone_els:
                        text = el.get_attribute("aria-label") or el.text
                        phone = self.extract_phone_numbers(text)
                        if phone:
                            break
                except Exception:
                    pass

                # Address Extraction
                try:
                    addr_els = self.driver.find_elements(
                        By.CSS_SELECTOR,
                        'button[data-item-id*="address"], [data-tooltip*="Copy address"]'
                    )
                    for el in addr_els:
                        txt = el.get_attribute("aria-label") or el.text
                        if txt and len(txt) > 5:
                            address = txt.replace("Address: ", "").strip()
                            break
                except Exception:
                    pass

                # Website Extraction
                try:
                    web_els = self.driver.find_elements(By.CSS_SELECTOR, 'a[data-item-id*="authority"]')
                    for el in web_els:
                        w = el.get_attribute("href") or ""
                        if w and "google" not in w:
                            website = w
                            break
                except Exception:
                    pass

                # Rating Extraction
                try:
                    rating_el = self.driver.find_element(By.CSS_SELECTOR, 'span[aria-label*="stars"]')
                    rating = rating_el.get_attribute("aria-label") or ""
                except Exception:
                    pass

                # Category Extraction
                try:
                    cat_el = self.driver.find_element(By.CSS_SELECTOR, 'button[jsaction*="category"]')
                    category = cat_el.text.strip()
                except Exception:
                    pass

                lead = self.normalize_lead(
                    name=name,
                    phone=phone,
                    category=category,
                    address=address,
                    website=website,
                    rating=rating,
                    profile_url=href,
                    query=query
                )
                results.append(lead)

                phone_badge = f"📞 {phone}" if phone else "⚠️ No phone"
                self.log(f"✅ [Google Maps #{len(results)}] '{name}' | {phone_badge}")
                self.emit_progress(
                    len(results),
                    f"Extracted #{len(results)}: {name}",
                    {"name": name, "phone": phone, "category": category, "address": address}
                )
                self.push_frame()

            except Exception as e:
                self.log(f"⚠️ [Google Maps] Error parsing item #{i+1}: {e}")
                continue

        return results
