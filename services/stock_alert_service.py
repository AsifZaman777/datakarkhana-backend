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
        """Check whether this phone number has completed WhatsApp OTP verification"""
        norm = self.normalize_phone(phone)
        if not norm:
            return False
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT phone_number FROM verified_phone_numbers WHERE phone_number = %s;", (norm,))
            row = cursor.fetchone()
            conn.close()
            return row is not None
        except Exception as e:
            logger.error(f"[is_phone_verified error] {e}")
            return False

    async def send_verification_otp(self, phone: str) -> Dict[str, Any]:
        """Generate a 6-digit OTP, store in database, and dispatch via headless WhatsApp self-chat"""
        norm = self.normalize_phone(phone)
        if not norm or len(norm) < 11:
            return {"success": False, "message": "Please enter a valid WhatsApp phone number (e.g. 017xxxxxxxx)."}

        otp_code = f"{random.randint(100000, 999999)}"
        expires_at = time.time() + 600.0  # 10 minutes

        try:
            conn = get_db()
            cursor = conn.cursor()
            # Upsert into phone_verification_otps
            cursor.execute("""
                INSERT INTO phone_verification_otps (phone_number, otp_code, expires_at, attempts)
                VALUES (%s, %s, %s, 0)
                ON CONFLICT (phone_number) DO UPDATE
                SET otp_code = EXCLUDED.otp_code, expires_at = EXCLUDED.expires_at, attempts = 0, created_at = CURRENT_TIMESTAMP;
            """, (norm, otp_code, expires_at))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[send_verification_otp db error] {e}")
            return {"success": False, "message": f"Database error storing OTP: {e}"}

        # Format message in conversational AI bot style for Self-Chat
        otp_message = (
            "🤖 *[DataKarkhana AI Stock Bot]*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 *WhatsApp Self-Chat Verification*\n\n"
            f"Your one-time security OTP code is:\n"
            f"👉  *{otp_code}*  👈\n\n"
            "Enter this 6-digit code in DataKarkhana to verify this phone number.\n"
            "Once verified, your personal WhatsApp Self-Chat will be linked to receive:\n"
            "• Real-time stock target price alerts\n"
            "• Circuit breaker & intra-day spike notifications\n"
            "• Corporate announcements (PSI), earnings & dividends\n"
            "• Conversational bot replies right inside your self-chat!\n\n"
            "⏱️ *Code expires in 10 minutes.*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ DataKarkhana Live Trading Intelligence"
        )

        logger.info(f"[OTP GENERATED] Sent OTP {otp_code} to {norm}")
        asyncio.create_task(self._dispatch_whatsapp(norm, otp_message))
        return {
            "success": True,
            "message": f"Verification code dispatched to {norm} via WhatsApp.",
            "phone": norm
        }

    async def verify_phone_otp(self, phone: str, otp: str) -> Dict[str, Any]:
        """Verify the user-provided OTP code against the database record"""
        norm = self.normalize_phone(phone)
        input_code = str(otp).strip()

        if not norm or not input_code:
            return {"success": False, "message": "Phone number and OTP code are required."}

        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT otp_code, expires_at, attempts FROM phone_verification_otps WHERE phone_number = %s;", (norm,))
            row = cursor.fetchone()

            if not row:
                conn.close()
                return {"success": False, "message": "No verification request found for this phone number. Please request a new OTP."}

            stored_code = row[0] if isinstance(row, (list, tuple)) else row["otp_code"]
            expires_at = float(row[1] if isinstance(row, (list, tuple)) else row["expires_at"])
            attempts = int(row[2] if isinstance(row, (list, tuple)) else row["attempts"])

            if time.time() > expires_at:
                conn.close()
                return {"success": False, "message": "This verification code has expired. Please request a new OTP."}

            if attempts >= 5:
                conn.close()
                return {"success": False, "message": "Too many failed attempts. Please request a new OTP."}

            if stored_code != input_code:
                cursor.execute("UPDATE phone_verification_otps SET attempts = attempts + 1 WHERE phone_number = %s;", (norm,))
                conn.commit()
                conn.close()
                return {"success": False, "message": "Incorrect verification code. Please check and try again."}

            # Verification successful: record verified number and clean up OTP
            cursor.execute("DELETE FROM phone_verification_otps WHERE phone_number = %s;", (norm,))
            cursor.execute("""
                INSERT INTO verified_phone_numbers (phone_number, verified_at)
                VALUES (%s, CURRENT_TIMESTAMP)
                ON CONFLICT (phone_number) DO UPDATE SET verified_at = CURRENT_TIMESTAMP;
            """, (norm,))
            conn.commit()
            conn.close()

            # Send interactive welcome bot message into the self-chat
            welcome_msg = (
                "🤖 *[DataKarkhana AI Stock Bot Activated!]*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ *WhatsApp Self-Chat Verified Successfully!*\n\n"
                "This self-chat is now your personal AI Stock Trading Assistant.\n"
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
                "message": "Phone number verified successfully! Self-chat alerts are now enabled.",
                "phone": norm
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
        """Run headless Selenium WhatsApp sender in threadpool to prevent blocking the async loop"""
        try:
            from senders import send_single_whatsapp_message
            loop = asyncio.get_running_loop()
            success = await loop.run_in_executor(None, send_single_whatsapp_message, phone, message_text, True)
            if success:
                logger.info(f"[WHATSAPP ALERT SUCCESS] Dispatched to {phone} (Headless)")
            else:
                logger.warning(f"[WHATSAPP ALERT NOT SENT] Could not dispatch to {phone}")
        except Exception as e:
            logger.error(f"[WHATSAPP ALERT EXCEPTION] {e}")

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
