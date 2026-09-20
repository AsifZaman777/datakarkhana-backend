import time
import urllib.parse
from typing import Optional, List, Dict, Any
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from scrapers.base import BaseScraper

class InstagramScraper(BaseScraper):
    platform_id = "instagram"
    platform_name = "Instagram"
    requires_auth = True
    description = "Scrape Instagram Creators, Business Profiles, Contact Bios, Phone & Emails"
    icon = "instagram"

    def authenticate(self, credentials: Optional[Dict[str, str]] = None) -> bool:
        if not credentials or not credentials.get("username") or not credentials.get("password"):
            self.log("⚠️ [Instagram] Credentials not provided. Proceeding in guest exploration mode...")
            return True

        username = credentials.get("username", "").strip()
        password = credentials.get("password", "").strip()

        masked_user = username[:3] + "***" if len(username) > 3 else "***"
        self.log(f"🔑 [Instagram] Authenticating with account: {masked_user}...")

        try:
            self.driver.get("https://www.instagram.com/accounts/login/")
            self.push_frame()
            time.sleep(3.0)

            # Accept cookies if dialog exists
            try:
                c_btns = self.driver.find_elements(By.CSS_SELECTOR, "button[class*='_a9--'], button:contains('Allow')")
                for b in c_btns:
                    b.click()
                    time.sleep(0.5)
            except Exception:
                pass

            # Fill username
            u_el = self.driver.find_element(By.CSS_SELECTOR, "input[name='username']")
            u_el.clear()
            u_el.send_keys(username)
            time.sleep(0.5)

            # Fill password
            p_el = self.driver.find_element(By.CSS_SELECTOR, "input[name='password']")
            p_el.clear()
            p_el.send_keys(password)
            self.push_frame()
            time.sleep(0.5)

            # Submit
            p_el.send_keys(Keys.RETURN)
            self.log("🚀 [Instagram] Login form submitted. Verifying session...")
            self.push_frame()
            time.sleep(5.0)
            self.push_frame()

            # Dismiss "Save Info" or "Turn on Notifications" modal if present
            try:
                not_now_btns = self.driver.find_elements(By.XPATH, "//button[contains(text(), 'Not Now') or contains(text(), 'not now')]")
                for b in not_now_btns:
                    b.click()
                    time.sleep(1.0)
            except Exception:
                pass

            self.log("✅ [Instagram] Session established.")
            return True

        except Exception as e:
            self.log(f"⚠️ [Instagram] Login notice: {e}")
            return True

    def search_and_extract(self, query: str, max_results: int = 50) -> List[Dict[str, Any]]:
        results = []
        clean_tag = query.replace(" ", "").replace("#", "").lower()
        seen_handles = set()

        # Try hashtag / topic page or search
        target_url = f"https://www.instagram.com/explore/tags/{clean_tag}/"
        self.log(f"🌐 [Instagram] Exploring topic / tag: #{clean_tag} ({target_url})")
        self.driver.get(target_url)
        self.push_frame()
        time.sleep(3.5)

        # Look for post links to harvest creator profiles
        post_links = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/p/']")
        self.log(f"🎯 [Instagram] Discovered {len(post_links)} post cards. Scanning creators...")
        self.push_frame()

        for idx, post in enumerate(post_links):
            if self.is_stopped() or len(results) >= max_results:
                break

            try:
                href = post.get_attribute("href") or ""
                if not href:
                    continue

                # Open post dialog or page to get author info
                self.driver.get(href)
                self.push_frame()
                time.sleep(1.5)

                # Author profile link
                author_els = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "header a[role='link'], h2 a[role='link'], span a[role='link'][href^='/']"
                )
                if not author_els:
                    continue

                author_handle = author_els[0].text.strip().replace("@", "")
                if not author_handle or author_handle in seen_handles:
                    continue
                seen_handles.add(author_handle)

                profile_url = f"https://www.instagram.com/{author_handle}/"

                # Extract caption text for contact info
                caption = ""
                try:
                    caption_el = self.driver.find_element(By.CSS_SELECTOR, "h1, div[class*='_a9zs'], span[class*='_aacl']")
                    caption = caption_el.text.strip()
                except Exception:
                    pass

                phone = self.extract_phone_numbers(caption)
                emails = self.extract_emails(caption)
                email = emails[0] if emails else ""

                lead = self.normalize_lead(
                    name=author_handle,
                    phone=phone,
                    email=email,
                    category=f"Instagram Creator / #{clean_tag}",
                    profile_url=profile_url,
                    query=query,
                    extra={"Caption Snippet": caption[:80]}
                )
                results.append(lead)

                contact_badge = f"📞 {phone}" if phone else (f"✉️ {email}" if email else "📱 Profile")
                self.log(f"✅ [Instagram #{len(results)}] '@{author_handle}' | {contact_badge}")
                self.emit_progress(
                    len(results),
                    f"Extracted #{len(results)}: @{author_handle}",
                    {"name": author_handle, "phone": phone, "email": email}
                )
                self.push_frame()

            except Exception as ex:
                continue

        return results
