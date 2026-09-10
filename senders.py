import os
import time
import random
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from datetime import datetime
from database import get_db

# Log files directory
LOGS_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOGS_FOLDER, exist_ok=True)

# Screenshot directory for live map view
SCREENSHOTS_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrape_results", "screenshots")
os.makedirs(SCREENSHOTS_FOLDER, exist_ok=True)

def write_log_to_file(campaign_id, campaign_type, message):
    """Write campaign log entry to day-wise log file"""
    today = datetime.now().strftime("%Y-%m-%d")
    log_filename = f"{today}_campaigns.log"
    log_path = os.path.join(LOGS_FOLDER, log_filename)
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_line = f"[{timestamp}] [{campaign_id}] [{campaign_type.upper()}] {message}\n"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_line)
    except Exception as e:
        print(f"Log file write error: {e}")

def format_phone(phone):
    """Clean and format phone number for WhatsApp URL API"""
    p_str = str(phone).strip()
    if p_str.endswith(".0"):
        p_str = p_str[:-2]
    cleaned = "".join(filter(str.isdigit, p_str))
    if cleaned.startswith("88001"):
        cleaned = "8801" + cleaned[5:]
    elif cleaned.startswith("01"):
        cleaned = "88" + cleaned
    elif cleaned.startswith("1") and len(cleaned) == 10:
        cleaned = "880" + cleaned
    return cleaned

_active_setup_driver = None

def get_active_setup_driver():
    global _active_setup_driver
    return _active_setup_driver

def set_active_setup_driver(driver):
    global _active_setup_driver
    _active_setup_driver = driver

def close_active_setup_driver():
    global _active_setup_driver
    if _active_setup_driver:
        try:
            _active_setup_driver.quit()
        except Exception:
            pass
        _active_setup_driver = None

_active_campaign_drivers = {}
_stopped_campaign_ids = set()

def register_campaign_driver(campaign_id: str, driver):
    _active_campaign_drivers[str(campaign_id)] = driver

def unregister_campaign_driver(campaign_id: str):
    _active_campaign_drivers.pop(str(campaign_id), None)

def is_campaign_stopped(campaign_id: str) -> bool:
    return str(campaign_id) in _stopped_campaign_ids

def force_stop_campaign(campaign_id: str):
    """Immediately stops campaign: closes browser and marks campaign stopped in memory"""
    cid = str(campaign_id)
    _stopped_campaign_ids.add(cid)
    driver = _active_campaign_drivers.pop(cid, None)
    if driver:
        try:
            driver.quit()
        except Exception:
            pass

def cleanup_profile_locks(profile_path):
    """Remove Chrome Singleton locks that cause Chrome to drop persistent profile sessions"""
    lock_files = [
        "SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort", "LOCK",
        os.path.join("Default", "LOCK"),
        os.path.join("Default", "WebStorage", "QuotaManager-journal")
    ]
    for lock in lock_files:
        lock_p = os.path.join(profile_path, lock)
        if os.path.exists(lock_p) or os.path.islink(lock_p):
            try:
                os.unlink(lock_p)
            except Exception:
                pass

