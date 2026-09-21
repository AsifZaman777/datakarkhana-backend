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

def normalize_plan_tier(val: Optional[str]) -> str:
    """Normalizes package names or plan strings to standard tier keys (starter, pro, enterprise, admin)"""
    if not val:
        return "starter"
    v = str(val).strip().lower()
    if "superadmin" in v or "admin" in v:
        return "admin"
    if "enterprise" in v:
        return "enterprise"
    if "pro" in v:
        return "pro"
    if "starter" in v:
        return "starter"
    return v


def get_tier_permissions(conn, tier_id: str) -> dict:
    """Fetches access permissions for a specific tier from tier_permissions table with fallbacks."""
    normalized = normalize_plan_tier(tier_id)
    default_map = {
        "starter": {
            "tier_id": "starter",
            "tier_name": "Starter Pack",
            "allow_sync": False,
            "max_sync_files": 0,
            "allow_dataset_download": False,
            "allow_daraz_download": False,
            "can_use_scraper": True,
            "can_use_marketing": False
        },
        "pro": {
            "tier_id": "pro",
            "tier_name": "Pro Growth Pack",
            "allow_sync": True,
            "max_sync_files": 5,
            "allow_dataset_download": True,
            "allow_daraz_download": True,
            "can_use_scraper": True,
            "can_use_marketing": True
        },
        "enterprise": {
            "tier_id": "enterprise",
            "tier_name": "Enterprise Mega Pack",
            "allow_sync": True,
            "max_sync_files": 25,
            "allow_dataset_download": True,
            "allow_daraz_download": True,
            "can_use_scraper": True,
            "can_use_marketing": True
        },
        "admin": {
            "tier_id": "admin",
            "tier_name": "Super Admin",
            "allow_sync": True,
            "max_sync_files": 999,
            "allow_dataset_download": True,
            "allow_daraz_download": True,
            "can_use_scraper": True,
            "can_use_marketing": True
        }
    }

    if not conn:
        return default_map.get(normalized, default_map["starter"])

    try:
        row = conn.execute(
            "SELECT * FROM tier_permissions WHERE tier_id = ? OR tier_id = ?",
            (tier_id, normalized)
        ).fetchone()
        if row:
            r = dict(row)
            return {
                "tier_id": r.get("tier_id") or normalized,
                "tier_name": r.get("tier_name") or tier_id.capitalize(),
                "allow_sync": bool(r.get("allow_sync", 1)),
                "max_sync_files": int(r.get("max_sync_files", 5)),
                "allow_dataset_download": bool(r.get("allow_dataset_download", 1)),
                "allow_daraz_download": bool(r.get("allow_daraz_download", 1)),
                "can_use_scraper": bool(r.get("can_use_scraper", 1)),
                "can_use_marketing": bool(r.get("can_use_marketing", 1)),
                "updated_at": str(r.get("updated_at") or "")
            }
    except Exception:
        pass

    return default_map.get(normalized, default_map["starter"])


def get_user_plan_tier(conn, user_id: int, user_email: str, role: str) -> str:
    """
    Resolves the plan tier of a user.
    Prioritizes admin roles -> active licenses -> approved payment requests -> starter.
    Normalizes pack names (e.g. 'Enterprise Mega Pack' -> 'enterprise').
    """
    if role in ("admin", "superadmin"):
        return "admin"
    try:
        clean_email = (user_email or "").strip().lower()

        # 1. Check active licenses
        lics = conn.execute(
            """SELECT plan_tier, expires_at FROM licenses 
               WHERE (user_id = ? OR LOWER(customer_email) = ?) AND status = 'active'
               ORDER BY expires_at DESC""",
            (user_id, clean_email)
        ).fetchall()
        for lic in lics:
            t = normalize_plan_tier(lic["plan_tier"])
            if t == "enterprise":
                return "enterprise"
        for lic in lics:
            t = normalize_plan_tier(lic["plan_tier"])
            if t == "pro":
                return "pro"

        # 2. Check approved payment requests
        pays = conn.execute(
            """SELECT package_name FROM payment_requests 
               WHERE user_id = ? AND status = 'approved'
               ORDER BY id DESC""",
            (user_id,)
        ).fetchall()
        for pay in pays:
            t = normalize_plan_tier(pay["package_name"])
            if t == "enterprise":
                return "enterprise"
        for pay in pays:
            t = normalize_plan_tier(pay["package_name"])
            if t == "pro":
                return "pro"

        # Check other active licenses if custom tier
        if lics and lics[0]["plan_tier"]:
            return normalize_plan_tier(lics[0]["plan_tier"])
    except Exception:
        pass
    return "starter"


