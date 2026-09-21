import os
import time
from typing import Optional
from fastapi import Request, Header, HTTPException, status, Depends
import requests

from core.security import decode_jwt_token
from license_service import get_current_license_status

def _get_db():
    from database import get_db
    return get_db()

def _is_sqlite_active():
    from database import is_sqlite_active
    return is_sqlite_active()

_user_auth_cache = {}  # {token: (timestamp, user_dict)}

def get_current_user(request: Request, authorization: Optional[str] = Header(None), token: Optional[str] = None):
    raw_token = None
    if authorization and authorization.startswith("Bearer "):
        raw_token = authorization.split(" ")[1]
    elif token:
        raw_token = token

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header or token query parameter."
        )
    user_payload = decode_jwt_token(raw_token)
    if not user_payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid login token."
        )
    
    # Fast in-memory cache for concurrent requests (8s TTL)
    now = time.time()
    cached = _user_auth_cache.get(raw_token)
    if cached and (now - cached[0] < 8):
        return cached[1]

    # Retrieve user from DB
    conn = _get_db()
    
    # Check IP Ban first
    client_ip = request.client.host if request.client else "unknown"
    ip_ban = conn.execute("SELECT * FROM banned_ips WHERE ip_address = ?", (client_ip,)).fetchone()
    if ip_ban:
        conn.close()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your IP address has been banned for security violations."
        )
        
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_payload["user_id"],)).fetchone()
    conn.close()

    if not user:
        # If in local mode, attempt to sync/fetch profile from Render Cloud API using user's valid JWT
        cloud_url = os.getenv("RENDER_EXTERNAL_URL") or "https://datakarkhana-backend.onrender.com"
        fetched_cloud_user = None
        try:
            resp = requests.get(
                f"{cloud_url.rstrip('/')}/api/auth/me",
                headers={"Authorization": f"Bearer {raw_token}"},
                timeout=5
            )
            if resp.status_code == 200:
                fetched_cloud_user = resp.json()
        except Exception:
            pass

        if fetched_cloud_user:
            # Sync user into local database
            conn_sync = _get_db()
            try:
                conn_sync.execute("""
                    INSERT INTO users (id, email, full_name, password_hash, role, credits, is_verified, is_banned)
                    VALUES (?, ?, ?, 'cloud_synced', ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        email = EXCLUDED.email,
                        full_name = EXCLUDED.full_name,
                        role = EXCLUDED.role,
                        credits = EXCLUDED.credits,
                        is_verified = EXCLUDED.is_verified,
                        is_banned = EXCLUDED.is_banned;
                """, (
                    fetched_cloud_user.get("id", user_payload["user_id"]),
                    fetched_cloud_user.get("email", user_payload.get("email", "")),
                    fetched_cloud_user.get("full_name", user_payload.get("full_name", "")),
                    fetched_cloud_user.get("role", user_payload.get("role", "user")),
                    fetched_cloud_user.get("credits", 0),
                    fetched_cloud_user.get("is_verified", 1),
                    fetched_cloud_user.get("is_banned", 0)
                ))
                conn_sync.commit()
                user = conn_sync.execute("SELECT * FROM users WHERE id = ?", (user_payload["user_id"],)).fetchone()
            except Exception as sync_err:
                print(f"[CLOUD USER SYNC NOTICE] {sync_err}")
            finally:
                conn_sync.close()
        elif _is_sqlite_active():
            # Fallback for offline SQLite using JWT claims
            conn_sync = _get_db()
            try:
                conn_sync.execute("""
                    INSERT INTO users (id, email, full_name, password_hash, role, credits, is_verified, is_banned)
                    VALUES (?, ?, ?, 'local_offline', ?, 50, 1, 0)
                    ON CONFLICT (id) DO NOTHING;
                """, (
                    user_payload["user_id"],
                    user_payload.get("email", "user@local"),
                    user_payload.get("full_name", "Local User"),
                    user_payload.get("role", "user")
                ))
                conn_sync.commit()
                user = conn_sync.execute("SELECT * FROM users WHERE id = ?", (user_payload["user_id"],)).fetchone()
            except Exception as offline_err:
                print(f"[OFFLINE USER UPSERT NOTICE] {offline_err}")
            finally:
                conn_sync.close()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account no longer exists."
        )
        
    if user["is_banned"] == 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=user["warning_message"] or "Your account has been suspended for security policy violations."
        )
        
    user_dict = dict(user)
    _user_auth_cache[raw_token] = (now, user_dict)
    return user_dict

def get_optional_current_user(request: Request, authorization: Optional[str] = Header(None), token: Optional[str] = None) -> Optional[dict]:
    """Extracts current user if Authorization Bearer token is present, else None without raising 401."""
    try:
        raw_token = None
        if authorization and authorization.startswith("Bearer "):
            raw_token = authorization.split(" ")[1]
        elif token:
            raw_token = token

        if not raw_token:
            return None

        user_payload = decode_jwt_token(raw_token)
        if not user_payload:
            return None

        conn = _get_db()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_payload["user_id"],)).fetchone()
        conn.close()
        if not user or user["is_banned"] == 1:
            return None
        return dict(user)
    except Exception:
        return None

def get_admin_user(current_user: dict = Depends(get_current_user)):
    if current_user["role"] not in ("admin", "superadmin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access is required."
        )
    return current_user

def check_desktop_license(current_user: dict = Depends(get_current_user)):
    """Ensures local desktop scraping/operations are licensed and not expired"""
    if os.environ.get("TESTING") == "1":
        return True

    if current_user.get("role") in ("superadmin", "admin"):
        return True

    lic = get_current_license_status(current_user)
    if not lic.get("valid"):
        if lic.get("is_expired"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"License Expired: Your desktop production license expired on {lic.get('expires_at')}. Please enter an updated production key."
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="License Required: Please activate your desktop app with a production key in the Subscribe panel to run local operations."
            )
    return True

def get_user_plan_tier(conn, user_id: int, user_email: str, role: str) -> str:
    if role in ("admin", "superadmin"):
        return "admin"
    try:
        lic = conn.execute(
            """SELECT plan_tier FROM licenses 
               WHERE (user_id = ? OR LOWER(customer_email) = ?) AND status = 'active'
               ORDER BY expires_at DESC LIMIT 1""",
            (user_id, (user_email or "").strip().lower())
        ).fetchone()
        if lic and lic["plan_tier"]:
            return str(lic["plan_tier"]).lower()
    except Exception:
        pass
    return "starter"

def user_can_sync_to_cloud(user: dict, plan_tier: str) -> bool:
    if user.get("role") in ("admin", "superadmin"):
        return True
    if user.get("allow_sync") == 1 or user.get("allow_sync") is True:
        return True
    if str(plan_tier).lower() in ("pro", "enterprise"):
        return True
    return False
