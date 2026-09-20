import time
import urllib.parse
from typing import Optional, List, Dict, Any
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from scrapers.base import BaseScraper

class LinkedInScraper(BaseScraper):
    platform_id = "linkedin"
    platform_name = "LinkedIn"
    requires_auth = True
    description = "Scrape B2B Decision Makers, Executives, Companies, and Professional Profiles"
    icon = "linkedin"

    def authenticate(self, credentials: Optional[Dict[str, str]] = None) -> bool:
        if not credentials or not credentials.get("username") or not credentials.get("password"):
            self.log("⚠️ [LinkedIn] Credentials not provided. Attempting public search...")
            return True

        username = credentials.get("username", "").strip()
        password = credentials.get("password", "").strip()

        masked_user = username[:3] + "***" if len(username) > 3 else "***"
        self.log(f"🔑 [LinkedIn] Authenticating with account: {masked_user}...")

        try:
            self.driver.get("https://www.linkedin.com/login")
            self.push_frame()
            time.sleep(2.0)

            # Fill username
            user_el = self.driver.find_element(By.ID, "username")
            user_el.clear()
            user_el.send_keys(username)
            time.sleep(0.5)

            # Fill password
            pass_el = self.driver.find_element(By.ID, "password")
            pass_el.clear()
            pass_el.send_keys(password)
            self.push_frame()
            time.sleep(0.5)

            # Submit
            pass_el.send_keys(Keys.RETURN)
            self.log("🚀 [LinkedIn] Login submitted. Verifying session...")
            self.push_frame()
            time.sleep(4.0)
            self.push_frame()

            cur_url = self.driver.current_url
            if "checkpoint" in cur_url or "challenge" in cur_url:
                self.log("⚠️ [LinkedIn Security Notice] Pin/Security verification requested. Check live preview frame.")
                return True
            elif "login-submit" in cur_url or "error" in cur_url:
                self.log("❌ [LinkedIn] Login rejected. Please verify your credentials.")
                return False

            self.log("✅ [LinkedIn] Active session established.")
            return True

        except Exception as e:
            self.log(f"⚠️ [LinkedIn] Login step notice: {e}")
            return True

    def search_and_extract(self, query: str, max_results: int = 50) -> List[Dict[str, Any]]:
        results = []
        page = 1
        seen_names = set()
        encoded = urllib.parse.quote_plus(query)

        while len(results) < max_results and page <= 5:
            if self.is_stopped():
                break

            # Search URL
            search_url = f"https://www.linkedin.com/search/results/all/?keywords={encoded}&page={page}"
            self.log(f"🌐 [LinkedIn Page {page}] Searching: {search_url}")
            self.driver.get(search_url)
            self.push_frame()
            time.sleep(3.5)

            # Scroll page to trigger lazy loaded items
            for _ in range(3):
                self.driver.execute_script("window.scrollBy(0, 700);")
                time.sleep(0.8)
            self.push_frame()

            items = self.driver.find_elements(
                By.CSS_SELECTOR,
                "li.reusable-search__result-container, div.entity-result, div[data-chameleon-result-urn]"
            )
            self.log(f"🎯 [LinkedIn Page {page}] Found {len(items)} entity result cards.")

            if not items:
                break

            for i, item in enumerate(items):
                if self.is_stopped() or len(results) >= max_results:
                    break

                try:
                    text = item.text.strip()
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    if not lines:
                        continue

                    # Extract Profile Link & Name
                    name = ""
                    profile_url = ""
                    try:
                        link_el = item.find_element(By.CSS_SELECTOR, "a.app-aware-link, a[href*='/in/'], a[href*='/company/']")
                        profile_url = link_el.get_attribute("href") or ""
                        name = link_el.text.strip()
                    except Exception:
                        pass

                    if not name and lines:
                        name = lines[0]

                    if not name or name in seen_names or "LinkedIn Member" in name:
                        continue
                    seen_names.add(name)

                    # Extract Headline / Role & Location
                    headline = ""
                    location = ""
                    try:
                        sub_el = item.find_element(By.CSS_SELECTOR, "div.entity-result__primary-subtitle, .entity-result__summary")
                        headline = sub_el.text.strip()
                    except Exception:
                        if len(lines) > 1:
                            headline = lines[1]

                    try:
                        loc_el = item.find_element(By.CSS_SELECTOR, "div.entity-result__secondary-subtitle")
                        location = loc_el.text.strip()
                    except Exception:
                        if len(lines) > 2:
                            location = lines[2]

                    # Extract phone / email if present in text
                    phone = self.extract_phone_numbers(text)
                    emails = self.extract_emails(text)
                    email = emails[0] if emails else ""

                    lead = self.normalize_lead(
                        name=name,
                        phone=phone,
                        email=email,
                        category=headline,
                        address=location,
                        profile_url=profile_url,
                        query=query
                    )
                    results.append(lead)

                    self.log(f"✅ [LinkedIn #{len(results)}] '{name}' | {headline[:35]} | 📍 {location[:20] if location else 'Global'}")
                    self.emit_progress(
                        len(results),
                        f"Extracted #{len(results)}: {name}",
                        {"name": name, "category": headline, "address": location}
                    )
                    self.push_frame()

                except Exception as ex:
                    continue

            page += 1
            time.sleep(1.5)

        return results
