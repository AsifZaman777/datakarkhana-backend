import os
import json
import hmac
import hashlib
import base64
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

# Load secret key from environment or default JWT_SECRET
LICENSE_SECRET = os.getenv("LICENSE_MASTER_SECRET") or os.getenv("JWT_SECRET") or "marketingostad_super_secret_cyber_key_999"
LICENSE_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "license.json")
CACHE_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".license_cache.json")


def _b64_encode(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode("utf-8")).decode("utf-8").rstrip("=")


def _b64_decode(data: str) -> str:
    padding = 4 - (len(data) % 4)
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8")


def _generate_signature(payload_str: str) -> str:
    return hmac.new(LICENSE_SECRET.encode("utf-8"), payload_str.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def generate_production_license(
    customer_name: str,
    customer_email: Optional[str] = None,
    expiry_days: Optional[int] = 30,
    expires_at_str: Optional[str] = None,
    user_id: Optional[int] = None,
    payment_request_id: Optional[int] = None,
    custom_key: Optional[str] = None,
    plan_tier: str = "pro",
    credits_amount: int = 0
) -> Dict[str, Any]:
    """
    Generates a cryptographically signed production license key with an expiration date.
    """
    now = datetime.now(timezone.utc)
    if expires_at_str:
        try:
            # Validate ISO or YYYY-MM-DD
            if "T" in expires_at_str:
                exp_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            else:
                exp_dt = datetime.strptime(expires_at_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            exp_dt = now + timedelta(days=expiry_days or 30)
    else:
        exp_dt = now + timedelta(days=expiry_days or 30)

    # Format expiration to UTC ISO
    exp_iso = exp_dt.strftime("%Y-%m-%dT23:59:59Z")
    days_left = max(0, (exp_dt - now).days)

    # Clean Production Key format: e.g. DK-PROD-2026-7B9F-44A2
    if custom_key and custom_key.strip():
        prod_key = custom_key.strip().upper()
    else:
        rand_part = uuid.uuid4().hex[:8].upper()
        prod_key = f"DK-PROD-{exp_dt.year}-{rand_part[:4]}-{rand_part[4:]}"

    payload = {
        "key": prod_key,
        "customer": customer_name or "Valued Customer",
        "email": customer_email or "",
        "user_id": user_id,
        "payment_id": payment_request_id,
        "plan": plan_tier,
        "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": exp_iso
    }

    payload_json = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    sig = _generate_signature(payload_json)
    token = f"{_b64_encode(payload_json)}.{sig}"

    # Also save in database if DB is reachable
    try:
        from database import get_db
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO licenses (user_id, customer_name, customer_email, payment_request_id, production_key, license_token, plan_tier, credits_amount, is_redeemed, status, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'active', ?)
        """, (
            user_id,
            customer_name,
            customer_email,
            payment_request_id,
            prod_key,
            token,
            plan_tier,
            credits_amount,
            exp_iso
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[LICENSE DB INSERT ERROR] {e}")

    return {
        "production_key": prod_key,
        "license_token": token,
        "customer_name": customer_name,
        "customer_email": customer_email or "",
        "expires_at": exp_iso,
        "days_remaining": days_left,
        "plan_tier": plan_tier,
        "credits_amount": credits_amount,
        "is_redeemed": False,
        "is_expired": False
    }


def verify_license(key_or_token: str) -> Dict[str, Any]:
    """
    Verifies a license token or production key.
    Checks signature authenticity, expiration date, and clock tampering.
    """
    if not key_or_token or not key_or_token.strip():
        return {
            "valid": False,
            "is_expired": False,
            "status": "unlicensed",
            "message": "No license key provided."
        }

    raw = key_or_token.strip()
    token = ""

    # If it's a raw production key (e.g. DK-PROD-2026-...), look up token in DB or local license file
    if "." not in raw:
        # Look up in DB
        found_token = None
        try:
            from database import get_db
            conn = get_db()
            row = conn.execute("SELECT * FROM licenses WHERE UPPER(production_key) = UPPER(?) ORDER BY id DESC LIMIT 1", (raw,)).fetchone()
            conn.close()
            if row:
                found_token = row["license_token"]
        except Exception as e:
            print(f"[VERIFY LICENSE DB LOOKUP ERROR] {e}")

        # If not in DB, check local license.json
        if not found_token and os.path.exists(LICENSE_FILE_PATH):
            try:
                with open(LICENSE_FILE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("production_key") == raw:
                        found_token = data.get("license_token")
            except Exception:
                pass

        if found_token:
            token = found_token
        else:
            return {
                "valid": False,
                "is_expired": False,
                "status": "invalid_key",
                "message": f"Production key '{raw}' not found in database or local license repository. Please verify the key."
            }
    else:
        token = raw

    # Split token into payload and signature
    parts = token.split(".")
    if len(parts) != 2:
        return {
            "valid": False,
            "is_expired": False,
            "status": "invalid_token",
            "message": "Invalid license token structure."
        }

    payload_b64, sig = parts
    try:
        payload_json = _b64_decode(payload_b64)
        payload = json.loads(payload_json)
    except Exception:
        return {
            "valid": False,
            "is_expired": False,
            "status": "corrupt_token",
            "message": "Corrupt or unreadable license payload."
        }

    # Verify signature
    expected_sig = _generate_signature(payload_json)
    if not hmac.compare_digest(sig, expected_sig):
        return {
            "valid": False,
            "is_expired": False,
            "status": "signature_mismatch",
            "message": "Cryptographic signature validation failed. License key has been tampered with."
        }

    # Check expiration date
    expires_at_str = payload.get("expires_at")
    if not expires_at_str:
        return {
            "valid": False,
            "is_expired": True,
            "status": "expired",
            "message": "No expiration date found in license."
        }

    try:
        if "T" in expires_at_str:
            exp_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        else:
            exp_dt = datetime.strptime(expires_at_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return {
            "valid": False,
            "is_expired": True,
            "status": "invalid_expiry",
            "message": "Invalid expiration timestamp format in license."
        }

    now = datetime.now(timezone.utc)

    # Anti-clock tampering protection
    last_verified = 0.0
    if os.path.exists(CACHE_FILE_PATH):
        try:
            with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
                cdata = json.load(f)
                last_verified = float(cdata.get("last_verified_ts", 0))
        except Exception:
            pass

    current_ts = now.timestamp()
    # Allow small 15-minute clock drift, but if system time is wound back > 1 hour, flag tampering
    if last_verified > 0 and (last_verified - current_ts) > 3600:
        return {
            "valid": False,
            "is_expired": True,
            "status": "clock_rollback_detected",
            "message": "System clock rollback detected. Please set your system date & time to the current time."
        }

    # Update cache timestamp
    try:
        with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump({"last_verified_ts": current_ts, "verified_at": now.isoformat()}, f)
    except Exception:
        pass

    days_remaining = (exp_dt - now).days
    is_expired = now > exp_dt

    if is_expired:
        return {
            "valid": False,
            "is_expired": True,
            "status": "expired",
            "production_key": payload.get("key"),
            "customer_name": payload.get("customer"),
            "customer_email": payload.get("email"),
            "expires_at": expires_at_str,
            "days_remaining": 0,
            "message": f"Your desktop production license expired on {exp_dt.strftime('%B %d, %Y')}. Please contact support or renew."
        }

    return {
        "valid": True,
        "is_expired": False,
        "status": "active",
        "production_key": payload.get("key"),
        "license_token": token,
        "customer_name": payload.get("customer"),
        "customer_email": payload.get("email"),
        "plan_tier": payload.get("plan", "pro"),
        "expires_at": expires_at_str,
        "days_remaining": max(0, days_remaining),
        "message": f"License active. Valid until {exp_dt.strftime('%B %d, %Y')} ({max(0, days_remaining)} days remaining)."
    }


def activate_license(key_or_token: str) -> Dict[str, Any]:
    """
    Validates and stores the active license locally in license.json.
    """
    res = verify_license(key_or_token)
    if not res.get("valid"):
        return res

    # Save to local license file
    license_data = {
        "production_key": res.get("production_key"),
        "license_token": res.get("license_token") or key_or_token.strip(),
        "customer_name": res.get("customer_name"),
        "customer_email": res.get("customer_email"),
        "expires_at": res.get("expires_at"),
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "status": "active"
    }

    try:
        with open(LICENSE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(license_data, f, indent=2)
    except Exception as e:
        return {
            "valid": False,
            "status": "file_error",
            "message": f"Could not save license locally: {str(e)}"
        }

    return {
        **res,
        "activated": True,
        "message": f"License successfully activated! Valid until {res.get('expires_at')}."
    }


def get_current_license_status(user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Retrieves and validates the license status dynamically on a per-user basis.
    - Admins/Superadmins get full active status.
    - Regular users get their specific license evaluated against PostgreSQL.
    - Unlicensed users receive 'unlicensed' status.
    """
    now = datetime.now(timezone.utc)

    # 1. If user is authenticated:
    if user:
        role = user.get("role")
        if role in ("admin", "superadmin"):
            return {
                "valid": True,
                "is_expired": False,
                "status": "active",
                "production_key": "ADMIN-SUPER-ACCESS",
                "customer_name": user.get("full_name") or "Administrator",
                "customer_email": user.get("email"),
                "plan_tier": "admin",
                "expires_at": "2099-12-31T23:59:59Z",
                "days_remaining": 9999,
                "message": "Superadmin Access • Unlimited"
            }

        # Check PostgreSQL for this specific user's license
        try:
            from database import get_db
            conn = get_db()
            user_id = user.get("id")
            email = (user.get("email") or "").strip().lower()
            row = conn.execute(
                """SELECT * FROM licenses 
                   WHERE (user_id = ? OR LOWER(customer_email) = ?) 
                   ORDER BY expires_at DESC LIMIT 1""",
                (user_id, email)
            ).fetchone()
            conn.close()

            if row:
                row_dict = dict(row)
                if row_dict.get("status") == "revoked":
                    return {
                        "valid": False,
                        "is_expired": True,
                        "status": "revoked",
                        "production_key": row_dict.get("production_key"),
                        "customer_name": row_dict.get("customer_name"),
                        "customer_email": row_dict.get("customer_email"),
                        "expires_at": str(row_dict.get("expires_at")),
                        "days_remaining": 0,
                        "message": "Your desktop license has been revoked by the administrator."
                    }

                exp_val = row_dict.get("expires_at")
                if isinstance(exp_val, datetime):
                    exp_dt = exp_val if exp_val.tzinfo else exp_val.replace(tzinfo=timezone.utc)
                else:
                    exp_str = str(exp_val)
                    if "T" in exp_str:
                        exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    else:
                        exp_dt = datetime.strptime(exp_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

                is_exp = now > exp_dt
                days_left = max(0, (exp_dt - now).days)

                if is_exp:
                    return {
                        "valid": False,
                        "is_expired": True,
                        "status": "expired",
                        "production_key": row_dict.get("production_key"),
                        "customer_name": row_dict.get("customer_name"),
                        "customer_email": row_dict.get("customer_email"),
                        "plan_tier": row_dict.get("plan_tier") or "pro",
                        "expires_at": exp_dt.isoformat(),
                        "days_remaining": 0,
                        "message": f"Your desktop production license expired on {exp_dt.strftime('%B %d, %Y')}."
                    }

                return {
                    "valid": True,
                    "is_expired": False,
                    "status": "active",
                    "production_key": row_dict.get("production_key"),
                    "license_token": row_dict.get("license_token"),
                    "customer_name": row_dict.get("customer_name") or user.get("full_name"),
                    "customer_email": row_dict.get("customer_email") or user.get("email"),
                    "plan_tier": row_dict.get("plan_tier") or "pro",
                    "expires_at": exp_dt.isoformat(),
                    "days_remaining": days_left,
                    "credits_amount": row_dict.get("credits_amount", 0),
                    "is_redeemed": bool(row_dict.get("is_redeemed", 0)),
                    "message": f"License active. Valid until {exp_dt.strftime('%B %d, %Y')} ({days_left} days remaining)."
                }

        except Exception as e:
            print(f"[LICENSE STATUS DB ERROR] {e}")

        # Regular user with no license in DB
        return {
            "valid": False,
            "is_expired": False,
            "status": "unlicensed",
            "production_key": None,
            "customer_name": user.get("full_name"),
            "customer_email": user.get("email"),
            "expires_at": None,
            "days_remaining": 0,
            "message": "No active license assigned. Enter your production key in the Subscribe panel to activate."
        }

    # 2. If unauthenticated / environment key check
    env_key = os.getenv("PRODUCTION_KEY") or os.getenv("LICENSE_KEY")
    if env_key:
        return verify_license(env_key)

    return {
        "valid": False,
        "is_expired": False,
        "status": "unlicensed",
        "production_key": None,
        "customer_name": None,
        "expires_at": None,
        "days_remaining": 0,
        "message": "Desktop app is unlicensed. Please log in and enter your production key to activate."
    }
