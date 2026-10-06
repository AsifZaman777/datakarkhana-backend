import asyncio
import logging
import uuid
import time
import random
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional
from database import get_db

logger = logging.getLogger("stock_alert_service")
logger.setLevel(logging.INFO)

BST = timezone(timedelta(hours=6))


class StockAlertService:
    _instance: Optional["StockAlertService"] = None

    def __init__(self):
        # In-memory cooldown cache: alert_id -> last_triggered_timestamp
        self._cooldown_cache: Dict[str, float] = {}

    @classmethod
    def get_instance(cls) -> "StockAlertService":
        if cls._instance is None:
            cls._instance = StockAlertService()
        return cls._instance

    @staticmethod
    def normalize_phone(phone: str) -> str:
        """Sanitize phone number into international E.164-compatible format (e.g. 88017xxxxxxxx)"""
        if not phone:
            return ""
        digits = re.sub(r"\D", "", str(phone).strip())
        if digits.startswith("01"):
            digits = "88" + digits
        elif digits.startswith("1") and len(digits) == 10:
            digits = "880" + digits
        return digits

    def is_phone_verified(self, phone: str) -> bool:
        """Check whether this phone number has an active verified session (valid for 1 week)"""
        norm = self.normalize_phone(phone)
        if not norm:
            return False
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT phone_number 
                FROM verified_phone_numbers 
                WHERE phone_number = %s 
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP);
            """, (norm,))
            row = cursor.fetchone()
            conn.close()
            return row is not None
        except Exception as e:
            logger.error(f"[is_phone_verified error] {e}")
    def dispose_phone_session(self, phone: str, clear_credentials: bool = False) -> bool:
        """Dispose of the verified session, clearing verification records and rate limit blocks"""
        norm = self.normalize_phone(phone)
        if not norm:
            return False
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM verified_phone_numbers WHERE phone_number = %s;", (norm,))
            cursor.execute("DELETE FROM phone_verification_otps WHERE phone_number = %s;", (norm,))
            if clear_credentials:
                cursor.execute("DELETE FROM user_green_api_configs WHERE phone_number = %s;", (norm,))
            conn.commit()
            conn.close()
            logger.info(f"[SESSION DISPOSED] WhatsApp session disposed for {norm}")
            return True
        except Exception as e:
            logger.error(f"[dispose_phone_session error] {e}")
            return False

    def get_phone_verification_info(self, phone: str) -> Dict[str, Any]:
        """Get detailed verification and rate-limit status for a phone number (1-week session)"""
        norm = self.normalize_phone(phone)
        if not norm:
            return {"is_verified": False, "phone": phone}

        now = time.time()
        info = {
            "phone": norm,
            "is_verified": False,
            "expires_at": None,
            "remaining_seconds": 0,
            "is_rate_limited": False,
            "rate_limited_until": 0,
            "failed_attempts": 0
        }
        try:
            conn = get_db()
            cursor = conn.cursor()
            # 1. Check verified_phone_numbers
            cursor.execute("""
                SELECT phone_number, verified_at, expires_at 
                FROM verified_phone_numbers 
                WHERE phone_number = %s;
            """, (norm,))
            vrow = cursor.fetchone()
            if vrow:
                exp = vrow[2] if isinstance(vrow, (list, tuple)) else vrow.get("expires_at")
                if exp:
                    if isinstance(exp, str):
                        try:
                            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                        except Exception:
                            exp_dt = None
                    elif isinstance(exp, datetime):
                        exp_dt = exp
                    else:
                        exp_dt = None

                    if exp_dt:
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                        now_utc = datetime.now(timezone.utc)
                        if exp_dt > now_utc:
                            info["is_verified"] = True
                            info["expires_at"] = exp_dt.astimezone(BST).strftime("%Y-%m-%d %I:%M %p BST")
                            info["remaining_seconds"] = int((exp_dt - now_utc).total_seconds())
                    else:
                        info["is_verified"] = True
                else:
                    info["is_verified"] = True

            # 2. Check rate limit in phone_verification_otps
            cursor.execute("""
                SELECT failed_count, blocked_until 
                FROM phone_verification_otps 
                WHERE phone_number = %s;
            """, (norm,))
            orow = cursor.fetchone()
            if orow:
                fc = int(orow[0] or 0)
                bu = float(orow[1] or 0.0)
                info["failed_attempts"] = fc
                if bu > now:
                    info["is_rate_limited"] = True
                    info["rate_limited_until"] = bu
                    info["rate_limit_remaining_seconds"] = int(bu - now)

            conn.close()
        except Exception as e:
            logger.error(f"[get_phone_verification_info error] {e}")

        return info

    async def send_verification_otp(self, phone: str) -> Dict[str, Any]:
        """Generate a 6-digit OTP, store in database, apply 7-failed/1-day rate limit, and dispatch to WhatsApp"""
        norm = self.normalize_phone(phone)
        if not norm or len(norm) < 11:
            return {"success": False, "message": "Please enter a valid WhatsApp phone number (e.g. 017xxxxxxxx)."}

        now = time.time()
        try:
            conn = get_db()
            cursor = conn.cursor()

            # Rate Limit check
            cursor.execute("""
                SELECT failed_count, blocked_until 
                FROM phone_verification_otps 
                WHERE phone_number = %s;
            """, (norm,))
            row = cursor.fetchone()
            failed_count = int(row[0] or 0) if row else 0
            blocked_until = float(row[1] or 0.0) if row else 0.0

            # If currently blocked (1-day rate limit active)
            if blocked_until > now:
                diff_sec = int(blocked_until - now)
                hours = max(1, (diff_sec + 3599) // 3600)
                conn.close()
                return {
                    "success": False,
                    "message": f"Too many failed attempts. This phone number is rate-limited for 24 hours to prevent spam. Please try again after {hours} hour(s).",
                    "is_rate_limited": True,
                    "retry_after_seconds": diff_sec
                }

            # If failed_count reached 7, activate 24-hour block
            if failed_count >= 7:
                blocked_until = now + 86400.0  # 24 hours (1 day)
                cursor.execute("""
                    UPDATE phone_verification_otps 
                    SET blocked_until = %s 
                    WHERE phone_number = %s;
                """, (blocked_until, norm))
                conn.commit()
                conn.close()
                return {
                    "success": False,
                    "message": "Too many failed attempts (7/7). This phone number is now rate-limited for 24 hours to prevent spam.",
                    "is_rate_limited": True,
                    "retry_after_seconds": 86400
                }

            otp_code = f"{random.randint(100000, 999999)}"
            expires_at = now + 600.0  # 10 minutes

            # Upsert into phone_verification_otps preserving failed_count
            cursor.execute("""
                INSERT INTO phone_verification_otps (phone_number, otp_code, expires_at, attempts, failed_count, blocked_until)
                VALUES (%s, %s, %s, 0, %s, %s)
                ON CONFLICT (phone_number) DO UPDATE
                SET otp_code = EXCLUDED.otp_code, 
                    expires_at = EXCLUDED.expires_at, 
                    attempts = 0, 
                    created_at = CURRENT_TIMESTAMP;
            """, (norm, otp_code, expires_at, failed_count, blocked_until))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[send_verification_otp db error] {e}")
            return {"success": False, "message": f"Database error storing OTP: {e}"}

        # Format message for WhatsApp inbox delivery
        otp_message = (
            "🤖 *[DataKarkhana AI Stock Bot]*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 *WhatsApp Phone Verification*\n\n"
            f"Your one-time security OTP code is:\n"
            f"👉  *{otp_code}*  👈\n\n"
            "Enter this 6-digit code in DataKarkhana to verify this phone number.\n"
            "Once verified, your WhatsApp session will remain active for 1 full week (7 days) to receive:\n"
            "• Real-time stock target price alerts\n"
            "• Circuit breaker & intra-day spike notifications\n"
            "• Corporate announcements (PSI), earnings & dividends\n"
            "• Interactive live quotes & bot commands right in your chat!\n\n"
            "⏱️ *Code expires in 10 minutes.*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ DataKarkhana Live Trading Intelligence"
        )

        logger.info(f"[OTP GENERATED] Sent OTP {otp_code} to {norm}")
        asyncio.create_task(self._dispatch_whatsapp(norm, otp_message))
        return {
            "success": True,
            "message": f"Verification code dispatched to {norm} via WhatsApp.",
            "phone": norm,
            "failed_attempts": failed_count,
            "max_attempts": 7
        }

    async def verify_phone_otp(self, phone: str, otp: str) -> Dict[str, Any]:
        """Verify the user-provided OTP code against the database record and grant 1-week session"""
        norm = self.normalize_phone(phone)
        input_code = str(otp).strip()

        if not norm or not input_code:
            return {"success": False, "message": "Phone number and OTP code are required."}

        now = time.time()
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT otp_code, expires_at, attempts, failed_count, blocked_until 
                FROM phone_verification_otps 
                WHERE phone_number = %s;
            """, (norm,))
            row = cursor.fetchone()

            if not row:
                conn.close()
                return {"success": False, "message": "No verification request found for this phone number. Please request a new OTP."}

            stored_code = str(row[0] if isinstance(row, (list, tuple)) else row["otp_code"])
            expires_at = float(row[1] if isinstance(row, (list, tuple)) else row["expires_at"])
            attempts = int(row[2] if isinstance(row, (list, tuple)) else row["attempts"])
            failed_count = int(row[3] if isinstance(row, (list, tuple)) else (row.get("failed_count") or 0))
            blocked_until = float(row[4] if isinstance(row, (list, tuple)) else (row.get("blocked_until") or 0.0))

            # 1. Check if number is blocked for 1 day
            if blocked_until > now:
                diff_sec = int(blocked_until - now)
                hours = max(1, (diff_sec + 3599) // 3600)
                conn.close()
                return {
                    "success": False,
                    "message": f"Too many failed attempts. This phone number is rate-limited for 24 hours to prevent spam. Please try again after {hours} hour(s).",
                    "is_rate_limited": True,
                    "retry_after_seconds": diff_sec
                }

            # 2. Check if OTP is expired
            if now > expires_at:
                new_failed = failed_count + 1
                new_blocked = (now + 86400.0) if new_failed >= 7 else 0.0
                cursor.execute("""
                    UPDATE phone_verification_otps 
                    SET failed_count = %s, blocked_until = %s 
                    WHERE phone_number = %s;
                """, (new_failed, new_blocked, norm))
                conn.commit()
                conn.close()
                if new_failed >= 7:
                    return {
                        "success": False,
                        "message": "This verification code has expired and maximum failed attempts (7/7) reached. This number is rate-limited for 24 hours.",
                        "is_rate_limited": True,
                        "failed_attempts": 7,
                        "max_attempts": 7
                    }
                return {
                    "success": False,
                    "message": f"This verification code has expired. (Failed attempt {new_failed} of 7). Please request a new OTP.",
                    "failed_attempts": new_failed,
                    "max_attempts": 7
                }

            # 3. Check if OTP does not match
            if stored_code != input_code:
                new_failed = failed_count + 1
                new_blocked = (now + 86400.0) if new_failed >= 7 else 0.0
                cursor.execute("""
                    UPDATE phone_verification_otps 
                    SET attempts = attempts + 1, failed_count = %s, blocked_until = %s 
                    WHERE phone_number = %s;
                """, (new_failed, new_blocked, norm))
                conn.commit()
                conn.close()
                if new_failed >= 7:
                    return {
                        "success": False,
                        "message": "Incorrect verification code. Maximum failed attempts (7/7) reached! This phone number is now rate-limited for 24 hours to prevent spam.",
                        "is_rate_limited": True,
                        "failed_attempts": 7,
                        "max_attempts": 7
                    }
                return {
                    "success": False,
                    "message": f"Incorrect verification code. (Attempt {new_failed} of 7). Please check and try again.",
                    "failed_attempts": new_failed,
                    "max_attempts": 7
                }

            # 4. OTP MATCHED! Verification successful!
            # Clean up OTP record
            cursor.execute("DELETE FROM phone_verification_otps WHERE phone_number = %s;", (norm,))

            # 1-week session expiry (7 days from now)
            expires_at_dt = datetime.now(timezone.utc) + timedelta(days=7)
            cursor.execute("""
                INSERT INTO verified_phone_numbers (phone_number, verified_at, expires_at)
                VALUES (%s, CURRENT_TIMESTAMP, %s)
                ON CONFLICT (phone_number) DO UPDATE 
                SET verified_at = CURRENT_TIMESTAMP,
                    expires_at = EXCLUDED.expires_at;
            """, (norm, expires_at_dt))
            conn.commit()
            conn.close()

            week_expiry_str = (datetime.now(BST) + timedelta(days=7)).strftime("%Y-%m-%d %I:%M %p BST")

            # Send interactive welcome bot message directly into the inbox
            welcome_msg = (
                "🤖 *[DataKarkhana AI Stock Bot Activated!]*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ *WhatsApp Inbox Verified Successfully!*\n\n"
                "This chat is now your personal AI Stock Trading Assistant.\n"
                f"⏱️ *Session Status:* Active for 1 week (until {week_expiry_str}).\n"
                "All your price targets, sudden spikes, circuit breakers, and company filings will arrive right here.\n\n"
                "💡 *Try sending these bot commands in this chat:* \n"
                "• *GP* → Instant live quote for Grameenphone\n"
                "• *MATINSPINN* → Instant live quote for Matin Spinning\n"
                "• *TOP* → View today's top gainers & movers\n"
                "• *ALERTS* → View your active alert rules\n"
                "• *HELP* → View all available commands\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚡ Powered by DataKarkhana Desktop Engine"
            )
            asyncio.create_task(self._dispatch_whatsapp(norm, welcome_msg))

            return {
                "success": True,
                "message": f"Phone number verified successfully! WhatsApp session is active for 1 week (until {week_expiry_str}).",
                "phone": norm,
                "session_valid_until": week_expiry_str,
                "session_valid_days": 7
            }

        except Exception as e:
            logger.error(f"[verify_phone_otp error] {e}")
            return {"success": False, "message": f"Verification error: {e}"}

    def get_active_alerts_for_tickers(self, tickers: List[str]) -> List[Dict[str, Any]]:
        """Query active alerts for given list of tickers"""
        if not tickers:
            return []
        try:
            conn = get_db()
            cursor = conn.cursor()
            placeholders = ",".join(["%s"] * len(tickers))
            query = f"""
                SELECT id, user_id, whatsapp_number, ticker, alert_type, threshold_value, 
                       is_one_shot, status, last_triggered_at
                FROM user_stock_alerts
                WHERE ticker IN ({placeholders}) AND status = 'ACTIVE';
            """
            cursor.execute(query, tuple(tickers))
            rows = cursor.fetchall()
            conn.close()

            alerts = []
            for r in rows:
                if isinstance(r, dict):
                    alerts.append(r)
                else:
                    alerts.append({
                        "id": r[0],
                        "user_id": r[1],
                        "whatsapp_number": r[2],
                        "ticker": r[3],
                        "alert_type": r[4],
                        "threshold_value": float(r[5] or 0.0),
                        "is_one_shot": int(r[6] or 1),
                        "status": r[7],
                        "last_triggered_at": r[8]
                    })
            return alerts
        except Exception as e:
            logger.error(f"Error fetching active alerts for tickers: {e}")
            return []

    async def evaluate_price_alerts(self, diffs: List[Dict[str, Any]]):
        """Evaluate changed tickers against active user stock alerts"""
        if not diffs:
            return

        ticker_map = {d["ticker"]: d for d in diffs}
        alerts = self.get_active_alerts_for_tickers(list(ticker_map.keys()))
        if not alerts:
            return

        now_ts = time.time()
        for alert in alerts:
            aid = str(alert["id"])
            ticker = alert["ticker"]
            alert_type = alert["alert_type"].upper()
            threshold = float(alert.get("threshold_value") or 0.0)
            is_one_shot = bool(alert.get("is_one_shot", 1))
            phone = alert["whatsapp_number"]
            diff = ticker_map.get(ticker)

            if not diff:
                continue

            # Deadband check: minimum 30-minute cooldown (1800 seconds)
            last_ts = self._cooldown_cache.get(aid, 0.0)
            if (now_ts - last_ts) < 1800:
                continue

            ltp = float(diff.get("ltp") or 0.0)
            change = float(diff.get("change") or 0.0)
            percent = float(diff.get("percent") or 0.0)
            volume = int(diff.get("volume") or 0)
            value_mn = float(diff.get("value_mn") or 0.0)

            triggered = False
            msg_header = ""
            msg_reason = ""

            if alert_type == "PRICE_BELOW" and ltp > 0 and ltp <= threshold:
                triggered = True
                msg_header = "🎯 *[BUY TARGET REACHED]*"
                msg_reason = f"Price dropped to *{ltp:.2f} BDT* (Target: <= {threshold:.2f} BDT)"
            elif alert_type == "PRICE_ABOVE" and ltp >= threshold:
                triggered = True
                msg_header = "💰 *[PROFIT TARGET HIT]*"
                msg_reason = f"Price reached *{ltp:.2f} BDT* (Target: >= {threshold:.2f} BDT)"
            elif alert_type == "PERCENT_SPIKE" and percent >= threshold:
                triggered = True
                msg_header = "🚀 *[RAPID SPIKE ALERT]*"
                msg_reason = f"Stock surged *+{percent:.2f}%* today (Threshold: +{threshold:.2f}%)"
            elif alert_type == "PERCENT_DROP" and percent <= -abs(threshold):
                triggered = True
                msg_header = "⚠️ *[SHARP DROP WARNING]*"
                msg_reason = f"Stock dropped *{percent:.2f}%* today (Threshold: -{threshold:.2f}%)"
            elif alert_type == "CIRCUIT_LIMIT" and abs(percent) >= 9.8:
                triggered = True
                direction = "Upper Ceiling (+10%)" if percent > 0 else "Lower Floor (-10%)"
                msg_header = f"🔥 *[CIRCUIT BREAKER: {direction}]*"
                msg_reason = f"Stock hit limit at *{ltp:.2f} BDT* ({percent:+.2f}%)"

            if triggered:
                self._cooldown_cache[aid] = now_ts
                time_str = datetime.now(BST).strftime("%I:%M %p")

                # Format message like an AI Stock Assistant
                alert_text = (
                    f"🤖 *[DataKarkhana Stock Bot]*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{msg_header}\n"
                    f"📈 *Symbol:* {ticker}\n"
                    f"🎯 *Trigger:* {msg_reason}\n"
                    f"💵 *LTP:* {ltp:.2f} BDT ({change:+.2f} / {percent:+.2f}%)\n"
                    f"📊 *Volume:* {volume:,} | Value: {value_mn:.2f}M BDT\n"
                    f"🕒 *Time:* {time_str} BST (LankaBangla Live)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💬 Reply '{ticker}' in this chat for updated quote."
                )

                # Update database
                new_status = "TRIGGERED" if is_one_shot else "ACTIVE"
                self._update_alert_triggered(aid, new_status)

                # Dispatch message in worker thread
                logger.info(f"[STOCK ALERT TRIGGERED] {ticker} for {phone}: {msg_reason}")
                asyncio.create_task(self._dispatch_whatsapp(phone, alert_text))

    async def evaluate_news_alert(self, news_item: Dict[str, Any]):
        """Evaluate corporate announcements against user alert subscriptions"""
        code = str(news_item.get("code", "")).strip().upper()
        if not code:
            return

        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, whatsapp_number, ticker, alert_type, is_one_shot 
                FROM user_stock_alerts 
                WHERE (ticker = %s OR ticker = 'ALL_NEWS') AND status = 'ACTIVE'
                  AND alert_type IN ('NEWS_DISCLOSURE', 'NEWS_DIVIDEND', 'NEWS_EARNINGS', 'NEWS_CATEGORY');
            """, (code,))
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                return

            summary = news_item.get("summary", "")
            ntype = news_item.get("type", "Corporate Announcement")
            pub_date = news_item.get("date", "")
            pub_time = news_item.get("time", "")

            # Determine news tag
            tag = "📢 *[CORPORATE DISCLOSURE (PSI)]*"
            s_lower = summary.lower()
            if any(k in s_lower for k in ["dividend", "cash", "bonus"]):
                tag = "💵 *[DIVIDEND DECLARATION]*"
            elif any(k in s_lower for k in ["eps", "financials", "un-audited", "quarter"]):
                tag = "📊 *[EARNINGS / EPS REPORT]*"
            elif any(k in s_lower for k in ["category", "shifted", "z-category"]):
                tag = "🚨 *[CATEGORY SHIFT]*"
            elif any(k in s_lower for k in ["director", "sponsor", "buy", "sale"]):
                tag = "👔 *[DIRECTOR SHARE TRANSACTION]*"

            alert_text = (
                f"🤖 *[DataKarkhana Stock Bot]*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{tag}\n"
                f"📈 *Company:* {code} ({news_item.get('name', '')})\n"
                f"📑 *Type:* {ntype}\n"
                f"📝 *Details:* {summary}\n"
                f"🕒 *Published:* {pub_date} at {pub_time} BST\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💬 Reply '{code}' in this chat for live price."
            )

            for r in rows:
                phone = r[1] if isinstance(r, (list, tuple)) else r["whatsapp_number"]
                asyncio.create_task(self._dispatch_whatsapp(phone, alert_text))

        except Exception as e:
            logger.error(f"Error evaluating news alert: {e}")

    def _update_alert_triggered(self, alert_id: str, new_status: str):
        """Update last_triggered_at and status in database"""
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE user_stock_alerts 
                SET last_triggered_at = CURRENT_TIMESTAMP, status = %s 
                WHERE id = %s;
            """, (new_status, alert_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating alert triggered state: {e}")

    async def _dispatch_whatsapp(self, phone: str, message_text: str):
        """Dispatch WhatsApp message using Green-API (primary) or local headless Selenium (fallback)"""
        norm_phone = self.normalize_phone(phone)
        if not norm_phone:
            logger.warning(f"[WHATSAPP ALERT] Invalid phone: {phone}")
            return False

        # 1. Attempt Green-API cloud gateway dispatch (user-specific or global)
        try:
            from services.green_api_service import GreenApiService
            green_api = GreenApiService.get_instance()
            res = await green_api.send_message(norm_phone, message_text)
            if res.get("success"):
                logger.info(f"[WHATSAPP ALERT VIA GREEN-API SUCCESS] Dispatched to {norm_phone}")
                return True
            else:
                logger.warning(f"[WHATSAPP ALERT GREEN-API FAILED] {res.get('message')}. Attempting Selenium fallback...")
        except Exception as e:
            logger.error(f"[WHATSAPP ALERT GREEN-API EXCEPTION] {e}")

        # 2. Local headless Selenium session fallback
        try:
            from senders import send_single_whatsapp_message
            loop = asyncio.get_running_loop()
            success = await loop.run_in_executor(None, send_single_whatsapp_message, norm_phone, message_text, True)
            if success:
                logger.info(f"[WHATSAPP ALERT SUCCESS] Dispatched to {norm_phone} (Headless)")
                return True
            else:
                logger.warning(f"[WHATSAPP ALERT NOT SENT] Could not dispatch to {norm_phone}")
                return False
        except Exception as e:
            logger.error(f"[WHATSAPP ALERT EXCEPTION] {e}")
            return False

    def create_alert(
        self,
        whatsapp_number: str,
        ticker: str,
        alert_type: str,
        threshold_value: float = 0.0,
        is_one_shot: bool = True,
        user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Create a new stock alert rule after verifying phone number"""
        norm_phone = self.normalize_phone(whatsapp_number)
        if not self.is_phone_verified(norm_phone):
            raise ValueError(f"Phone number {norm_phone} is not verified. Please verify via WhatsApp OTP first.")

        aid = str(uuid.uuid4())
        code = ticker.upper().strip()
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_stock_alerts (id, user_id, whatsapp_number, ticker, alert_type, threshold_value, is_one_shot, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACTIVE');
        """, (aid, user_id, norm_phone, code, alert_type.upper(), threshold_value, 1 if is_one_shot else 0))
        conn.commit()
        conn.close()
        return {
            "id": aid,
            "whatsapp_number": norm_phone,
            "ticker": code,
            "alert_type": alert_type.upper(),
            "threshold_value": threshold_value,
            "is_one_shot": is_one_shot,
            "status": "ACTIVE"
        }

    def get_alerts(self, whatsapp_number: Optional[str] = None, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get alerts filtered by phone or user_id"""
        conn = get_db()
        cursor = conn.cursor()
        norm_phone = self.normalize_phone(whatsapp_number) if whatsapp_number else None

        if norm_phone:
            cursor.execute("""
                SELECT id, user_id, whatsapp_number, ticker, alert_type, threshold_value, is_one_shot, status, last_triggered_at, created_at
                FROM user_stock_alerts
                WHERE whatsapp_number = %s
                ORDER BY created_at DESC;
            """, (norm_phone,))
        else:
            cursor.execute("""
                SELECT id, user_id, whatsapp_number, ticker, alert_type, threshold_value, is_one_shot, status, last_triggered_at, created_at
                FROM user_stock_alerts
                ORDER BY created_at DESC;
            """)
        rows = cursor.fetchall()
        conn.close()

        alerts = []
        for r in rows:
            if isinstance(r, dict):
                alerts.append(r)
            else:
                alerts.append({
                    "id": r[0],
                    "user_id": r[1],
                    "whatsapp_number": r[2],
                    "ticker": r[3],
                    "alert_type": r[4],
                    "threshold_value": float(r[5] or 0.0),
                    "is_one_shot": bool(r[6]),
                    "status": r[7],
                    "last_triggered_at": r[8].isoformat() if r[8] else None,
                    "created_at": r[9].isoformat() if r[9] else None
                })
        return alerts

    def delete_alert(self, alert_id: str) -> bool:
        """Delete an alert by ID"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_stock_alerts WHERE id = %s;", (alert_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

    def rearm_alert(self, alert_id: str) -> bool:
        """Re-arm a triggered or paused alert"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE user_stock_alerts SET status = 'ACTIVE' WHERE id = %s;", (alert_id,))
        updated = cursor.rowcount > 0
        conn.commit()
        conn.close()
        self._cooldown_cache.pop(alert_id, None)
        return updated

    async def send_test_alert(self, phone: str) -> bool:
        """Send immediate test alert in bot persona to verify self-chat connection"""
        norm_phone = self.normalize_phone(phone)
        if not self.is_phone_verified(norm_phone):
            raise ValueError(f"Phone number {norm_phone} is not verified. Please verify via WhatsApp OTP first.")

        test_msg = (
            "🤖 *[DataKarkhana AI Stock Bot]*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ *Self-Chat Connection Active!*\n\n"
            "Your WhatsApp Self-Chat is linked and operational.\n"
            "You will receive live stock market alerts and corporate filings right here.\n\n"
            "💬 *Try replying to this message:* Send any ticker symbol (e.g. *GP*, *MATINSPINN*, *SQURPHARMA*) to get an instant live quote!\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ DataKarkhana Desktop Live Engine"
        )
        await self._dispatch_whatsapp(norm_phone, test_msg)
        return True

    def handle_bot_command(self, phone: str, text: str) -> Optional[str]:
        """Process incoming command from self-chat and return instant bot reply"""
        from services.dse_service import DSEMarketService
        cmd = text.strip().upper()
        dse = DSEMarketService.get_instance()

        if cmd in ("HELP", "MENU", "?"):
            return (
                "🤖 *[DataKarkhana Stock Bot Commands]*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "• *<TICKER>* (e.g. *GP*, *MATINSPINN*) → Live price & volume\n"
                "• *TOP* or *MOVERS* → Today's top gainers & losers\n"
                "• *ALERTS* → View your active alert rules\n"
                "• *SUMMARY* → DSEX index & total market turnover\n"
                "• *HELP* → Show this command list\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚡ Type any command right here in your self-chat!"
            )

        if cmd in ("TOP", "MOVERS", "GAINERS"):
            movers = dse._top_movers_cache
            gainers = movers.get("top_gainers", [])[:5] if movers else []
            losers = movers.get("top_losers", [])[:5] if movers else []
            lines = ["🤖 *[Today's Top Movers]*\n━━━━━━━━━━━━━━━━━━━━━━\n🟢 *Top Gainers:*"]
            for g in gainers:
                lines.append(f"• {g.get('ticker')}: {g.get('ltp')} BDT (+{g.get('percent', 0):.2f}%)")
            lines.append("\n🔴 *Top Losers:*")
            for l in losers:
                lines.append(f"• {l.get('ticker')}: {l.get('ltp')} BDT ({l.get('percent', 0):.2f}%)")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━\n⚡ LankaBangla Live")
            return "\n".join(lines)

        if cmd == "ALERTS":
            user_alerts = self.get_alerts(whatsapp_number=phone)
            if not user_alerts:
                return "🤖 You have no active alerts. Create alerts in the DataKarkhana dashboard."
            lines = [f"🤖 *[Your Active Alerts ({len(user_alerts)})]*\n━━━━━━━━━━━━━━━━━━━━━━"]
            for a in user_alerts[:8]:
                status_icon = "🟢" if a["status"] == "ACTIVE" else "⚪"
                lines.append(f"{status_icon} *{a['ticker']}* - {a['alert_type']}: {a['threshold_value']} BDT [{a['status']}]")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━")
            return "\n".join(lines)

        if cmd in ("SUMMARY", "MARKET", "DSEX"):
            summary = dse._last_market_summary
            indices = summary.get("indices", [])
            lines = ["🤖 *[DSE Market Summary]*\n━━━━━━━━━━━━━━━━━━━━━━"]
            for ind in indices:
                lines.append(f"• *{ind.get('symbol')}*: {ind.get('value')} ({ind.get('change'):+.2f} / {ind.get('percent'):+.2f}%)")
            if summary.get("total_turnover_mn"):
                lines.append(f"\n📊 *Total Turnover:* {summary.get('total_turnover_mn'):.2f}M BDT")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━\n⚡ DataKarkhana Live")
            return "\n".join(lines)

        # Check ticker lookup
        clean_ticker = cmd.replace("LTP ", "").replace("PRICE ", "").strip()
        stock = dse._last_prices.get(clean_ticker)
        if stock:
            ltp = stock.get("ltp", 0.0)
            change = stock.get("change", 0.0)
            percent = stock.get("percent", 0.0)
            vol = stock.get("volume", 0)
            val_mn = stock.get("value_mn", 0.0)
            name = stock.get("name", "")
            sector = stock.get("sector", "General")
            time_str = datetime.now(BST).strftime("%I:%M %p")
            return (
                f"🤖 *[Live Quote: {clean_ticker}]*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏢 *{name}*\n"
                f"💵 *LTP:* {ltp:.2f} BDT ({change:+.2f} / {percent:+.2f}%)\n"
                f"📊 *High:* {stock.get('high')} | *Low:* {stock.get('low')} | *YCP:* {stock.get('ycp')}\n"
                f"📦 *Volume:* {vol:,} | Value: {val_mn:.2f}M BDT\n"
                f"🏷️ *Sector:* {sector} (Cat {stock.get('category')})\n"
                f"🕒 {time_str} BST (LankaBangla Live)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ Reply with another ticker or 'TOP' for movers."
            )

        return None