def get_user_effective_permissions(conn, user: dict, plan_tier: Optional[str] = None) -> dict:
    """
    Evaluates effective permissions for a user by merging tier policy defaults
    with user-level manual overrides (e.g. allow_sync, max_sync_files, allow_download).
    """
    role = user.get("role", "user")
    if role in ("admin", "superadmin"):
        return {
            "tier_id": "admin",
            "tier_name": "Admin / Superadmin",
            "allow_sync": True,
            "max_sync_files": 999,
            "allow_dataset_download": True,
            "allow_daraz_download": True,
            "can_use_scraper": True,
            "can_use_marketing": True,
            "is_sync_overridden": False,
            "is_download_overridden": False,
            "is_quota_overridden": False
        }

    resolved_tier = plan_tier or get_user_plan_tier(conn, user.get("id", 0), user.get("email", ""), role)
    tier_policy = get_tier_permissions(conn, resolved_tier)

    # Check user-level overrides
    allow_sync = tier_policy["allow_sync"]
    is_sync_overridden = False
    if user.get("allow_sync") is not None:
        u_val = user.get("allow_sync")
        if u_val in (1, True, "1"):
            allow_sync = True
            is_sync_overridden = not tier_policy["allow_sync"]
        elif u_val in (0, False, "0"):
            # If user purchased pro/enterprise, their tier policy grants allow_sync unless explicitly disabled
            if resolved_tier in ("pro", "enterprise"):
                allow_sync = True
            else:
                allow_sync = False
                is_sync_overridden = tier_policy["allow_sync"]

    max_sync_files = tier_policy["max_sync_files"]
    is_quota_overridden = False
    if user.get("max_sync_files") is not None:
        u_max = int(user.get("max_sync_files"))
        if u_max != tier_policy["max_sync_files"]:
            max_sync_files = u_max
            is_quota_overridden = True

    allow_dataset_download = tier_policy["allow_dataset_download"]
    is_download_overridden = False
    if user.get("allow_download") is not None:
        u_dl = user.get("allow_download")
        if u_dl in (1, True, "1"):
            allow_dataset_download = True
            is_download_overridden = True
        elif u_dl in (0, False, "0"):
            allow_dataset_download = False
            is_download_overridden = True

    allow_daraz_download = tier_policy["allow_daraz_download"] or allow_dataset_download

    return {
        "tier_id": resolved_tier,
        "tier_name": tier_policy.get("tier_name", resolved_tier.capitalize()),
        "allow_sync": allow_sync,
        "max_sync_files": max_sync_files,
        "allow_dataset_download": allow_dataset_download,
        "allow_daraz_download": allow_daraz_download,
        "can_use_scraper": tier_policy["can_use_scraper"],
        "can_use_marketing": tier_policy["can_use_marketing"],
        "is_sync_overridden": is_sync_overridden,
        "is_download_overridden": is_download_overridden,
        "is_quota_overridden": is_quota_overridden
    }


def user_can_sync_to_cloud(user: dict, plan_tier: str) -> bool:
    """Checks whether a user is allowed to sync datasets to the cloud."""
    if user.get("role") in ("admin", "superadmin"):
        return True
    if user.get("allow_sync") in (1, True, "1"):
        return True
    norm = normalize_plan_tier(plan_tier)
    if norm in ("pro", "enterprise"):
        return True
    return False
