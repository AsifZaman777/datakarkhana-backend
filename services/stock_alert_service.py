import asyncio
import logging
import uuid
import time
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
                msg_header = "🎯 [BUY TARGET MET]"
                msg_reason = f"Price dropped to {ltp:.2f} BDT (Target: <= {threshold:.2f} BDT)"
            elif alert_type == "PRICE_ABOVE" and ltp >= threshold:
                triggered = True
                msg_header = "💰 [SELL / PROFIT TARGET]"
                msg_reason = f"Price reached {ltp:.2f} BDT (Target: >= {threshold:.2f} BDT)"
            elif alert_type == "PERCENT_SPIKE" and percent >= threshold:
                triggered = True
                msg_header = "🚀 [INTRA-DAY SPIKE]"
                msg_reason = f"Stock jumped +{percent:.2f}% today (Threshold: +{threshold:.2f}%)"
            elif alert_type == "PERCENT_DROP" and percent <= -abs(threshold):
                triggered = True
                msg_header = "⚠️ [SHARP DROP ALERT]"
                msg_reason = f"Stock dropped {percent:.2f}% today (Threshold: -{threshold:.2f}%)"
            elif alert_type == "CIRCUIT_LIMIT" and abs(percent) >= 9.8:
                triggered = True
                direction = "Ceiling (+10%)" if percent > 0 else "Floor (-10%)"
                msg_header = f"🔥 [CIRCUIT BREAKER: {direction}]"
                msg_reason = f"Stock touched limit at {ltp:.2f} BDT ({percent:+.2f}%)"

            if triggered:
                self._cooldown_cache[aid] = now_ts
                time_str = datetime.now(BST).strftime("%I:%M %p")
                
                # Format message
                alert_text = (
                    f"{msg_header}\n"
                    f"📈 Ticker: {ticker}\n"
                    f"🎯 Status: {msg_reason}\n"
                    f"💵 Current LTP: {ltp:.2f} BDT ({change:+.2f} / {percent:+.2f}%)\n"
                    f"📊 Volume: {volume:,} | Value: {value_mn:.2f} mn BDT\n"
                    f"🕒 Time: {time_str} BST (DSE Live)\n\n"
                    f"⚡ DataKarkhana Stock Intelligence"
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
            tag = "📢 [DSE CORPORATE NEWS]"
            s_lower = summary.lower()
            if any(k in s_lower for k in ["dividend", "cash", "bonus"]):
                tag = "💵 [DIVIDEND DECLARATION]"
            elif any(k in s_lower for k in ["eps", "financials", "un-audited", "quarter"]):
                tag = "📊 [EARNINGS / EPS REPORT]"
            elif any(k in s_lower for k in ["category", "shifted", "z-category"]):
                tag = "🚨 [CATEGORY SHIFT]"
            elif any(k in s_lower for k in ["director", "sponsor", "buy", "sale"]):
                tag = "👔 [DIRECTOR TRADE]"

            alert_text = (
                f"{tag}\n"
                f"📈 Company: {code} ({news_item.get('name', '')})\n"
                f"📑 Category: {ntype}\n"
                f"📝 Details: {summary}\n"
                f"🕒 Published: {pub_date} at {pub_time} BST\n\n"
                f"⚡ DataKarkhana Stock Alert"
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
        """Run Selenium WhatsApp sender in threadpool to prevent blocking the async loop"""
        try:
            from senders import send_single_whatsapp_message
            loop = asyncio.get_running_loop()
            success = await loop.run_in_executor(None, send_single_whatsapp_message, phone, message_text)
            if success:
                logger.info(f"[WHATSAPP ALERT SUCCESS] Dispatched to {phone}")
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
        """Create a new stock alert rule"""
        aid = str(uuid.uuid4())
        code = ticker.upper().strip()
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_stock_alerts (id, user_id, whatsapp_number, ticker, alert_type, threshold_value, is_one_shot, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACTIVE');
        """, (aid, user_id, whatsapp_number, code, alert_type.upper(), threshold_value, 1 if is_one_shot else 0))
        conn.commit()
        conn.close()
        return {
            "id": aid,
            "whatsapp_number": whatsapp_number,
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
        if whatsapp_number:
            cursor.execute("""
                SELECT id, user_id, whatsapp_number, ticker, alert_type, threshold_value, is_one_shot, status, last_triggered_at, created_at
                FROM user_stock_alerts
                WHERE whatsapp_number = %s
                ORDER BY created_at DESC;
            """, (whatsapp_number,))
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
        """Send immediate test alert to verify local WhatsApp connection"""
        test_msg = (
            "🔔 [DataKarkhana Stock Intelligence]\n"
            "✅ Test Alert: Your WhatsApp connection is active!\n"
            "You will receive live DSE stock market alerts on this number.\n\n"
            "⚡ DataKarkhana Desktop Live Engine"
        )
        await self._dispatch_whatsapp(phone, test_msg)
        return True
