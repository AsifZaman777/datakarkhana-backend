import os
import re
import logging
from typing import Dict, Any, Optional
import httpx
from database import get_db

logger = logging.getLogger("green_api_service")
logger.setLevel(logging.INFO)

class GreenApiService:
    _instance: Optional["GreenApiService"] = None

    def __init__(self):
        # Optional global fallback from environment (if provided)
        self.instance_id = os.getenv("GREEN_API_INSTANCE_ID", "").strip()
        self.api_token = os.getenv("GREEN_API_TOKEN", "").strip()
        self.host = self.resolve_host(self.instance_id, os.getenv("GREEN_API_HOST", "").strip())

    @classmethod
    def get_instance(cls) -> "GreenApiService":
        if cls._instance is None:
            cls._instance = GreenApiService()
        return cls._instance

    @staticmethod
    def normalize_phone(phone: str) -> str:
        """Sanitize phone number into international E.164 format without plus (e.g. 8801863443343)"""
        if not phone:
            return ""
        digits = re.sub(r"\D", "", str(phone).strip())
        if digits.startswith("01"):
            digits = "88" + digits
        elif digits.startswith("1") and len(digits) == 10:
            digits = "880" + digits
        return digits

    @classmethod
    def format_chat_id(cls, phone: str) -> str:
        """Format phone into Green-API chatId format (e.g. 8801863443343@c.us or group-id@g.us)"""
        p = str(phone).strip()
        if "@g.us" in p or "@c.us" in p:
            return p
        norm = cls.normalize_phone(p)
        return f"{norm}@c.us"

    @staticmethod
    def resolve_host(instance_id: str, custom_url: Optional[str] = None) -> str:
        """Determine Green-API base host URL from custom URL or instance ID prefix"""
        if custom_url and custom_url.strip():
            url = custom_url.strip().rstrip("/")
            if not url.startswith("http://") and not url.startswith("https://"):
                url = f"https://{url}"
            return url

        iid = str(instance_id).strip()
        if len(iid) >= 4 and iid[:4].isdigit():
            return f"https://{iid[:4]}.api.greenapi.com"
        return "https://api.green-api.com"

    def is_configured(self) -> bool:
        """Check if global fallback Green API credentials are set"""
        return bool(self.instance_id and self.api_token)

    async def check_credentials(
        self,
        instance_id: str,
        api_token: str,
        host: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Query Green-API getStateInstance to verify instance credentials.
        Returns authorization state: authorized, notAuthorized, starting, etc.
        """
        iid = str(instance_id).strip()
        token = str(api_token).strip()
        if not iid or not token:
            return {
                "success": False,
                "state": "unconfigured",
                "message": "Both Instance ID and API Token are required."
            }

        resolved_host = self.resolve_host(iid, host)
        url = f"{resolved_host}/waInstance{iid}/getStateInstance/{token}"

        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json()
                    state = data.get("stateInstance", "unknown")
                    is_auth = (state == "authorized")
                    msg = "Instance is authorized and ready for WhatsApp delivery." if is_auth else f"Instance found (status: {state}). Scan QR code in your Green-API console to authorize."
                    return {
                        "success": True,
                        "state": state,
                        "is_authorized": is_auth,
                        "host": resolved_host,
                        "message": msg,
                        "raw": data
                    }
                else:
                    return {
                        "success": False,
                        "state": "error",
                        "status_code": res.status_code,
                        "message": f"Green-API error ({res.status_code}): {res.text}"
                    }
        except Exception as e:
            logger.error(f"[GREEN-API CHECK ERROR] {e}")
            return {
                "success": False,
                "state": "error",
                "message": f"Could not connect to Green-API ({e}). Please check your API URL and internet connection."
            }

    def save_user_config(
        self,
        phone: str,
        instance_id: str,
        api_token: str,
        api_url: Optional[str] = None,
        user_id: Optional[int] = None,
        is_authorized: bool = True
    ) -> Dict[str, Any]:
        """Save or update user-specific Green-API credentials in database"""
        norm_phone = self.normalize_phone(phone)
        if not norm_phone:
            raise ValueError("A valid WhatsApp phone number is required.")

        iid = str(instance_id).strip()
        token = str(api_token).strip()
        if not iid or not token:
            raise ValueError("Both Instance ID and API Token are required.")

        resolved_host = self.resolve_host(iid, api_url)

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_green_api_configs (phone_number, user_id, instance_id, api_token, api_url, is_authorized, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (phone_number) DO UPDATE
            SET user_id = COALESCE(EXCLUDED.user_id, user_green_api_configs.user_id),
                instance_id = EXCLUDED.instance_id,
                api_token = EXCLUDED.api_token,
                api_url = EXCLUDED.api_url,
                is_authorized = EXCLUDED.is_authorized,
                updated_at = CURRENT_TIMESTAMP;
        """, (norm_phone, user_id, iid, token, resolved_host, 1 if is_authorized else 0))
        conn.commit()
        conn.close()

        logger.info(f"[GREEN-API USER CONFIG SAVED] Phone: {norm_phone}, Instance: {iid}")
        return {
            "success": True,
            "phone_number": norm_phone,
            "instance_id": iid,
            "api_url": resolved_host,
            "is_authorized": is_authorized
        }

    def get_user_config(self, phone: str) -> Optional[Dict[str, Any]]:
        """Retrieve user-specific Green-API credentials from database by phone number"""
        norm_phone = self.normalize_phone(phone)
        if not norm_phone:
            return None

        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT phone_number, user_id, instance_id, api_token, api_url, is_authorized, updated_at
                FROM user_green_api_configs
                WHERE phone_number = %s;
            """, (norm_phone,))
            row = cursor.fetchone()
            conn.close()

            if not row:
                return None

            if isinstance(row, dict):
                return row

            return {
                "phone_number": row[0],
                "user_id": row[1],
                "instance_id": row[2],
                "api_token": row[3],
                "api_url": row[4],
                "is_authorized": bool(row[5]),
                "updated_at": row[6]
            }
        except Exception as e:
            logger.error(f"[get_user_config error] {e}")
            return None

    def delete_user_config(self, phone: str) -> bool:
        """Remove user-specific Green-API credentials from database"""
        norm_phone = self.normalize_phone(phone)
        if not norm_phone:
            return False

        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_green_api_configs WHERE phone_number = %s;", (norm_phone,))
            deleted = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"[delete_user_config error] {e}")
            return False

    async def send_message(
        self,
        phone: str,
        text: str,
        instance_id: Optional[str] = None,
        api_token: Optional[str] = None,
        host: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Send text message to WhatsApp phone via Green-API REST endpoint.
        Uses user-specific credentials stored in database if available,
        or explicitly passed credentials, or global fallback.
        """
        norm_phone = self.normalize_phone(phone)
        if not norm_phone:
            return {"success": False, "message": "Invalid recipient phone number."}

        target_iid = instance_id
        target_token = api_token
        target_host = host

        # 1. If credentials not explicitly passed, lookup in user_green_api_configs
        if not target_iid or not target_token:
            user_cfg = self.get_user_config(norm_phone)
            if user_cfg:
                target_iid = user_cfg["instance_id"]
                target_token = user_cfg["api_token"]
                target_host = user_cfg.get("api_url")

        # 2. Fallback to global config if available
        if not target_iid or not target_token:
            if self.is_configured():
                target_iid = self.instance_id
                target_token = self.api_token
                target_host = self.host
            else:
                return {
                    "success": False,
                    "message": (
                        f"Green-API credentials are not configured for {norm_phone}. "
                        "Please enter your Instance ID and API Token in Step 2."
                    )
                }

        resolved_host = self.resolve_host(target_iid, target_host)
        chat_id = self.format_chat_id(norm_phone)
        url = f"{resolved_host}/waInstance{target_iid}/sendMessage/{target_token}"
        payload = {
            "chatId": chat_id,
            "message": text
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    msg_id = data.get("idMessage", "")
                    logger.info(f"[GREEN-API SENT] Dispatched to {chat_id} via instance {target_iid} (MessageId: {msg_id})")
                    return {
                        "success": True,
                        "idMessage": msg_id,
                        "chatId": chat_id,
                        "instance_id": target_iid,
                        "message": "Delivered successfully via Green-API."
                    }
                else:
                    err_msg = f"Green-API HTTP {res.status_code}: {res.text}"
                    logger.error(f"[GREEN-API SEND ERROR] {err_msg}")
                    return {
                        "success": False,
                        "message": err_msg,
                        "status_code": res.status_code
                    }
        except Exception as e:
            logger.error(f"[GREEN-API EXCEPTION] {e}")
            return {
                "success": False,
                "message": f"Network exception communicating with Green-API: {e}"
            }
