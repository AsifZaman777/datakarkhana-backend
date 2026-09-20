import time
import urllib.parse
from typing import Optional, List, Dict, Any
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from scrapers.base import BaseScraper

class FacebookScraper(BaseScraper):
    platform_id = "facebook"
    platform_name = "Facebook"
    requires_auth = True
    description = "Scrape Facebook Business Pages, shops, contact numbers, emails, and social profiles"
    icon = "facebook"

    def authenticate(self, credentials: Optional[Dict[str, str]] = None) -> bool:
        if not credentials or not credentials.get("username") or not credentials.get("password"):
            self.log("⚠️ [Facebook] Credentials not supplied. Proceeding in public/guest mode...")
            return True

        username = credentials.get("username", "").strip()
        password = credentials.get("password", "").strip()

        masked_user = username[:3] + "***" if len(username) > 3 else "***"
        self.log(f"🔑 [Facebook] Authenticating with account: {masked_user}...")

        try:
            self.driver.get("https://www.facebook.com/login")
            self.push_frame()
            time.sleep(2.0)

            # Accept cookies if prompted
            try:
                cookie_btns = self.driver.find_elements(By.CSS_SELECTOR, "button[data-cookiebanner='accept_button'], button[title*='cookie'], button[title*='Accept']")
                for b in cookie_btns:
                    b.click()
                    time.sleep(0.5)
            except Exception:
                pass

            # Fill username
            user_input = None
            for sel in ["input#email", "input[name='email']", "input[type='email']", "input[type='text']"]:
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, sel)
                    if el.is_displayed():
                        user_input = el
                        break
                except Exception:
                    pass

            if not user_input:
                self.log("⚠️ [Facebook] Login input not found. Attempting home page...")
                self.driver.get("https://www.facebook.com/")
                time.sleep(2.0)
                user_input = self.driver.find_element(By.CSS_SELECTOR, "input#email, input[name='email']")

            user_input.clear()
            user_input.send_keys(username)
            time.sleep(0.5)

            # Fill password
            pass_input = self.driver.find_element(By.CSS_SELECTOR, "input#pass, input[name='pass'], input[type='password']")
            pass_input.clear()
            pass_input.send_keys(password)
            self.push_frame()
            time.sleep(0.5)

            # Submit
            pass_input.send_keys(Keys.RETURN)
            self.log("🚀 [Facebook] Login form submitted. Verifying session...")
            self.push_frame()
            time.sleep(4.0)
            self.push_frame()

            # Check if login succeeded
            current_url = self.driver.current_url
            if "checkpoint" in current_url or "two_factor" in current_url:
                self.log("⚠️ [Facebook Security Notice] Facebook prompted for two-factor approval / checkpoint verification. Check the live terminal frame.")
                return True
            elif "login" in current_url and ("error" in current_url or "login_attempt" in current_url):
                self.log("❌ [Facebook] Authentication failed. Check your username and password.")
                return False

            self.log("✅ [Facebook] Authentication session established successfully.")
            return True

        except Exception as e:
            self.log(f"⚠️ [Facebook] Login encountered notice / exception: {e}")
            return True

    def search_and_extract(self, query: str, max_results: int = 50) -> List[Dict[str, Any]]:
        results = []
        encoded = urllib.parse.quote_plus(query)
        
        # Target Facebook Pages search directly
        search_url = f"https://www.facebook.com/search/pages/?q={encoded}"
        self.log(f"🌐 [Facebook Search] Searching Business Pages for: '{query}' ({search_url})")
        self.driver.get(search_url)
        self.push_frame()
        time.sleep(3.5)

        seen_names = set()
        scroll_attempts = 0
        max_scrolls = 10

        while len(results) < max_results and scroll_attempts < max_scrolls:
            if self.is_stopped():
                break

            # Find result items (Page cards)
            items = self.driver.find_elements(
                By.CSS_SELECTOR,
                "div[role='feed'] > div, div[role='article'], div[data-visualcompletion='ignore-dynamic-snippet'], div.x1yztbdb"
            )
            if not items:
                items = self.driver.find_elements(By.CSS_SELECTOR, "div.x9f619.x1n2onr6.x1ja2u2z")

            self.log(f"📜 [Facebook] Feed scan: found {len(items)} potential page containers (Collected {len(results)}/{max_results})...")
            self.push_frame()

            for item in items:
                if self.is_stopped() or len(results) >= max_results:
                    break

                try:
                    text = item.text.strip()
                    if not text or len(text) < 10:
                        continue

                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    name = lines[0] if lines else ""

                    # Filter out common UI buttons / headers
                    if not name or name in seen_names or any(name.lower().startswith(x) for x in ["all", "posts", "people", "photos", "videos", "pages", "filters"]):
                        continue

                    # Extract link
                    page_url = ""
                    try:
                        links = item.find_elements(By.CSS_SELECTOR, "a[href*='facebook.com'], a[role='link']")
                        for a in links:
                            h = a.get_attribute("href") or ""
                            if "/search/" not in h and "facebook.com" in h:
                                page_url = h
                                break
                    except Exception:
                        pass

                    seen_names.add(name)

                    # Extract phone number & email
                    phone = self.extract_phone_numbers(text)
                    emails = self.extract_emails(text)
                    email = emails[0] if emails else ""

                    # Extract category or subtext
                    category = lines[1] if len(lines) > 1 and not lines[1].startswith("+") else ""

                    lead = self.normalize_lead(
                        name=name,
                        phone=phone,
                        email=email,
                        category=category,
                        profile_url=page_url,
                        query=query
                    )
                    results.append(lead)

                    phone_badge = f"📞 {phone}" if phone else "⚠️ No phone"
                    email_badge = f"| ✉️ {email}" if email else ""
                    self.log(f"✅ [Facebook #{len(results)}] '{name}' | {phone_badge} {email_badge}")
                    self.emit_progress(
                        len(results),
                        f"Extracted #{len(results)}: {name}",
                        {"name": name, "phone": phone, "email": email, "category": category}
                    )
                    self.push_frame()

                except Exception as ex:
                    continue

            # Scroll down to load more results
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            self.push_frame()
            time.sleep(2.0)
            scroll_attempts += 1

        return results