def fix_chrome_preferences(profile_path):
    """Ensure Chrome exit_type is Normal and exited_cleanly is true to preserve IndexedDB & cookies session"""
    pref_path = os.path.join(profile_path, "Default", "Preferences")
    if os.path.exists(pref_path):
        try:
            import json
            with open(pref_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                modified = False
                if "profile" in data and isinstance(data["profile"], dict):
                    data["profile"]["exit_type"] = "Normal"
                    data["profile"]["exited_cleanly"] = True
                    modified = True
                if modified:
                    with open(pref_path, "w", encoding="utf-8") as f:
                        json.dump(data, f)
        except Exception:
            pass

def setup_driver():
    """Setup Chrome options and persistence profile directory"""
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--window-size=1400,900")
    options.add_argument("--disable-session-crashed-bubble")
    options.add_argument("--disable-infobars")
    options.add_argument("--restore-last-session")

    profile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whatsapp_session")
    os.makedirs(profile_path, exist_ok=True)
    cleanup_profile_locks(profile_path)
    fix_chrome_preferences(profile_path)

    options.add_argument(f"--user-data-dir={profile_path}")
    options.add_argument("--profile-directory=Default")

    driver = webdriver.Chrome(options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver

def wait_for_whatsapp_login(driver):
    """Wait for scan session verification and open active WhatsApp chats"""
    if "web.whatsapp.com" not in driver.current_url:
        driver.get("https://web.whatsapp.com")
    start_time = time.time()
    while time.time() - start_time < 180:
        try:
            indicators = driver.find_elements(By.XPATH, '//div[@id="side"] | //div[@data-testid="chat-list"] | //div[@id="pane-side"] | //header')
            if indicators and len(indicators) > 0:
                time.sleep(2)
                return True
        except Exception:
            pass
        time.sleep(1)
    return False

def save_campaign_screenshot(driver, campaign_id):
    """Save a screenshot of the current browser state for live view"""
    try:
        screenshot_path = os.path.join(SCREENSHOTS_FOLDER, f"campaign_{campaign_id}.png")
        driver.save_screenshot(screenshot_path)
    except Exception:
        pass

def type_text_safely(driver, element, text, wpm=40):
    """Calibrated Humanized typing simulator at exact WPM (default 40 WPM)"""
    sec_per_word = 60.0 / float(wpm)  # 40 WPM = 1.5 seconds per word
    lines = text.split("\n")
    for l_idx, line in enumerate(lines):
        if l_idx > 0:
            try:
                ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT).perform()
            except Exception:
                pass
            time.sleep(random.uniform(0.8, 1.4))

        words = line.split(" ")
        for w_idx, word in enumerate(words):
            word_to_type = word + (" " if w_idx < len(words) - 1 else "")
            
            try:
                driver.execute_script(
                    """
                    var el = arguments[0];
                    var txt = arguments[1];
                    el.focus();
                    document.execCommand('insertText', false, txt);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    """,
                    element,
                    word_to_type
                )
            except Exception:
                bmp_word = "".join(c for c in word_to_type if ord(c) <= 0xFFFF)
                element.send_keys(bmp_word)

            # Calibrated word delay centered around 40 WPM (~1.5s per word)
            word_len = max(len(word), 1)
            word_delay = random.uniform(sec_per_word * 0.7, sec_per_word * 1.3) * (word_len / 5.0)
            word_delay = max(0.6, min(2.5, word_delay))
            time.sleep(word_delay)

            # Natural pause at punctuation & Bangla Dari (।)
            if word and word[-1] in ('.', ',', '!', '?', ':', '।'):
                time.sleep(random.uniform(0.5, 1.2))

def check_and_dismiss_invalid_modal(driver):
    """
    Rapidly checks if WhatsApp Web has displayed an invalid number / alert dialog.
    If found, automatically clicks the OK / Dismiss button and returns True.
    Executes directly in the browser via JavaScript and ActionChains so no human intervention is needed.
    """
    js_dismiss = """
    try {
        function triggerClick(el) {
            if (!el) return;
            try { el.focus(); } catch(e) {}
            ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function(evt) {
                try {
                    el.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                } catch(e) {}
            });
            try { el.click(); } catch(e) {}
        }

        // 1. Check for explicit dialog containers (WhatsApp Web alert modals)
        const dialogSelectors = [
            'div[role="dialog"]',
            'div[data-animate-modal-popup="true"]',
            'div[data-testid="confirm-popup"]',
            'div[data-testid="popup-contents"]',
            'div[class*="popup"]',
            'div[class*="modal"]'
        ];
        
        for (const sel of dialogSelectors) {
            const dialogs = document.querySelectorAll(sel);
            for (const dialog of dialogs) {
                // Find any clickable button in this dialog
                const buttons = dialog.querySelectorAll('button, div[role="button"], [data-testid="popup-controls-ok"]');
                if (buttons.length > 0) {
                    for (const btn of buttons) {
                        triggerClick(btn);
                    }
                    return { dismissed: true, source: 'dialog_button' };
                }
            }
        }

        // 2. Look for any visible button with "OK", "Ok", "Okay", or "ঠিক আছে"
        const allButtons = document.querySelectorAll('button, div[role="button"]');
        for (const btn of allButtons) {
            const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
            if (txt === 'ok' || txt === 'okay' || txt === 'ঠিক আছে' || txt === 'dismiss' || txt === 'close') {
                const rect = btn.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    triggerClick(btn);
                    return { dismissed: true, source: 'ok_text_button' };
                }
            }
        }

        // 3. Check page text indicators for invalid number modal
        const bodyText = (document.body ? (document.body.innerText || document.body.textContent || '') : '').toLowerCase();
        if (
            bodyText.includes('phone number shared via url is invalid') ||
            bodyText.includes("isn't on whatsapp") ||
            bodyText.includes('not on whatsapp') ||
            bodyText.includes('url is invalid') ||
            bodyText.includes("couldn't find this phone number") ||
            bodyText.includes('phone number is invalid')
        ) {
            if (document.activeElement && typeof document.activeElement.click === 'function') {
                triggerClick(document.activeElement);
            }
            return { dismissed: true, source: 'page_text_indicator' };
        }

        return { dismissed: false };
    } catch(err) {
        return { dismissed: false, error: err.toString() };
    }
    """
    try:
        res = driver.execute_script(js_dismiss)
        if isinstance(res, dict) and res.get("dismissed"):
            # Also dispatch keyboard Enter & Escape as reinforcement
            try:
                ActionChains(driver).send_keys(Keys.ENTER).perform()
            except Exception:
                pass
            try:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            except Exception:
                pass
            time.sleep(0.3)
            return True
    except Exception:
        pass

    # Native alert dialog fallback
    try:
        alert = driver.switch_to.alert
        alert.accept()
        time.sleep(0.3)
        return True
    except Exception:
        pass

    return False

def dismiss_whatsapp_invalid_modal(driver):
    """Wrapper maintaining compatibility with existing calls"""
    return check_and_dismiss_invalid_modal(driver)

def run_whatsapp_campaign(campaign_id, contacts, template_text, recipient_group, start_index=0):
    """Core WhatsApp human-emulating thread dispatch manager"""
    
    failed_count = 0

    def log_status(msg):
        try:
            conn = get_db()
            conn.execute("INSERT INTO campaign_logs (campaign_id, message) VALUES (?, ?)", (campaign_id, msg))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Log trace write error: {e}")
        # Also write to day-wise log file
        write_log_to_file(campaign_id, "whatsapp", msg)

    def update_failed():
        nonlocal failed_count
        failed_count += 1
        try:
            conn = get_db()
            conn.execute("UPDATE marketing_campaigns SET failed_count = ? WHERE id = ?", (failed_count, campaign_id))
            conn.commit()
            conn.close()
        except Exception:
            pass

    log_status(f"Campaign started. Preparing Selenium WhatsApp session...")
    log_status(f"Starting from contact index: {start_index} | Total contacts: {len(contacts)}")

    close_active_setup_driver()
    driver = None
    try:
        driver = setup_driver()
        register_campaign_driver(campaign_id, driver)
        if not wait_for_whatsapp_login(driver):
            log_status("WhatsApp Web login verification timed out or aborted.")
            conn = get_db()
            conn.execute("UPDATE marketing_campaigns SET status = 'failed' WHERE id = ?", (campaign_id,))
            conn.commit()
            conn.close()
            driver.quit()
            return

        log_status("Successfully logged in! Commencing lead group dispatch...")

        target_contacts = contacts[start_index:]
        for idx, contact in enumerate(target_contacts):
            current_index = start_index + idx

            # Check if campaign stopped
            if is_campaign_stopped(campaign_id):
                log_status(f"Campaign stopped by user immediately. Paused at lead index: {current_index}.")
                conn = get_db()
                conn.execute("UPDATE marketing_campaigns SET status = 'stopped' WHERE id = ?", (campaign_id,))
                conn.commit()
                conn.close()
                return

            conn = get_db()
            row = conn.execute("SELECT status FROM marketing_campaigns WHERE id = ?", (campaign_id,)).fetchone()
            conn.close()
            if row and row["status"] in ("stopping", "stopped"):
                log_status(f"Campaign stopped by user. Paused at lead index: {current_index}.")
                conn = get_db()
                conn.execute("UPDATE marketing_campaigns SET status = 'stopped' WHERE id = ?", (campaign_id,))
                conn.commit()
                conn.close()
                return

            phone = contact["phone"]
            name = contact["name"]
            formatted = format_phone(phone)
            personalized_msg = template_text.replace("{name}", name)

            # Cool down delay logic (Every 8 messages, sleep 2 min)
            if idx > 0 and idx % 8 == 0:
                log_status("Entering 2-minute cooldown to prevent accounts bans...")
                for _ in range(24):
                    if is_campaign_stopped(campaign_id):
                        log_status(f"Campaign stopped during cooldown. Paused at lead index: {current_index}.")
                        conn = get_db()
                        conn.execute("UPDATE marketing_campaigns SET status = 'stopped' WHERE id = ?", (campaign_id,))
                        conn.commit()
                        conn.close()
                        driver.quit()
                        return
                    conn = get_db()
                    chk = conn.execute("SELECT status FROM marketing_campaigns WHERE id = ?", (campaign_id,)).fetchone()
                    conn.close()
                    if chk and chk["status"] in ("stopping", "stopped"):
                        log_status(f"Campaign stopped during cooldown. Paused at lead index: {current_index}.")
                        conn = get_db()
                        conn.execute("UPDATE marketing_campaigns SET status = 'stopped' WHERE id = ?", (campaign_id,))
                        conn.commit()
                        conn.close()
                        driver.quit()
                        return
                    time.sleep(5)

            log_status(f"[{current_index + 1}/{len(contacts)}] Opening chat for {name} ({formatted})...")
            url = f"https://web.whatsapp.com/send?phone={formatted}"
            try:
                driver.execute_script("window.location.href = arguments[0];", url)
            except Exception:
                driver.get(url)

            # Save screenshot for live map view
            time.sleep(2)
            save_campaign_screenshot(driver, campaign_id)

            # Wait for text input panel or invalid number modal
            start_load = time.time()
            chat_ready = False
            invalid_num = False
            textbox = None

            textbox_selectors = [
                '//footer//div[@contenteditable="true"]',
                '//footer//div[@role="textbox"]',
                '//div[@data-tab="10"]',
                '//div[contains(@aria-placeholder, "Type a message")]',
                '//footer//p',
                '//div[@contenteditable="true"]'
            ]

            while time.time() - start_load < 30:
                # Early stop check during page load
                if is_campaign_stopped(campaign_id):
                    log_status(f"Campaign stopped by user immediately. Paused at lead index: {current_index}.")
                    conn = get_db()
                    conn.execute("UPDATE marketing_campaigns SET status = 'stopped' WHERE id = ?", (campaign_id,))
                    conn.commit()
                    conn.close()
                    driver.quit()
                    return

                conn = get_db()
                chk = conn.execute("SELECT status FROM marketing_campaigns WHERE id = ?", (campaign_id,)).fetchone()
                conn.close()
                if chk and chk["status"] in ("stopping", "stopped"):
                    log_status(f"Campaign stopped by user. Paused at lead index: {current_index}.")
                    conn = get_db()
                    conn.execute("UPDATE marketing_campaigns SET status = 'stopped' WHERE id = ?", (campaign_id,))
                    conn.commit()
                    conn.close()
                    driver.quit()
                    return

                # 1. Immediately detect and auto-dismiss invalid number / OK modal
                if check_and_dismiss_invalid_modal(driver):
                    invalid_num = True
                    break

                # 2. Check if chat textbox is ready
                try:
                    for sel in textbox_selectors:
                        inputs = driver.find_elements(By.XPATH, sel)
                        if inputs and len(inputs) > 0:
                            textbox = inputs[-1]
                            chat_ready = True
                            break
                    if chat_ready:
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            if invalid_num:
                # Dismiss again to guarantee overlay is fully closed
                check_and_dismiss_invalid_modal(driver)
                log_status(f"⚠️ Skipped {name} ({formatted}): Number is not on WhatsApp. Closed 'OK' modal automatically.")
                update_failed()
                conn = get_db()
                conn.execute("INSERT OR REPLACE INTO whatsapp_progress (recipient_group, last_index, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (recipient_group, current_index + 1))
                conn.execute("UPDATE marketing_campaigns SET sent_count = ? WHERE id = ?", (current_index + 1, campaign_id))
                conn.commit()
                conn.close()
                time.sleep(0.8)
                continue

            if not chat_ready or not textbox:
                # Check one more time if invalid number modal appeared
                if check_and_dismiss_invalid_modal(driver):
                    log_status(f"⚠️ Skipped {name} ({formatted}): Number is not on WhatsApp. Closed 'OK' modal automatically.")
                else:
                    check_and_dismiss_invalid_modal(driver)
                    log_status(f"⚠️ Could not load chat element for {name} ({formatted}). Closed any prompt & skipping...")
                update_failed()
                conn = get_db()
                conn.execute("INSERT OR REPLACE INTO whatsapp_progress (recipient_group, last_index, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (recipient_group, current_index + 1))
                conn.execute("UPDATE marketing_campaigns SET sent_count = ? WHERE id = ?", (current_index + 1, campaign_id))
                conn.commit()
                conn.close()
                time.sleep(0.8)
                continue

            try:
                # Focus textbox
                try:
                    driver.execute_script("arguments[0].focus();", textbox)
                except Exception:
                    pass
                textbox.click()
                time.sleep(0.5)

                log_status(f"Typing message to {name}...")
                type_text_safely(driver, textbox, personalized_msg)
                time.sleep(1.2)
                
                # Try clicking WhatsApp Send icon button first
                send_clicked = False
                try:
                    send_btn = driver.find_element(By.XPATH, '//button[@data-tab="11"] | //button[span[@data-icon="send"]] | //span[@data-icon="send"]/parent::button | //button[contains(@aria-label, "Send")]')
                    send_btn.click()
                    send_clicked = True
                except Exception:
                    pass

                if not send_clicked:
                    textbox.send_keys(Keys.ENTER)

                time.sleep(1.5)

                # Save screenshot after sending
                save_campaign_screenshot(driver, campaign_id)

                # Update SQLite database progress
                conn = get_db()
                conn.execute("INSERT OR REPLACE INTO whatsapp_progress (recipient_group, last_index, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (recipient_group, current_index + 1))
                conn.execute("UPDATE marketing_campaigns SET sent_count = ? WHERE id = ?", (current_index + 1, campaign_id))
                conn.commit()
                conn.close()

                log_status(f"✅ Message dispatched to {name} successfully! [{current_index + 1}/{len(contacts)}]")

                # Settle down delay
                delay = random.randint(10, 25)
                for _ in range(delay // 2):
                    if is_campaign_stopped(campaign_id):
                        log_status(f"Campaign stopped. Paused at lead index: {current_index + 1}.")
                        conn = get_db()
                        conn.execute("UPDATE marketing_campaigns SET status = 'stopped' WHERE id = ?", (campaign_id,))
                        conn.commit()
                        conn.close()
                        driver.quit()
                        return
                    conn = get_db()
                    chk = conn.execute("SELECT status FROM marketing_campaigns WHERE id = ?", (campaign_id,)).fetchone()
                    conn.close()
                    if chk and chk["status"] in ("stopping", "stopped"):
                        log_status(f"Campaign stopped. Paused at lead index: {current_index + 1}.")
                        conn = get_db()
                        conn.execute("UPDATE marketing_campaigns SET status = 'stopped' WHERE id = ?", (campaign_id,))
                        conn.commit()
                        conn.close()
                        driver.quit()
                        return
                    time.sleep(2)

            except Exception as ex:
                if is_campaign_stopped(campaign_id):
                    return
                log_status(f"❌ Typing process failed for {name}: {ex}")
                update_failed()

        driver.quit()
        log_status(f"🎉 WhatsApp campaign completed! Sent: {len(contacts) - failed_count}, Failed: {failed_count}")
        conn = get_db()
        conn.execute("UPDATE marketing_campaigns SET status = 'done' WHERE id = ?", (campaign_id,))
        conn.commit()
        conn.close()

    except Exception as ex:
        if is_campaign_stopped(campaign_id):
            log_status("Campaign thread terminated cleanly by user stop request.")
            try:
                conn = get_db()
                conn.execute("UPDATE marketing_campaigns SET status = 'stopped' WHERE id = ?", (campaign_id,))
                conn.commit()
                conn.close()
            except Exception:
                pass
        else:
            log_status(f"Fatal Dispatcher Thread Exception: {ex}")
            try:
                conn = get_db()
                conn.execute("UPDATE marketing_campaigns SET status = 'failed' WHERE id = ?", (campaign_id,))
                conn.commit()
                conn.close()
            except Exception:
                pass
    finally:
        unregister_campaign_driver(campaign_id)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

