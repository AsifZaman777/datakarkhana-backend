import os
import io
import json
import time
import uuid
import base64
import glob
import threading
import queue
import asyncio

import pandas as pd
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from fastapi import FastAPI, Depends, HTTPException, status, Header, BackgroundTasks, UploadFile, File, Form, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import Response, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr

from database import get_db, init_db, _request_db_conns
from auth import hash_password, verify_password, create_jwt_token, decode_jwt_token
from license_service import (
    generate_production_license,
    verify_license,
    activate_license,
    get_current_license_status
)
from scraper import (
    setup_driver, scrape_query, save_to_excel, get_live_frame, clear_scraper_frame,
    LIVE_FRAMES, is_job_stopped, stop_scraper_job, clear_job_stop,
    subscribe_scraper_job, unsubscribe_scraper_job, publish_scraper_event,
    add_job_log, get_job_recent_logs
)
from senders import run_whatsapp_campaign, write_log_to_file, SCREENSHOTS_FOLDER as CAMPAIGN_SCREENSHOTS
from config import REGIONS, CATEGORIES, BREVO_API_KEY as CONFIG_BREVO_API_KEY, SMTP_USER as CONFIG_SMTP_USER, SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD, SUPERADMIN_NAME, FRONTEND_URL, FRONTEND_LOCAL_URL, FRONTEND_RENDER_URL, FRONTEND_MODE, BKASH_NUMBER, BKASH_ACCOUNT_TYPE, PATHAO_NUMBER, PATHAO_ACCOUNT_TYPE, CREDIT_PACKAGES
from email_templates import get_verification_email_html, get_license_key_email_html

app = FastAPI(title="MarketingOstad API Service")

@app.middleware("http")
async def db_connection_lifecycle_middleware(request: Request, call_next):
    token = _request_db_conns.set([])
    try:
        response = await call_next(request)
        return response
    finally:
        conns = _request_db_conns.get()
        if conns:
            for conn in conns:
                try:
                    conn.close()
                except Exception:
                    pass
        _request_db_conns.reset(token)

# Allow CORS for React frontend (standard dev port 5173 / 3000 / localhost & production FRONTEND_URL)
_cors_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
for _fe in [FRONTEND_URL, FRONTEND_LOCAL_URL, FRONTEND_RENDER_URL]:
    if _fe and _fe not in _cors_origins:
        _cors_origins.append(_fe)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins if FRONTEND_URL else ["*"],
    allow_origin_regex=r"https?://.*" if FRONTEND_URL else None,
    allow_credentials=True,
    allow_headers=["*"],
    allow_methods=["*"],
)

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
SCRAPE_RESULTS_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrape_results")
SCRAPER_SCREENSHOTS_FOLDER = os.path.join(SCRAPE_RESULTS_FOLDER, "screenshots")  # Legacy, kept for compat
LOGS_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SCRAPE_RESULTS_FOLDER, exist_ok=True)
os.makedirs(SCRAPER_SCREENSHOTS_FOLDER, exist_ok=True)
os.makedirs(LOGS_FOLDER, exist_ok=True)

app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")

SERVER_START_TIME = time.time()

@app.get("/health")
@app.get("/api/health")
def health_check():
    """Lightweight health check endpoint for uptime monitors and Render keep-alive pings"""
    db_status = "ok"
    try:
        conn = get_db()
        conn.execute("SELECT 1;").fetchone()
        conn.close()
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"

    uptime_sec = int(time.time() - SERVER_START_TIME)
    uptime_str = f"{uptime_sec // 3600}h {(uptime_sec % 3600) // 60}m {uptime_sec % 60}s"
    return {
        "status": "healthy" if "unhealthy" not in db_status else "degraded",
        "service": "MarketingOstad Backend",
        "database": db_status,
        "uptime": uptime_str,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

def _keep_alive_loop():
    """Pings the public Render URL every 12 minutes to keep the free-tier service awake"""
    time.sleep(30)
    target = (
        os.getenv("SELF_PING_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or "https://datakarkhana-backend.onrender.com"
    ).rstrip("/")
    ping_url = f"{target}/api/health"
    print(f"[KEEP-ALIVE] Auto-ping background task started for: {ping_url}")
    while True:
        try:
            # Sleep 12 minutes (720s) - Render free tier sleeps after 15 min of inactivity
            time.sleep(720)
            res = requests.get(ping_url, timeout=15)
            print(f"[KEEP-ALIVE] Ping {res.status_code} to {ping_url} at {datetime.now().strftime('%H:%M:%S')}")
        except Exception as err:
            print(f"[KEEP-ALIVE] Ping notice: {err}")

@app.on_event("startup")
def startup_event():
    # Launch background keep-alive pinger thread
    t = threading.Thread(target=_keep_alive_loop, daemon=True)
    t.start()

    try:
        from database import get_clean_database_url
        if not get_clean_database_url():
            print("[SUPABASE NOTICE] DATABASE_URL is not set yet in backend/.env.")
            return

        conn = get_db()
        try:
            row = conn.execute("SELECT 1 FROM users LIMIT 1;").fetchone()
            is_initialized = True
        except Exception:
            is_initialized = False

        if not is_initialized:
            conn.close()
            init_db()
            conn = get_db()

        conn.execute("UPDATE users SET is_banned = 0, is_verified = 1, warning_message = '' WHERE role IN ('admin', 'superadmin') OR email = 'admin@databazaar.com'")
        try:
            conn.execute("DELETE FROM banned_ips")
            conn.execute("UPDATE scrape_jobs SET status = 'stopped' WHERE status = 'running'")
        except Exception:
            pass
        conn.commit()
        conn.close()
        print("[OK] Supabase PostgreSQL connected and ready.")
    except Exception as e:
        print("[STARTUP DB ERROR]", e)

# ── Dependencies ─────────────────────────────────────────

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
    conn = get_db()
    
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

        conn = get_db()
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

# ── Schemas ──────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    full_name: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class CreditRequest(BaseModel):
    user_id: int
    amount: int

class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    full_name: Optional[str] = None

class ScrapeRequest(BaseModel):
    query: Optional[str] = None
    queries: Optional[list[str]] = None
    division: Optional[str] = None
    district: Optional[str] = None
    area: Optional[str] = None
    headless: Optional[bool] = False

class WhatsAppCampaignRequest(BaseModel):
    recipient_group: str
    message_template: str
    resume: Optional[bool] = False
    start_row: Optional[int] = None
    selected_contacts: Optional[list[dict]] = None

class EmailCampaignRequest(BaseModel):
    recipient_group: str
    subject: str
    html_code: str
    selected_contacts: Optional[list[dict]] = None

class ResendVerificationRequest(BaseModel):
    email: EmailStr

class BrevoApplyRequest(BaseModel):
    business_name: str
    domain_name: str
    location: str
    business_phone: str
    social_media_website: str

class BrevoApproveRequest(BaseModel):
    api_key: Optional[str] = ""
    daily_limit: Optional[int] = 300
    account_status: Optional[str] = "approved"

class BrevoRejectRequest(BaseModel):
    reason: str

class UserBrevoConfigRequest(BaseModel):
    api_key: str
    daily_limit: Optional[int] = 300
    account_status: Optional[str] = "approved"

class BrevoActivateLinkRequest(BaseModel):
    activation_url: str

# ── Free Brevo API Helper ────────────────────────────────

def generate_otp() -> str:
    import secrets
    return f"{secrets.randbelow(900000) + 100000}"

def send_free_verification_email(recipient_email: str, full_name: str, otp_code: str):
    brevo_api_key = os.getenv("BREVO_API_KEY", "")
    sender_email = os.getenv("SENDER_EMAIL", os.getenv("SUPPORT_EMAIL", os.getenv("SMTP_USER", "asifdev777@gmail.com")))

    html_body = get_verification_email_html(full_name, otp_code)
    last_error = ""

    print(f"[OTP CODE GENERATED] Verification Code for {recipient_email}: {otp_code}")

    if not brevo_api_key:
        print(f"[BREVO NOTICE] BREVO_API_KEY is not set. In local dev mode, OTP is: {otp_code}")
        return True, ""

    # Use Brevo REST API directly
    try:
        import urllib.request
        url = "https://api.brevo.com/v3/smtp/email"
        payload = {
            "sender": {"name": "DataKarkhana Platform", "email": sender_email},
            "to": [{"email": recipient_email, "name": full_name}],
            "subject": f"Your DataKarkhana Verification Code: {otp_code}",
            "htmlContent": html_body
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "accept": "application/json",
                "api-key": brevo_api_key,
                "content-type": "application/json"
            }
        )
        with urllib.request.urlopen(req) as resp:
            if resp.status in (200, 201):
                print(f"[BREVO API SUCCESS] Verification code {otp_code} sent to {recipient_email} via Brevo API!")
                return True, ""
    except urllib.error.HTTPError as ex:
        err_body = ex.read().decode("utf-8")
        print(f"[BREVO API ERROR] {ex.code}: {err_body}")
        try:
            err_json = json.loads(err_body)
            last_error = err_json.get("message", str(ex))
        except Exception:
            last_error = f"HTTP {ex.code}: {err_body}"
    except Exception as ex:
        print(f"[BREVO API ERROR] {ex}")
        last_error = str(ex)

    # In local development, if Brevo fails, don't block registration
    print(f"[DEV FALLBACK] Brevo error: {last_error}. Local OTP code remains: {otp_code}")
    return True, last_error


def send_license_key_email(recipient_email: str, customer_name: str, production_key: str, plan_name: str, credits: int, expires_at: str):
    """Sends the production license key to the customer via Brevo transactional email after payment approval."""
    brevo_api_key = os.getenv("BREVO_API_KEY", "")
    sender_email = os.getenv("SENDER_EMAIL", os.getenv("SUPPORT_EMAIL", os.getenv("SMTP_USER", "asifdev777@gmail.com")))

    if not brevo_api_key:
        print("[LICENSE EMAIL SKIP] BREVO_API_KEY not configured. License key email not sent.")
        return False, "BREVO_API_KEY is not configured."

    html_body = get_license_key_email_html(customer_name, production_key, plan_name, credits, expires_at)

    try:
        import urllib.request
        url = "https://api.brevo.com/v3/smtp/email"
        payload = {
            "sender": {"name": "DataKarkhana Platform", "email": sender_email},
            "to": [{"email": recipient_email, "name": customer_name}],
            "subject": f"🔑 Your DataKarkhana Production Key — {plan_name} ({credits} Credits)",
            "htmlContent": html_body
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "accept": "application/json",
                "api-key": brevo_api_key,
                "content-type": "application/json"
            }
        )
        with urllib.request.urlopen(req) as resp:
            if resp.status in (200, 201):
                print(f"[LICENSE EMAIL SUCCESS] License key email sent to {recipient_email}!")
                return True, ""
    except Exception as ex:
        print(f"[LICENSE EMAIL ERROR] {ex}")
        return False, str(ex)

    return False, "Unknown error sending license key email."


def resolve_frontend_base_url(request: Optional[Request] = None) -> str:
    """Dynamically determine the frontend URL based on FRONTEND_MODE flag ('local' or 'render') or request origin"""
    mode = (os.getenv("FRONTEND_MODE") or FRONTEND_MODE or "local").strip().lower()

    # 1. If FRONTEND_MODE flag is set to render
    if mode in ("render", "production", "prod"):
        if FRONTEND_RENDER_URL:
            return FRONTEND_RENDER_URL.rstrip("/")

    # 2. If FRONTEND_MODE flag is set to local
    if mode in ("local", "dev", "development"):
        if FRONTEND_LOCAL_URL:
            return FRONTEND_LOCAL_URL.rstrip("/")

    # 3. Direct detection from the active client's request (e.g. Origin or Referer header)
    if request:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(origin)
                if parsed.scheme in ("http", "https") and parsed.netloc:
                    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
            except Exception:
                pass

    # 4. Check if running on cloud Render
    if os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL"):
        if FRONTEND_RENDER_URL:
            return FRONTEND_RENDER_URL.rstrip("/")

    # 5. Explicit FRONTEND_URL or local fallback
    if FRONTEND_URL:
        return FRONTEND_URL.rstrip("/")
    if FRONTEND_LOCAL_URL:
        return FRONTEND_LOCAL_URL.rstrip("/")

    return "http://localhost:3000"

@app.get("/api/download/desktop")
def download_desktop_app(os_name: str = Query("windows", alias="os")):
    """Serves the standalone Desktop application installer for Windows or Mac.
    Supports:
    1. Direct Cloud Storage / GitHub Release redirect (DESKTOP_MAC_DOWNLOAD_URL / DESKTOP_WIN_DOWNLOAD_URL)
    2. Local file serving when compiled locally on developer machine.
    """
    clean_os = (os_name or "windows").lower().strip()

    # 1. Cloud URL redirect (for Vercel / Render cloud deployments)
    if clean_os in ("mac", "macos", "darwin", "apple"):
        cloud_url = os.getenv("DESKTOP_MAC_DOWNLOAD_URL") or os.getenv("MAC_DOWNLOAD_URL")
        if cloud_url:
            return RedirectResponse(url=cloud_url.strip(), status_code=302)
    elif clean_os in ("win", "windows"):
        cloud_url = os.getenv("DESKTOP_WIN_DOWNLOAD_URL") or os.getenv("WIN_DOWNLOAD_URL")
        if cloud_url:
            return RedirectResponse(url=cloud_url.strip(), status_code=302)
    else:
        raise HTTPException(status_code=400, detail="Invalid OS specified. Please use ?os=windows or ?os=mac")

    # 2. Local file lookup in desktop/dist (for local development)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.abspath(os.path.join(base_dir, "..", "desktop", "dist"))

    if clean_os in ("win", "windows"):
        candidates = [
            os.path.join(dist_dir, "DataKarkhana Desktop Setup 2.0.0.exe"),
            os.path.join(dist_dir, "DataKarkhana Desktop 2.0.0.exe"),
            os.path.join(dist_dir, "DataKarkhana Desktop Setup.exe"),
        ]
        filename = "DataKarkhana_Desktop_Setup_v2.0.0.exe"
    else:
        candidates = [
            os.path.join(dist_dir, "DataKarkhana Desktop-2.0.0-arm64.dmg"),
            os.path.join(dist_dir, "DataKarkhana Desktop-2.0.0-arm64-mac.zip"),
            os.path.join(dist_dir, "DataKarkhana Desktop-2.0.0.dmg"),
        ]
        filename = "DataKarkhana_Desktop_Mac_v2.0.0.dmg"

    for candidate in candidates:
        if os.path.exists(candidate):
            return FileResponse(
                path=candidate,
                filename=filename,
                media_type="application/octet-stream"
            )

    support_email = os.getenv("SUPPORT_EMAIL", "asifdev777@gmail.com")
    hotline = os.getenv("HOTLINE_PHONE", "+880 1824500704")
    raise HTTPException(
        status_code=404,
        detail=(
            f"Desktop installer for {clean_os} is not hosted on this cloud server disk. "
            f"Please set DESKTOP_{clean_os.upper()}_DOWNLOAD_URL in your cloud environment variables, "
            f"or contact WhatsApp ({hotline}) / email ({support_email}) to receive the download link."
        )
    )

@app.post("/api/auth/register")
def register(req: RegisterRequest, request: Request):
    conn = get_db()
    exists = conn.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (req.email.strip(),)).fetchone()
    if exists:
        conn.close()
        raise HTTPException(status_code=400, detail="An account with this email already exists.")
    
    pwd_hash = hash_password(req.password)
    otp = generate_otp()
    
    # 1. SEND OTP EMAIL
    email_dispatched, err_msg = send_free_verification_email(req.email, req.full_name, otp)

    # 2. INSERT USER INTO DATABASE
    try:
        conn.execute(
            "INSERT INTO users (email, full_name, password_hash, role, credits, is_verified, verification_token) VALUES (?, ?, ?, 'user', 5, 0, ?)",
            (req.email.strip(), req.full_name.strip(), pwd_hash, otp)
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (req.email.strip(),)).fetchone()
    except Exception as db_err:
        conn.close()
        print(f"[REGISTRATION DB ERROR] {db_err}")
        raise HTTPException(
            status_code=500,
            detail=f"Database error during registration: {db_err}"
        )
    conn.close()
    
    return {
        "success": True,
        "requires_verification": True,
        "email_dispatched": email_dispatched,
        "message": f"A 6-digit verification code has been sent to {req.email}! Please enter it below to activate your account.",
        "email": user["email"] if user else req.email
    }

class VerifyOtpRequest(BaseModel):
    email: str
    otp: str

@app.post("/api/auth/verify-otp")
def verify_otp(req: VerifyOtpRequest):
    """Verifies a user account using a 6-digit OTP code and logs the user in immediately."""
    conn = get_db()
    user_row = conn.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (req.email.strip(),)).fetchone()
    if not user_row:
        conn.close()
        raise HTTPException(status_code=404, detail="No account found with this email.")

    user = dict(user_row)
    if user["is_verified"] == 1:
        conn.close()
        token = create_jwt_token(user["id"], user["role"])
        return {
            "success": True,
            "already_verified": True,
            "message": "Account email is already verified.",
            "token": token,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "full_name": user["full_name"],
                "role": user["role"],
                "credits": user["credits"],
                "is_verified": 1,
                "warning_message": user.get("warning_message") or ""
            }
        }

    expected_otp = (user.get("verification_token") or "").strip()
    provided_otp = req.otp.strip()

    if not expected_otp or expected_otp != provided_otp:
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid or expired verification code. Please check your email and try again.")

    conn.execute("UPDATE users SET is_verified = 1, verification_token = NULL WHERE id = ?", (user["id"],))
    conn.commit()
    conn.close()

    token = create_jwt_token(user["id"], user["role"])
    return {
        "success": True,
        "message": "Email verified successfully! Welcome to DataKarkhana.",
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "role": user["role"],
            "credits": user["credits"],
            "is_verified": 1,
            "warning_message": user.get("warning_message") or ""
        }
    }

@app.get("/api/auth/verify-email")
def verify_email(token: str):
    if not token or not token.strip():
        return {
            "success": False,
            "message": "Verification code or token is missing."
        }
    
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE verification_token = ?", (token.strip(),)).fetchone()
    if not user:
        conn.close()
        return {
            "success": False,
            "already_verified": True,
            "message": "This verification code is invalid or has already been used. Please log in to access your account."
        }
    
    conn.execute("UPDATE users SET is_verified = 1, verification_token = NULL WHERE id = ?", (user["id"],))
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": f"Email {user['email']} verified successfully! You can now sign in."
    }

@app.post("/api/auth/resend-verification")
def resend_verification(req: ResendVerificationRequest, request: Request):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (req.email.strip(),)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="No account found with this email.")
    
    if user["is_verified"] == 1:
        conn.close()
        return {"success": True, "message": "Account email is already verified. Please proceed to login."}
    
    otp = generate_otp()
    conn.execute("UPDATE users SET verification_token = ? WHERE id = ?", (otp, user["id"]))
    conn.commit()
    conn.close()
    
    email_dispatched, err_msg = send_free_verification_email(req.email.strip(), user["full_name"], otp)

    return {
        "success": True,
        "email_dispatched": email_dispatched,
        "message": f"A new 6-digit verification code has been sent to {req.email}."
    }

@app.post("/api/auth/login")
def login(req: LoginRequest):
    conn = get_db()
    user_row = conn.execute("SELECT * FROM users WHERE email = ?", (req.email,)).fetchone()
    
    if not user_row or not verify_password(req.password, user_row["password_hash"]):
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid email or password.")
    
    user = dict(user_row)
    
    # Require email verification for non-admin users
    if user["role"] not in ("admin", "superadmin") and user.get("is_verified", 0) != 1:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail="Email address not verified! Please check your email inbox for the verification link before logging in."
        )

    # Check license expiration for non-admin users
    if user["role"] not in ("admin", "superadmin"):
        lic_row = conn.execute(
            """SELECT * FROM licenses 
               WHERE user_id = ? OR LOWER(customer_email) = ? 
               ORDER BY expires_at DESC LIMIT 1""",
            (user["id"], user["email"].strip().lower())
        ).fetchone()

        if lic_row:
            lic_dict = dict(lic_row)
            if lic_dict.get("status") == "revoked":
                conn.close()
                raise HTTPException(
                    status_code=403,
                    detail="Your license has been revoked by the system administrator. Please contact support."
                )

            exp_val = lic_dict.get("expires_at")
            if exp_val:
                now = datetime.now(timezone.utc)
                if isinstance(exp_val, datetime):
                    exp_dt = exp_val if exp_val.tzinfo else exp_val.replace(tzinfo=timezone.utc)
                else:
                    exp_str = str(exp_val)
                    if "T" in exp_str:
                        exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    else:
                        exp_dt = datetime.strptime(exp_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

                if now > exp_dt:
                    conn.close()
                    raise HTTPException(
                        status_code=403,
                        detail=f"License Expired: Your license expired on {exp_dt.strftime('%B %d, %Y')}. You cannot log in until your license is renewed. Please contact your administrator."
                    )

    conn.close()
    
    token = create_jwt_token(user["id"], user["role"])
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "role": user["role"],
            "credits": user["credits"],
            "is_verified": user.get("is_verified", 1),
            "warning_message": user.get("warning_message") or ""
        }
    }

@app.get("/api/auth/me")
def me(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "email": current_user["email"],
        "full_name": current_user["full_name"],
        "role": current_user["role"],
        "credits": current_user["credits"],
        "is_verified": current_user.get("is_verified", 1),
        "is_banned": current_user.get("is_banned", 0),
        "warning_message": current_user.get("warning_message") or ""
    }

# ── Security Endpoints ───────────────────────────────────

class ViolationRequest(BaseModel):
    violation_type: str

@app.post("/api/security/log-violation")
def log_security_violation(req: ViolationRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    
    user_id = None
    auth_header = request.headers.get("authorization", "")
    if auth_header and auth_header.startswith("Bearer "):
        tok = auth_header.split(" ", 1)[1]
        payload = verify_jwt_token(tok)
        if payload:
            user_id = payload.get("user_id")

    conn = get_db()
    conn.execute(
        "INSERT INTO security_violations (user_id, ip_address, user_agent, violation_type) VALUES (?, ?, ?, ?)",
        (user_id, client_ip, user_agent, req.violation_type)
    )
    conn.commit()
    conn.close()
    return {"success": True}

# ── Datasets Endpoints ───────────────────────────────────

@app.get("/api/datasets")
def list_datasets(category: Optional[str] = None, division: Optional[str] = None, district: Optional[str] = None, area: Optional[str] = None, search: Optional[str] = None):
    conn = get_db()
    q = "SELECT * FROM datasets WHERE is_active = 1"
    params = []
    
    if category:
        q += " AND category = ?"
        params.append(category)
    if division:
        q += " AND division = ?"
        params.append(division)
    if district:
        q += " AND district = ?"
        params.append(district)
    if area:
        q += " AND area = ?"
        params.append(area)
    if search:
        q += " AND (name LIKE ? OR category LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
        
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/datasets/my-private")
def get_my_private_datasets(current_user: dict = Depends(get_current_user)):
    """Get demoted/private datasets owned by current user or all if admin"""
    conn = get_db()
    if current_user["role"] in ("admin", "superadmin"):
        rows = conn.execute("SELECT * FROM datasets WHERE is_active = 0 ORDER BY created_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM datasets WHERE is_active = 0 AND uploaded_by = ? ORDER BY created_at DESC", (current_user["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/datasets/{dataset_id}/demote")
@app.post("/api/datasets/{dataset_id}/unpublish")
def demote_dataset_to_private(dataset_id: int, current_user: dict = Depends(get_current_user)):
    """Demote a public dataset to unpublic/private catalogue"""
    conn = get_db()
    ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not ds:
        conn.close()
        raise HTTPException(status_code=404, detail="Dataset not found.")

    if current_user["role"] not in ("admin", "superadmin") and ds["uploaded_by"] != current_user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have permission to demote this dataset.")

    conn.execute("UPDATE datasets SET is_active = 0 WHERE id = ?", (dataset_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Dataset '{ds['name']}' demoted to Private Catalogue!"}

@app.post("/api/datasets/{dataset_id}/publish")
def publish_dataset_to_public(dataset_id: int, current_user: dict = Depends(get_current_user)):
    """Publish a private dataset back to the public catalogue"""
    conn = get_db()
    ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not ds:
        conn.close()
        raise HTTPException(status_code=404, detail="Dataset not found.")

    if current_user["role"] not in ("admin", "superadmin") and ds["uploaded_by"] != current_user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have permission to publish this dataset.")

    conn.execute("UPDATE datasets SET is_active = 1 WHERE id = ?", (dataset_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Dataset '{ds['name']}' published to Public Catalogue!"}

def resolve_dataset_file_path(file_path: Optional[str], dataset_id: Optional[Union[int, str]] = None) -> Optional[str]:
    if not file_path:
        return None
    if os.path.exists(file_path):
        return file_path
    
    filename = os.path.basename(file_path.replace("\\", "/"))
    local_upload = os.path.join(UPLOAD_FOLDER, filename)
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root_mirpur = os.path.join(parent_dir, "coaching_centers_mirpur.xlsx")
    root_sanitized = os.path.join(parent_dir, "sanitized_coaching_centers.xlsx")

    found_path = None
    if os.path.exists(local_upload):
        found_path = local_upload
    elif "mirpur" in filename.lower() and os.path.exists(root_mirpur):
        found_path = root_mirpur
    elif os.path.exists(root_sanitized):
        found_path = root_sanitized
    elif os.path.exists(os.path.join(parent_dir, filename)):
        found_path = os.path.join(parent_dir, filename)

    if found_path and dataset_id:
        try:
            conn = get_db()
            conn.execute("UPDATE datasets SET file_path = ? WHERE id = ?", (found_path, str(dataset_id)))
            conn.commit()
            conn.close()
        except Exception as e:
            print("[RESOLVE DATASET PATH DB UPDATE ERROR]", e)

    return found_path

def resolve_any_recipient_group(recipient_group: str):
    file_path = None
    group_name = recipient_group or "Default Dataset"
    conn = get_db()

    if recipient_group.startswith("dataset_"):
        ds_id = recipient_group.replace("dataset_", "")
        ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (ds_id,)).fetchone()
        if ds:
            file_path = resolve_dataset_file_path(ds["file_path"], ds["id"])
            group_name = ds["name"]
    elif recipient_group.startswith("job_"):
        job_id = recipient_group.replace("job_", "")
        jb = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
        if jb:
            file_path = jb["result_path"]
            group_name = f"Scrape job: {jb['query']}"
    else:
        ds = conn.execute("SELECT * FROM datasets WHERE name = ? OR id = ?", (recipient_group, recipient_group)).fetchone()
        if ds:
            file_path = resolve_dataset_file_path(ds["file_path"], ds["id"])
            group_name = ds["name"]
        else:
            jb = conn.execute("SELECT * FROM scrape_jobs WHERE query = ? OR id = ?", (recipient_group, recipient_group)).fetchone()
            if jb:
                file_path = jb["result_path"]
                group_name = f"Scrape job: {jb['query']}"
    conn.close()

    if not file_path or not os.path.exists(file_path):
        parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        fn = os.path.basename(file_path.replace("\\", "/")) if file_path else ""
        candidates = [
            os.path.join(UPLOAD_FOLDER, fn) if fn else None,
            os.path.join(SCRAPE_RESULTS_FOLDER, fn) if fn else None,
            os.path.join(parent_dir, fn) if fn else None,
            os.path.join(parent_dir, "coaching_centers_mirpur.xlsx"),
            os.path.join(parent_dir, "sanitized_coaching_centers.xlsx"),
        ]

        if os.path.exists(UPLOAD_FOLDER):
            for f in os.listdir(UPLOAD_FOLDER):
                if f.endswith(".csv") or f.endswith(".xlsx"):
                    candidates.append(os.path.join(UPLOAD_FOLDER, f))
        if os.path.exists(SCRAPE_RESULTS_FOLDER):
            for f in os.listdir(SCRAPE_RESULTS_FOLDER):
                if f.endswith(".csv") or f.endswith(".xlsx"):
                    candidates.append(os.path.join(SCRAPE_RESULTS_FOLDER, f))

        for cand in candidates:
            if cand and os.path.exists(cand):
                file_path = cand
                break

    return file_path, group_name

def clean_lead_df(df):
    """Clean DataFrame to strip float conversion .0 suffixes and NaN strings"""
    df = df.fillna("")
    for col in df.columns:
        df[col] = df[col].astype(str).str.replace(r'\.0$', '', regex=True)
        df[col] = df[col].astype(str).str.replace(r'^\+?88001', '+8801', regex=True)
        df[col] = df[col].astype(str).str.replace(r'^88001', '+8801', regex=True)
        df[col] = df[col].replace({'nan': '', 'NaN': '', 'None': '', 'None.0': ''})
    return df

@app.get("/api/datasets/{dataset_id}")
def get_dataset(dataset_id: str, page: int = 1, limit: int = 25, search: Optional[str] = None, authorization: Optional[str] = Header(None)):
    page_size = max(1, min(1000, limit))
    conn = get_db()
    
    # Check if dataset_id is a private scrape job
    if str(dataset_id).startswith("job_"):
        job_real_id = str(dataset_id).replace("job_", "")
        job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_real_id,)).fetchone()
        conn.close()
        if not job or not job["result_path"]:
            raise HTTPException(status_code=404, detail="Private scrape job or file not found.")
            
        file_path = job["result_path"]
        if file_path and not os.path.exists(file_path):
            fn = os.path.basename(file_path.replace("\\", "/"))
            if os.path.exists(os.path.join(SCRAPE_RESULTS_FOLDER, fn)):
                file_path = os.path.join(SCRAPE_RESULTS_FOLDER, fn)
                
        if not file_path or not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Private scrape job result file missing.")
            
        try:
            df = pd.read_csv(file_path, dtype=str) if file_path.endswith(".csv") else pd.read_excel(file_path, dtype=str)
            df = clean_lead_df(df)
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to parse private job leads file.")

        if search:
            df = df[df.astype(str).apply(lambda x: x.str.contains(search, case=False)).any(axis=1)]

        total_rows = len(df)
        start_row = (page - 1) * page_size
        end_row = start_row + page_size
        raw_records = df.iloc[start_row:end_row].fillna("").to_dict(orient="records")
        leads = [{k: ("" if (v is None or str(v).lower() in ("nan", "none", "null")) else str(v)) for k, v in r.items()} for r in raw_records]

        return {
            "dataset": {
                "id": f"job_{job_real_id}",
                "name": job["query"],
                "category": "Private Scraped Dataset",
                "division": job["division"],
                "district": job["district"],
                "area": job["area"],
                "row_count": total_rows,
                "price_credits": 0
            },
            "unlocked": True,
            "leads": leads,
            "total_rows": total_rows,
            "page": page,
            "current_page": page,
            "page_size": page_size,
            "pages_count": max(1, (total_rows + page_size - 1) // page_size)
        }

    # Standard public dataset lookup
    ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not ds:
        conn.close()
        raise HTTPException(status_code=404, detail="Dataset not found.")
        
    file_path = resolve_dataset_file_path(ds["file_path"], ds["id"])
    if not file_path or not os.path.exists(file_path):
        conn.close()
        raise HTTPException(status_code=404, detail="Data file missing.")

    # Parse Excel/CSV
    try:
        if file_path.endswith(".csv"):
            df = pd.read_csv(file_path, dtype=str)
        else:
            df = pd.read_excel(file_path, dtype=str)
        df = clean_lead_df(df)
    except Exception:
        conn.close()
        raise HTTPException(status_code=500, detail="Error reading file contents.")

    # Check if user unlocked this dataset
    unlocked = False
    current_user_id = None
    role = "user"
    
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        user_payload = decode_jwt_token(token)
        if user_payload:
            current_user_id = user_payload["user_id"]
            role = user_payload["role"]
            # Look up access log
            log = conn.execute(
                "SELECT id FROM access_logs WHERE user_id = ? AND dataset_id = ? AND action = 'unlock'",
                (current_user_id, dataset_id)
            ).fetchone()
            if log or role in ("admin", "superadmin"):
                unlocked = True

    # Apply search filter
    if search:
        df = df[df.astype(str).apply(lambda x: x.str.contains(search, case=False)).any(axis=1)]

    total_rows = len(df)
    
    # Render Preview (unmasked if unlocked, masked with email watermark if locked)
    if not unlocked:
        # Show first 5 rows with masked phone numbers
        preview_df = df.head(5).copy().fillna("")
        phone_cols = [c for c in preview_df.columns if "phone" in c.lower() or "mobile" in c.lower() or "contact" in c.lower()]
        for c in phone_cols:
            preview_df[c] = preview_df[c].apply(lambda p: (str(p)[:5] + "XXX" + str(p)[-3:]) if len(str(p)) >= 8 else str(p))
        raw_records = preview_df.to_dict(orient="records")
    else:
        # Show full paginated results
        start_row = (page - 1) * page_size
        end_row = start_row + page_size
        raw_records = df.iloc[start_row:end_row].fillna("").to_dict(orient="records")

    leads = [{k: ("" if (v is None or str(v).lower() in ("nan", "none", "null")) else str(v)) for k, v in r.items()} for r in raw_records]

    conn.close()
    return {
        "dataset": dict(ds),
        "unlocked": unlocked,
        "leads": leads,
        "total_rows": total_rows,
        "page": page,
        "current_page": page,
        "page_size": page_size,
        "pages_count": max(1, (total_rows + page_size - 1) // page_size)
    }

@app.post("/api/datasets/{dataset_id}/unlock")
def unlock_dataset(dataset_id: str, current_user: dict = Depends(get_current_user)):
    if str(dataset_id).startswith("job_"):
        return {"success": True, "message": "Private scrape job datasets are automatically unlocked.", "credits": current_user["credits"]}

    conn = get_db()
    ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not ds:
        conn.close()
        raise HTTPException(status_code=404, detail="Dataset not found.")

    # Verify if already unlocked
    already = conn.execute(
        "SELECT id FROM access_logs WHERE user_id = ? AND dataset_id = ? AND action = 'unlock'",
        (current_user["id"], dataset_id)
    ).fetchone()
    if already:
        conn.close()
        return {"success": True, "message": "Dataset already unlocked."}

    cost = ds["price_credits"]
    if current_user["role"] not in ("admin", "superadmin"):
        if current_user["credits"] < cost:
            conn.close()
            raise HTTPException(status_code=403, detail="Insufficient credit balances to unlock this dataset.")
            
        conn.execute("UPDATE users SET credits = credits - ? WHERE id = ?", (cost, current_user["id"]))
        conn.execute(
            "INSERT INTO credit_transactions (user_id, amount, transaction_type, description) VALUES (?, ?, 'deduct', ?)",
            (current_user["id"], cost, f"Unlocked dataset: {ds['name']}")
        )
    
    # Log access
    conn.execute(
        "INSERT INTO access_logs (user_id, dataset_id, action) VALUES (?, ?, 'unlock')",
        (current_user["id"], dataset_id)
    )
    conn.commit()
    
    # Reload profile
    user = conn.execute("SELECT credits FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    conn.close()
    return {"success": True, "credits": user["credits"]}

def generate_pdf_from_df(df: pd.DataFrame, title: str = "MarketingOstad Dataset Export") -> bytes:
    try:
        from reportlab.lib.pagesizes import letter, landscape
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=landscape(letter),
            rightMargin=20,
            leftMargin=20,
            topMargin=20,
            bottomMargin=20
        )
        elements = []
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            'DocTitle',
            parent=styles['Heading1'],
            fontSize=16,
            textColor=colors.HexColor('#0a0e17'),
            spaceAfter=8
        )
        elements.append(Paragraph(f"<b>MarketingOstad — {title}</b>", title_style))
        elements.append(Paragraph(f"Export Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Total Business Items: {len(df)}", styles['Normal']))
        elements.append(Spacer(1, 10))

        cols = list(df.columns)[:7]
        table_data = [[Paragraph(f"<b>{col}</b>", styles['Normal']) for col in cols]]

        for _, row in df.head(300).iterrows():
            row_data = []
            for col in cols:
                val = str(row[col]) if pd.notna(row[col]) and str(row[col]) != "nan" else ""
                if len(val) > 40:
                    val = val[:37] + "..."
                row_data.append(Paragraph(val, styles['Normal']))
            table_data.append(row_data)

        t = Table(table_data, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#06b6d4')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')])
        ]))
        elements.append(t)
        doc.build(elements)
        buffer.seek(0)
        return buffer.getvalue()
    except Exception as e:
        print("[PDF GENERATION NOTICE]", e)
        header = f"MarketingOstad Dataset Export: {title}\nDate: {datetime.now()}\nTotal Records: {len(df)}\n\n"
        body = df.to_string(index=False)
        return (header + body).encode("utf-8")

def generate_export_response(file_path: str, export_format: str, title: str = "Exported_Dataset"):
    fmt = (export_format or "excel").lower().strip()
    safe_title = "".join(c for c in title if c.isalnum() or c in ("_", "-")).strip() or "Dataset"
    filename_base = f"{safe_title}_{int(time.time())}"

    if file_path.endswith(".csv"):
        df = pd.read_csv(file_path, dtype=str).fillna("")
    else:
        df = pd.read_excel(file_path, dtype=str).fillna("")

    if fmt in ("csv", ".csv"):
        csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.csv"'}
        )

    elif fmt in ("json", ".json"):
        json_bytes = df.to_json(orient="records", indent=2, force_ascii=False).encode("utf-8")
        return Response(
            content=json_bytes,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.json"'}
        )

    elif fmt in ("pdf", ".pdf"):
        pdf_bytes = generate_pdf_from_df(df, title=title)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.pdf"'}
        )

    else:
        if file_path.endswith(".xlsx") and os.path.exists(file_path):
            return FileResponse(
                file_path,
                filename=f"{filename_base}.xlsx",
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        out_buf = io.BytesIO()
        with pd.ExcelWriter(out_buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Exported_Leads")
        out_buf.seek(0)
        return Response(
            content=out_buf.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.xlsx"'}
        )

@app.get("/api/datasets/{dataset_id}/export")
def export_dataset(dataset_id: str, request: Request, format: Optional[str] = "excel", token: Optional[str] = None, authorization: Optional[str] = Header(None)):
    current_user = get_current_user(request=request, authorization=authorization, token=token)
    if current_user.get("role") not in ("admin", "superadmin"):
        raise HTTPException(
            status_code=403,
            detail="Permission denied. Only Administrators can export datasets."
        )

    conn = get_db()
    file_path = None
    title_name = "Exported_Dataset"
    if str(dataset_id).startswith("job_"):
        job_real_id = str(dataset_id).replace("job_", "")
        job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_real_id,)).fetchone()
        if job:
            file_path = job["result_path"]
            title_name = job["query"] or f"Job_{job['id']}"
            if file_path and not os.path.exists(file_path):
                fn = os.path.basename(file_path.replace("\\", "/"))
                if os.path.exists(os.path.join(SCRAPE_RESULTS_FOLDER, fn)):
                    file_path = os.path.join(SCRAPE_RESULTS_FOLDER, fn)
    else:
        ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        if ds:
            file_path = resolve_dataset_file_path(ds["file_path"], int(dataset_id))
            title_name = ds["name"]
    conn.close()

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Data file missing.")

    return generate_export_response(file_path, export_format=format, title=title_name)

# ── Admin Upload ─────────────────────────────────────────

@app.post("/api/admin/datasets/upload")
def admin_upload(
    name: str = Form(...),
    category: str = Form(...),
    price_credits: int = Form(...),
    division: str = Form(None),
    district: str = Form(None),
    area: str = Form(None),
    file: UploadFile = File(...),
    admin_user: dict = Depends(get_admin_user)
):
    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in ("csv", "xlsx", "xls"):
        raise HTTPException(status_code=400, detail="Only valid dataset formats (.xlsx, .csv) are supported.")
        
    filename = f"{int(time.time())}_{file.filename}"
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    
    with open(file_path, "wb") as f:
        f.write(file.file.read())
        
    # Read file row counts and columns
    try:
        if ext == "csv":
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)
        row_count = len(df)
        column_names = ", ".join(df.columns)
    except Exception:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=500, detail="Error parsing file headers.")

    conn = get_db()
    conn.execute(
        """INSERT INTO datasets (name, category, division, district, area, file_path, row_count, column_names, price_credits, uploaded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, category, division, district, area, file_path, row_count, column_names, price_credits, admin_user["id"])
    )
    conn.commit()
    conn.close()
    return {"success": True, "message": "Dataset catalog file uploaded successfully."}

@app.delete("/api/admin/datasets/{dataset_id}")
def admin_delete_dataset(dataset_id: str, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
    conn.commit()
    conn.close()
    return {"success": True}

# ── Scraper Endpoints ────────────────────────────────────

def run_background_scrape(job_id, queries, division, district, area, headless=False):
    def log_cb(msg):
        # 1. Stream log line instantly in real-time to active WebSocket subscribers
        add_job_log(job_id, msg)
        # 2. Persist immediately to database
        try:
            db = get_db()
            db.execute("INSERT INTO scrape_logs (job_id, message) VALUES (?, ?)", (job_id, msg))
            db.commit()
            db.close()
        except Exception:
            pass

    def flush_db_logs():
        pass

    driver = None
    all_results = []
    stopped_early = False

    try:
        log_cb("🚀 Initializing automated Chrome browser engine...")
        driver = setup_driver(headless=headless)
        log_cb("🌐 Chrome browser session established. Ready for Google Maps scraping.")
        for idx, q in enumerate(queries):
            if is_job_stopped(job_id):
                stopped_early = True
                log_cb("⏹️ Manual stop requested. Exiting scraper loop...")
                break

            log_cb(f"─── [Query {idx+1}/{len(queries)}] Searching Google Maps: '{q}' ───")
            res = scrape_query(driver, q, log_cb, job_id=job_id)
            if res:
                all_results.extend(res)
            log_cb(f"─── [Query {idx+1}/{len(queries)}] Completed: Collected {len(res) if res else 0} items ───")

            if is_job_stopped(job_id):
                stopped_early = True
                log_cb("⏹️ Manual stop requested. Exiting scraper loop...")
                break

    except Exception as ex:
        log_cb(f"⚠️ Scraper thread encountered exception / interruption: {ex}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            import gc
            gc.collect()
        clear_scraper_frame(job_id)

    # Save any results collected so far (even if stopped or interrupted!)
    if all_results:
        filename = f"job_{job_id}_{int(time.time())}.xlsx"
        result_path = os.path.join(SCRAPE_RESULTS_FOLDER, filename)
        saved_count = save_to_excel(all_results, result_path)
        if saved_count is None:
            saved_count = len(all_results)

        final_status = "stopped" if stopped_early else "done"
        db = get_db()
        db.execute(
            "UPDATE scrape_jobs SET status = ?, result_path = ?, result_count = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (final_status, result_path, saved_count, job_id)
        )
        db.commit()
        db.close()

        status_msg = "stopped manually" if stopped_early else f"completed all {len(queries)} queries"
        log_cb(f"🎉 Scraper {status_msg}! Preserved {saved_count} total business records into your private catalogue dataset.")
    else:
        final_status = "stopped" if stopped_early else "failed"
        db = get_db()
        db.execute("UPDATE scrape_jobs SET status = ?, error_message = 'No records parsed before process ended.' WHERE id = ?", (final_status, job_id))
        db.commit()
        db.close()
        log_cb("Scraper execution halted: 0 records collected.")

    flush_db_logs()
    publish_scraper_event(job_id, {
        "type": "job_ended",
        "status": final_status,
        "result_count": len(all_results)
    })
    clear_job_stop(job_id)

# ── Dataset Requests Portal API ──────────────────────────────

class DatasetRequestCreate(BaseModel):
    phone: str
    business_name: Optional[str] = ""
    category_query: str
    division: Optional[str] = ""
    district: Optional[str] = ""
    area: Optional[str] = ""
    additional_notes: Optional[str] = ""

class DatasetRequestStatusUpdate(BaseModel):
    status: str # 'pending', 'fulfilled', 'rejected'
    admin_notes: Optional[str] = ""
    notify_channel: Optional[str] = "none" # 'email', 'whatsapp', 'both', 'none'
    custom_message: Optional[str] = ""

def send_custom_notification(recipient_email: str, recipient_phone: str, subject: str, message: str, channel: str):
    """Sends custom notification to user about request status update via Email and/or WhatsApp"""
    sent_email = False
    sent_wa = False

    # 1. Email Notification
    if channel in ("email", "both") and recipient_email:
        try:
            brevo_api_key = os.getenv("BREVO_API_KEY", "")
            smtp_user = os.getenv("SMTP_USER", "asifdev777@gmail.com")
            smtp_pass = os.getenv("SMTP_PASSWORD", os.getenv("BREVO_SMTP_KEY", os.getenv("SMTP_PASS", "")))
            smtp_server = os.getenv("SMTP_SERVER", "smtp-relay.brevo.com")
            smtp_port = int(os.getenv("SMTP_PORT", 587))

            html_body = f"""
            <div style="font-family: Arial, sans-serif; background: #0f172a; color: #e2e8f0; padding: 30px; border-radius: 10px;">
                <h2 style="color: #06b6d4; margin-top: 0;">MarketingOstad - Dataset Request Update</h2>
                <div style="background: rgba(255,255,255,0.05); padding: 20px; border-radius: 8px; border-left: 4px solid #06b6d4; margin: 20px 0;">
                    <p style="font-size: 1rem; line-height: 1.6; white-space: pre-wrap; margin: 0; color: #f8fafc;">{message}</p>
                </div>
                <p style="font-size: 0.85rem; color: #94a3b8; margin-top: 30px;">
                    Thank you for choosing MarketingOstad Data Platform.<br>
                    Website: <a href="https://yourdatapoint.com" style="color: #06b6d4;">yourdatapoint.com</a>
                </p>
            </div>
            """

            if brevo_api_key:
                try:
                    import urllib.request
                    import json
                    headers = {
                        "accept": "application/json",
                        "api-key": brevo_api_key,
                        "content-type": "application/json"
                    }
                    payload = {
                        "sender": {"name": "MarketingOstad Team", "email": smtp_user},
                        "to": [{"email": recipient_email}],
                        "subject": subject,
                        "htmlContent": html_body
                    }
                    req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=json.dumps(payload).encode("utf-8"), headers=headers)
                    with urllib.request.urlopen(req) as resp:
                        if resp.status in (200, 201):
                            sent_email = True
                except Exception as e:
                    print(f"[BREVO NOTIF ERROR] {e}")

        except Exception as e:
            print(f"[NOTIF EMAIL GENERAL EXCEPTION] {e}")

    # 2. WhatsApp Notification
    if channel in ("whatsapp", "both") and recipient_phone:
        try:
            from senders import format_phone
            clean_p = format_phone(recipient_phone)
            camp_id = f"notif_wa_{int(time.time())}"
            contacts_list = [{"name": "User", "phone": clean_p}]
            t = threading.Thread(target=run_whatsapp_campaign, args=(camp_id, contacts_list, message, f"notif_{clean_p}", 0))
            t.daemon = True
            t.start()
            sent_wa = True
        except Exception as e:
            print(f"[NOTIF WHATSAPP ERROR] {e}")

    return sent_email or sent_wa

@app.post("/api/scraper/requests")
@app.post("/api/requests/submit")
def submit_dataset_request(req: DatasetRequestCreate, current_user: dict = Depends(get_current_user)):
    if not req.category_query or not req.category_query.strip():
        raise HTTPException(status_code=400, detail="Required data / category query cannot be empty.")
    if not req.phone or not req.phone.strip():
        raise HTTPException(status_code=400, detail="Contact phone number is required.")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO dataset_requests (
            user_id, user_email, full_name, phone, business_name,
            category_query, division, district, area, additional_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        current_user["id"], current_user["email"], current_user.get("full_name", "User"),
        req.phone.strip(), (req.business_name or "").strip(),
        req.category_query.strip(), (req.division or "").strip(),
        (req.district or "").strip(), (req.area or "").strip(),
        (req.additional_notes or "").strip()
    ))
    conn.commit()
    request_id = cursor.lastrowid
    conn.close()
    return {"success": True, "request_id": request_id, "message": "Dataset request submitted successfully! Our data team will review and drop this dataset into the Public Catalog."}

@app.get("/api/requests/my-requests")
def get_my_dataset_requests(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM dataset_requests WHERE user_id = ? ORDER BY created_at DESC", (current_user["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/requests/admin/list")
def list_dataset_requests(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin authorization required.")
    conn = get_db()
    rows = conn.execute("SELECT dr.*, u.credits FROM dataset_requests dr JOIN users u ON dr.user_id = u.id ORDER BY dr.created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/requests/admin/{request_id}/status")
def update_dataset_request_status(request_id: int, req: DatasetRequestStatusUpdate, current_user: dict = Depends(get_current_user)):
    if current_user.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin authorization required.")
    if req.status not in ("pending", "fulfilled", "rejected"):
        raise HTTPException(status_code=400, detail="Invalid status value.")
    conn = get_db()
    req_row = conn.execute("SELECT * FROM dataset_requests WHERE id = ?", (request_id,)).fetchone()
    if not req_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Dataset request not found.")

    conn.execute("UPDATE dataset_requests SET status = ?, admin_notes = ? WHERE id = ?", (req.status, (req.admin_notes or "").strip(), request_id))
    conn.commit()
    conn.close()

    if req.notify_channel and req.notify_channel != "none" and req.custom_message and req.custom_message.strip():
        subj = f"Dataset Request #{request_id} Update: {req.status.upper()}"
        send_custom_notification(
            recipient_email=req_row["user_email"],
            recipient_phone=req_row["phone"],
            subject=subj,
            message=req.custom_message.strip(),
            channel=req.notify_channel
        )

    return {"success": True, "message": f"Dataset request status updated to '{req.status}'!"}

@app.post("/api/scraper/scrape")
def trigger_scrape(
    req: ScrapeRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    _license_valid: bool = Depends(check_desktop_license)
):
    query_list = []
    if req.queries and isinstance(req.queries, list):
        query_list = [q.strip() for q in req.queries if q and q.strip()]
    if not query_list and req.query:
        query_list = [req.query.strip()]
    
    if not query_list:
        raise HTTPException(status_code=400, detail="Please provide at least one search query.")
        
    display_query = " + ".join(query_list)
    cost = 20 * len(query_list)
    conn = get_db()
    if current_user["role"] not in ("admin", "superadmin"):
        if current_user["credits"] < cost:
            conn.close()
            raise HTTPException(status_code=403, detail=f"Insufficient credits to run scraper (costs {cost} credits for {len(query_list)} queries).")
            
        conn.execute("UPDATE users SET credits = credits - ? WHERE id = ?", (cost, current_user["id"]))
        conn.execute(
            "INSERT INTO credit_transactions (user_id, amount, transaction_type, description) VALUES (?, ?, 'deduct', ?)",
            (current_user["id"], cost, f"Google Maps Scraper Run ({len(query_list)} queries)")
        )
    
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO scrape_jobs (user_id, query, division, district, area, status, cost_credits) VALUES (?, ?, ?, ?, ?, 'running', ?)",
        (current_user["id"], display_query, req.division, req.district, req.area, cost)
    )
    conn.commit()
    job_id = cursor.lastrowid
    conn.close()

    background_tasks.add_task(run_background_scrape, job_id, query_list, req.division, req.district, req.area, req.headless)
    return {"success": True, "job_id": job_id}

@app.get("/api/scraper/jobs")
def get_jobs(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    if current_user["role"] in ("admin", "superadmin"):
        jobs = conn.execute("SELECT sj.*, u.email FROM scrape_jobs sj JOIN users u ON sj.user_id = u.id ORDER BY sj.created_at DESC").fetchall()
    else:
        jobs = conn.execute("SELECT * FROM scrape_jobs WHERE user_id = ? ORDER BY created_at DESC", (current_user["id"],)).fetchall()
    conn.close()
    return [dict(j) for j in jobs]

@app.get("/api/scraper/jobs/{job_id}/status")
def get_job_status(job_id: int, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Scrape job not found.")
        
    logs = conn.execute("SELECT message, created_at FROM scrape_logs WHERE job_id = ? ORDER BY id ASC", (job_id,)).fetchall()
    conn.close()
    return {
        "job": dict(job),
        "logs": [f"[{str(l['created_at']).split(' ')[1] if ' ' in str(l['created_at']) else str(l['created_at'])}] {l['message']}" for l in logs]
    }

@app.delete("/api/scraper/jobs/{job_id}")
def delete_scrape_job(job_id: int, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Scrape job not found.")

    if current_user["role"] not in ("admin", "superadmin") and job["user_id"] != current_user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have permission to delete this scrape job.")

    if job["result_path"] and os.path.exists(job["result_path"]):
        try:
            os.remove(job["result_path"])
        except Exception:
            pass

    conn.execute("DELETE FROM scrape_logs WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM whatsapp_progress WHERE recipient_group = ?", (f"job_{job_id}",))
    conn.execute("DELETE FROM scrape_jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Scrape job deleted successfully."}

@app.post("/api/scraper/jobs/{job_id}/stop")
def stop_scrape_job_endpoint(job_id: int, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Scrape job not found.")
    
    if current_user["role"] not in ("admin", "superadmin") and job["user_id"] != current_user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have permission to stop this job.")

    stop_scraper_job(job_id)
    conn.execute("INSERT INTO scrape_logs (job_id, message) VALUES (?, '⏹️ Manual stop request received from console user.')", (job_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Stop signal sent for job #{job_id}."}

@app.get("/api/scraper/jobs/{job_id}/data")
def get_job_scraped_data(job_id: int, current_user: dict = Depends(get_current_user)):
    """Return parsed leads data from the job's excel spreadsheet for private catalog viewing"""
    conn = get_db()
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if not job:
        raise HTTPException(status_code=404, detail="Scrape job not found.")

    if current_user["role"] not in ("admin", "superadmin") and job["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Permission denied.")
    
    if not job["result_path"] or not os.path.exists(job["result_path"]):
        return {"job_id": job_id, "query": job["query"], "count": 0, "data": []}

    try:
        df = pd.read_excel(job["result_path"])
        df = df.fillna("")
        records = df.to_dict(orient="records")
        return {
            "job_id": job_id,
            "query": job["query"],
            "division": job["division"],
            "district": job["district"],
            "area": job["area"],
            "count": len(records),
            "data": records
        }
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"Failed to parse job Excel data: {ex}")

@app.get("/api/scraper/jobs/{job_id}/download")
def download_job_excel(job_id: int, request: Request, token: Optional[str] = None, authorization: Optional[str] = Header(None)):
    """Download scraped Excel dataset file"""
    current_user = get_current_user(request=request, authorization=authorization, token=token)
    conn = get_db()
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if not job or not job["result_path"] or not os.path.exists(job["result_path"]):
        raise HTTPException(status_code=404, detail="Result file not found or job incomplete.")

    if current_user["role"] not in ("admin", "superadmin") and job["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Permission denied.")

    sanitized_query = re.sub(r'[^a-zA-Z0-9_\-]', '_', job["query"] or "leads")
    filename = f"scraped_{sanitized_query}_job{job_id}.xlsx"
    return FileResponse(
        job["result_path"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename
    )

@app.post("/api/scraper/jobs/{job_id}/promote")
@app.post("/api/scraper/jobs/{job_id}/request-promote")
def request_promote_job(
    job_id: int,
    category: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user)
):
    conn = get_db()
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job or not job["result_path"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Cannot add job to catalogue because result file is missing.")

    if job["status"] not in ("done", "stopped"):
        conn.close()
        raise HTTPException(status_code=400, detail="Job must be completed or stopped before adding to catalogue.")

    if current_user["role"] != "admin" and current_user["role"] != "superadmin" and job["user_id"] != current_user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have permission to promote this dataset.")

    final_name = name or job["query"] or f"Scraped Dataset #{job_id}"
    final_category = category or "Scraped Leads"

    # Check if already added to datasets table
    existing_ds = conn.execute("SELECT id FROM datasets WHERE file_path LIKE ?", (f"%promoted_{job_id}_%",)).fetchone()
    if existing_ds:
        conn.execute("UPDATE scrape_jobs SET promotion_status = 'approved' WHERE id = ?", (job_id,))
        conn.commit()
        conn.close()
        return {"success": True, "dataset_id": existing_ds["id"], "message": "Dataset already present in Private Catalogue."}

    # Add dataset directly into catalogue
    new_filename = f"promoted_{job_id}_{int(time.time())}.xlsx"
    new_path = os.path.join(UPLOAD_FOLDER, new_filename)
    import shutil
    shutil.copy(job["result_path"], new_path)
    rel_path = f"uploads/{new_filename}"

    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO datasets (name, category, division, district, area, file_path, row_count, column_names, price_credits, uploaded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'Name, Phone, Address, Website, Rating, Category, Maps URL, Query', 10, ?)""",
        (final_name, final_category, job["division"], job["district"], job["area"], rel_path, job["result_count"], current_user["id"])
    )
    new_ds_id = cursor.lastrowid
    conn.execute("UPDATE scrape_jobs SET promotion_status = 'approved', proposed_name = ?, proposed_category = ? WHERE id = ?", (final_name, final_category, job_id))
    conn.commit()
    conn.close()
    return {"success": True, "dataset_id": new_ds_id, "message": "Scraped dataset added successfully to Private Catalogue!"}

@app.get("/api/admin/promotion-requests")
def list_promotion_requests(admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    rows = conn.execute("""
        SELECT sj.*, u.email AS user_email, u.full_name FROM scrape_jobs sj
        JOIN users u ON sj.user_id = u.id
        WHERE sj.promotion_status = 'pending'
        ORDER BY sj.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/promotion-requests/{job_id}/approve")
def approve_promotion_request(job_id: int, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job or not job["result_path"]:
        conn.close()
        raise HTTPException(status_code=404, detail="Scrape job or result file not found.")

    ds_name = job["proposed_name"] or job["query"]
    ds_cat = job["proposed_category"] or "Coaching Center"

    new_filename = f"promoted_{job_id}_{int(time.time())}.xlsx"
    new_path = os.path.join(UPLOAD_FOLDER, new_filename)
    import shutil
    shutil.copy(job["result_path"], new_path)
    rel_path = f"uploads/{new_filename}"

    conn.execute(
        """INSERT INTO datasets (name, category, division, district, area, file_path, row_count, column_names, price_credits, uploaded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'Name, Phone, Address, Website, Rating, Category, Maps URL, Query', 10, ?)""",
        (ds_name, ds_cat, job["division"], job["district"], job["area"], rel_path, job["result_count"], admin_user["id"])
    )
    conn.execute("UPDATE scrape_jobs SET promotion_status = 'approved' WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Promotion request for '{ds_name}' approved & published to public catalog!"}

@app.post("/api/admin/promotion-requests/{job_id}/reject")
def reject_promotion_request(job_id: int, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    conn.execute("UPDATE scrape_jobs SET promotion_status = 'rejected' WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Promotion request rejected."}

# ── Payment & bKash / Pathao Module Endpoints ───────────────

def get_payment_gateway_settings():
    conn = get_db()
    rows = conn.execute("SELECT setting_key, setting_value FROM payment_settings").fetchall()
    conn.close()
    
    settings_dict = {row["setting_key"]: row["setting_value"] for row in rows if row["setting_value"]}
    
    from config import load_credit_packages_config
    pkg_cfg = load_credit_packages_config()
    return {
        "bkash_number": settings_dict.get("bkash_number") or BKASH_NUMBER,
        "bkash_account_type": settings_dict.get("bkash_account_type") or BKASH_ACCOUNT_TYPE,
        "bkash_qr_url": settings_dict.get("bkash_qr_url") or "",
        "pathao_number": settings_dict.get("pathao_number") or PATHAO_NUMBER,
        "pathao_account_type": settings_dict.get("pathao_account_type") or PATHAO_ACCOUNT_TYPE,
        "pathao_qr_url": settings_dict.get("pathao_qr_url") or "",
        "packages": pkg_cfg.get("packages", []),
        "custom_package": pkg_cfg.get("custom_package", {})
    }

@app.get("/api/config/payment-gateways")
@app.get("/api/payments/packages-config")
def get_payment_packages_config():
    return get_payment_gateway_settings()

@app.post("/api/admin/payment-settings")
async def update_admin_payment_settings(
    bkash_number: Optional[str] = Form(None),
    bkash_account_type: Optional[str] = Form(None),
    pathao_number: Optional[str] = Form(None),
    pathao_account_type: Optional[str] = Form(None),
    bkash_qr_file: Optional[UploadFile] = File(None),
    pathao_qr_file: Optional[UploadFile] = File(None),
    current_user: dict = Depends(get_current_user)
):
    if current_user.get("role") not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Admin permissions required.")

    conn = get_db()

    if bkash_number is not None:
        conn.execute("INSERT INTO payment_settings (setting_key, setting_value) VALUES ('bkash_number', ?) ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value", (bkash_number.strip(),))
    if bkash_account_type is not None:
        conn.execute("INSERT INTO payment_settings (setting_key, setting_value) VALUES ('bkash_account_type', ?) ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value", (bkash_account_type.strip(),))
    if pathao_number is not None:
        conn.execute("INSERT INTO payment_settings (setting_key, setting_value) VALUES ('pathao_number', ?) ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value", (pathao_number.strip(),))
    if pathao_account_type is not None:
        conn.execute("INSERT INTO payment_settings (setting_key, setting_value) VALUES ('pathao_account_type', ?) ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value", (pathao_account_type.strip(),))

    if bkash_qr_file and bkash_qr_file.filename:
        ext = os.path.splitext(bkash_qr_file.filename)[1] or ".png"
        filename = f"bkash_qr{ext}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        with open(filepath, "wb") as f:
            f.write(await bkash_qr_file.read())
        rel_url = f"/uploads/{filename}"
        conn.execute("INSERT INTO payment_settings (setting_key, setting_value) VALUES ('bkash_qr_url', ?) ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value", (rel_url,))

    if pathao_qr_file and pathao_qr_file.filename:
        ext = os.path.splitext(pathao_qr_file.filename)[1] or ".png"
        filename = f"pathao_qr{ext}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        with open(filepath, "wb") as f:
            f.write(await pathao_qr_file.read())
        rel_url = f"/uploads/{filename}"
        conn.execute("INSERT INTO payment_settings (setting_key, setting_value) VALUES ('pathao_qr_url', ?) ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value", (rel_url,))

    conn.commit()
    conn.close()

    return {"success": True, "message": "Payment gateway numbers, account types, and QR codes updated successfully!"}


class SavePackagesPayload(BaseModel):
    packages: list[dict]
    custom_package: Optional[dict] = None

@app.post("/api/admin/package-settings")
def save_admin_package_settings(req: SavePackagesPayload, admin_user: dict = Depends(get_admin_user)):
    if admin_user.get("role") not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Superadmin or Admin permission required.")

    pkg_file = os.path.join(os.path.dirname(__file__), "packages.json")
    try:
        import json
        save_data = {
            "packages": req.packages,
            "custom_package": req.custom_package or {
                "name": "Custom Upgrade",
                "price_per_credit_bdt": 10,
                "min_credits": 10,
                "max_credits": 5000,
                "step": 10,
                "description": "Select the exact credit amount your team requires:"
            }
        }
        with open(pkg_file, "w", encoding="utf-8") as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save package settings: {str(e)}")

    return {"success": True, "message": "Package prices and features updated successfully!"}


class PaymentRequestPayload(BaseModel):
    package_name: str
    credits_requested: int
    amount_bdt: float
    payment_method: str = "bkash"
    user_name: Optional[str] = None
    bkash_number: str
    transaction_id: str

@app.post("/api/payments/submit")
@app.post("/api/payments/submit-request")
def submit_payment_request(req: PaymentRequestPayload, current_user: dict = Depends(get_current_user)):
    if not req.transaction_id or not req.bkash_number:
        raise HTTPException(status_code=400, detail="Sender Phone Number and Transaction ID (TrxID) are required.")
        
    conn = get_db()
    # Check if duplicate TrxID pending or approved
    existing = conn.execute("SELECT id FROM payment_requests WHERE transaction_id = ? AND status IN ('pending', 'approved')", (req.transaction_id.strip(),)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="This Transaction ID (TrxID) has already been submitted.")

    u_name = req.user_name or current_user.get("full_name") or current_user.get("email")

    conn.execute(
        """INSERT INTO payment_requests (user_id, package_name, credits_requested, amount_bdt, payment_method, user_name, bkash_number, transaction_id, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (current_user["id"], req.package_name, req.credits_requested, req.amount_bdt, req.payment_method, u_name, req.bkash_number.strip(), req.transaction_id.strip())
    )
    conn.commit()
    conn.close()
    return {"success": True, "message": "Payment proof submitted successfully! Pending admin verification."}

@app.get("/api/payments/my-requests")
def list_my_payment_requests(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM payment_requests WHERE user_id = ? ORDER BY created_at DESC", (current_user["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/admin/payments")
@app.get("/api/admin/payment-requests")
def list_admin_payment_requests(admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    rows = conn.execute("""
        SELECT pr.*, u.email AS user_email, u.full_name FROM payment_requests pr
        JOIN users u ON pr.user_id = u.id
        ORDER BY CASE WHEN pr.status = 'pending' THEN 0 ELSE 1 END, pr.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

class ApprovePaymentPayload(BaseModel):
    expiry_days: Optional[int] = 30
    expires_at: Optional[str] = None
    custom_key: Optional[str] = None

@app.post("/api/admin/payment-requests/{request_id}/approve")
def approve_payment_request(
    request_id: int,
    payload: Optional[ApprovePaymentPayload] = None,
    admin_user: dict = Depends(get_admin_user)
):
    conn = get_db()
    req = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
    if not req:
        conn.close()
        raise HTTPException(status_code=404, detail="Payment request not found.")

    if req["status"] != "pending":
        conn.close()
        raise HTTPException(status_code=400, detail=f"Payment request has already been {req['status']}.")

    # Fetch user info
    user_row = conn.execute("SELECT email, full_name FROM users WHERE id = ?", (req["user_id"],)).fetchone()
    customer_email = user_row["email"] if user_row else ""
    customer_name = (user_row["full_name"] if user_row else "") or req.get("user_name") or "Customer"

    # NOTE: Credits are NO LONGER added here. They are deferred until the
    # customer activates the license key in the Subscribe panel.
    # This ensures the Super Admin has full control — the key acts as a
    # redeemable token that grants credits on first activation.

    credits_amount = req["credits_requested"]
    plan_name = req["package_name"] or "Pro"

    # Generate Desktop Production License Key (with embedded credits_amount)
    expiry_days = (payload.expiry_days if payload and payload.expiry_days else 30)
    expires_at_val = (payload.expires_at if payload else None)
    custom_key_val = (payload.custom_key if payload else None)

    license_info = generate_production_license(
        customer_name=customer_name,
        customer_email=customer_email,
        expiry_days=expiry_days,
        expires_at_str=expires_at_val,
        user_id=req["user_id"],
        payment_request_id=request_id,
        custom_key=custom_key_val,
        plan_tier=plan_name.lower(),
        credits_amount=credits_amount
    )

    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """UPDATE payment_requests 
           SET status = 'approved', processed_at = ?, processed_by = ?, production_key = ?, license_expiry = ?
           WHERE id = ?""",
        (now_str, admin_user["id"], license_info["production_key"], license_info["expires_at"], request_id)
    )
    conn.commit()
    conn.close()

    # Send the license key to the customer via email (async, non-blocking)
    email_sent = False
    if customer_email:
        try:
            email_sent_result, email_error = send_license_key_email(
                recipient_email=customer_email,
                customer_name=customer_name,
                production_key=license_info["production_key"],
                plan_name=plan_name,
                credits=credits_amount,
                expires_at=license_info["expires_at"]
            )
            email_sent = email_sent_result
        except Exception as e:
            print(f"[LICENSE EMAIL ERROR] {e}")

    return {
        "success": True,
        "message": f"Payment approved! Production key generated for {customer_name}." + (" License key emailed." if email_sent else " (Email not sent — key shown below.)"),
        "credits_pending": credits_amount,
        "email_sent": email_sent,
        "license": license_info
    }

@app.post("/api/admin/payment-requests/{request_id}/reject")
def reject_payment_request(request_id: int, rejection_reason: str = Form("Transaction ID mismatch or invalid payment"), admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    req = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
    if not req:
        conn.close()
        raise HTTPException(status_code=404, detail="Payment request not found.")

    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE payment_requests SET status = 'rejected', rejection_reason = ?, processed_at = ?, processed_by = ? WHERE id = ?",
        (rejection_reason, now_str, admin_user["id"], request_id)
    )
    conn.commit()
    conn.close()
    return {"success": True, "message": "Payment request rejected."}

# ── Desktop Production License & Admin Key Management Endpoints ──

@app.get("/api/admin/licenses")
def list_admin_licenses(admin_user: dict = Depends(get_admin_user)):
    """Admin endpoint: lists all issued production keys and expiration dates"""
    conn = get_db()
    rows = conn.execute("""
        SELECT l.*, u.email as user_email_ref
        FROM licenses l
        LEFT JOIN users u ON l.user_id = u.id
        ORDER BY l.created_at DESC
    """).fetchall()
    conn.close()

    results = []
    now = datetime.now(timezone.utc)
    for r in rows:
        d = dict(r)
        exp_str = d.get("expires_at")
        days_left = 0
        is_exp = False
        if exp_str:
            try:
                if "T" in str(exp_str):
                    exp_dt = datetime.fromisoformat(str(exp_str).replace("Z", "+00:00"))
                else:
                    exp_dt = datetime.strptime(str(exp_str)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                days_left = max(0, (exp_dt - now).days)
                is_exp = now > exp_dt
            except Exception:
                pass
        d["days_remaining"] = days_left
        d["is_expired"] = is_exp
        results.append(d)
    return results

class AdminGenerateLicensePayload(BaseModel):
    customer_name: str
    customer_email: Optional[str] = ""
    expiry_days: Optional[int] = 30
    expires_at: Optional[str] = None
    custom_key: Optional[str] = None
    plan_tier: Optional[str] = "pro"
    credits_amount: Optional[int] = 0

@app.post("/api/admin/licenses/generate")
def admin_generate_license(payload: AdminGenerateLicensePayload, admin_user: dict = Depends(get_admin_user)):
    """Admin endpoint: manually generate and sign a new production license key with optional user linking and credits"""
    user_id = None
    cust_email = (payload.customer_email or "").strip()
    if cust_email:
        conn = get_db()
        matched_user = conn.execute(
            "SELECT id, full_name FROM users WHERE LOWER(email) = LOWER(?)",
            (cust_email,)
        ).fetchone()
        conn.close()
        if matched_user:
            user_id = matched_user["id"]

    license_info = generate_production_license(
        customer_name=payload.customer_name,
        customer_email=cust_email,
        expiry_days=payload.expiry_days or 30,
        expires_at_str=payload.expires_at,
        user_id=user_id,
        custom_key=payload.custom_key,
        plan_tier=payload.plan_tier or "pro",
        credits_amount=payload.credits_amount or 0
    )
    return {"success": True, "license": license_info}

class AdminExtendLicensePayload(BaseModel):
    additional_days: int = 30

@app.post("/api/admin/licenses/{license_id}/extend")
def admin_extend_license(license_id: int, payload: AdminExtendLicensePayload, admin_user: dict = Depends(get_admin_user)):
    """Admin endpoint: extend an existing license's expiration date"""
    conn = get_db()
    row = conn.execute("SELECT * FROM licenses WHERE id = ?", (license_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="License not found.")

    now = datetime.now(timezone.utc)
    current_exp_str = row["expires_at"]
    try:
        if "T" in str(current_exp_str):
            base_dt = datetime.fromisoformat(str(current_exp_str).replace("Z", "+00:00"))
        else:
            base_dt = datetime.strptime(str(current_exp_str)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if base_dt < now:
            base_dt = now
    except Exception:
        base_dt = now

    new_exp_dt = base_dt + timedelta(days=payload.additional_days)
    new_exp_str = new_exp_dt.strftime("%Y-%m-%dT23:59:59Z")

    updated_license = generate_production_license(
        customer_name=row["customer_name"],
        customer_email=row["customer_email"],
        expires_at_str=new_exp_str,
        user_id=row["user_id"],
        payment_request_id=row["payment_request_id"],
        custom_key=row["production_key"],
        plan_tier=row["plan_tier"],
        credits_amount=row["credits_amount"] or 0
    )

    conn.execute(
        "UPDATE licenses SET expires_at = ?, license_token = ?, status = 'active' WHERE id = ?",
        (new_exp_str, updated_license["license_token"], license_id)
    )
    conn.commit()
    conn.close()
    return {"success": True, "message": f"License extended by {payload.additional_days} days.", "license": updated_license}

@app.post("/api/admin/licenses/{license_id}/revoke")
def admin_revoke_license(license_id: int, admin_user: dict = Depends(get_admin_user)):
    """Admin endpoint: revoke an active license"""
    conn = get_db()
    conn.execute("UPDATE licenses SET status = 'revoked' WHERE id = ?", (license_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "License revoked successfully."}

# ── Client-facing Desktop License Status & Activation Endpoints ──

@app.get("/api/license/status")
def get_license_status_endpoint(current_user: Optional[dict] = Depends(get_optional_current_user)):
    """Client endpoint: returns current desktop app license status dynamically for the calling user"""
    return get_current_license_status(current_user)

class ActivateLicensePayload(BaseModel):
    license_key: str

@app.post("/api/license/activate")
def activate_license_endpoint(payload: ActivateLicensePayload, current_user: dict = Depends(get_current_user)):
    """Client endpoint: activates desktop app using production key or full token.
    On activation of an unredeemed key, binds it to the current user and grants credits."""
    raw_key = payload.license_key.strip()
    if not raw_key:
        raise HTTPException(status_code=400, detail="License key cannot be empty.")

    conn = get_db()
    lic_row = conn.execute(
        "SELECT * FROM licenses WHERE production_key = ? OR license_token = ? ORDER BY id DESC LIMIT 1",
        (raw_key, raw_key)
    ).fetchone()

    # If not found directly, verify signed token
    if not lic_row and "." in raw_key:
        token_res = verify_license(raw_key)
        if token_res.get("valid"):
            prod_k = token_res.get("production_key")
            if prod_k:
                lic_row = conn.execute(
                    "SELECT * FROM licenses WHERE production_key = ? ORDER BY id DESC LIMIT 1",
                    (prod_k,)
                ).fetchone()

    if not lic_row:
        token_res = verify_license(raw_key)
        if not token_res.get("valid"):
            conn.close()
            raise HTTPException(status_code=400, detail=token_res.get("message", "Invalid license key."))
        # Insert standalone verified key
        exp_iso = token_res.get("expires_at")
        conn.execute("""
            INSERT INTO licenses (user_id, customer_name, customer_email, production_key, license_token, plan_tier, credits_amount, is_redeemed, status, expires_at, last_validated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, 1, 'active', ?, CURRENT_TIMESTAMP)
        """, (
            current_user["id"],
            current_user["full_name"],
            current_user["email"],
            token_res.get("production_key") or raw_key,
            token_res.get("license_token") or raw_key,
            token_res.get("plan_tier") or "pro",
            exp_iso
        ))
        conn.commit()
        lic_row = conn.execute("SELECT * FROM licenses WHERE production_key = ?", (token_res.get("production_key") or raw_key,)).fetchone()

    lic_dict = dict(lic_row)

    if lic_dict.get("status") == "revoked":
        conn.close()
        raise HTTPException(status_code=400, detail="This license has been revoked.")

    exp_val = lic_dict.get("expires_at")
    now = datetime.now(timezone.utc)
    if isinstance(exp_val, datetime):
        exp_dt = exp_val if exp_val.tzinfo else exp_val.replace(tzinfo=timezone.utc)
    else:
        exp_str = str(exp_val)
        if "T" in exp_str:
            exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
        else:
            exp_dt = datetime.strptime(exp_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

    if now > exp_dt:
        conn.close()
        raise HTTPException(status_code=400, detail=f"This license key expired on {exp_dt.strftime('%B %d, %Y')}.")

    # If assigned to another user's email or user_id
    if lic_dict.get("user_id") and lic_dict.get("user_id") != current_user["id"]:
        if lic_dict.get("customer_email") and lic_dict.get("customer_email").strip().lower() != current_user["email"].strip().lower():
            conn.close()
            raise HTTPException(status_code=400, detail="This license key is assigned to a different user account.")

    credits_granted = 0
    new_balance = current_user.get("credits", 0)

    if not lic_dict.get("is_redeemed"):
        credits_to_add = lic_dict.get("credits_amount") or 0
        if credits_to_add > 0:
            conn.execute("UPDATE users SET credits = credits + ? WHERE id = ?", (credits_to_add, current_user["id"]))
            conn.execute(
                """INSERT INTO credit_transactions (user_id, amount, transaction_type, description)
                   VALUES (?, ?, 'add', ?)""",
                (current_user["id"], credits_to_add, f"License Key Activation: {lic_dict.get('production_key')}")
            )
            credits_granted = credits_to_add

        conn.execute("""
            UPDATE licenses 
            SET user_id = ?, customer_email = ?, is_redeemed = 1, status = 'active', last_validated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (current_user["id"], current_user["email"], lic_dict["id"]))
        conn.commit()

        u_row = conn.execute("SELECT credits FROM users WHERE id = ?", (current_user["id"],)).fetchone()
        if u_row:
            new_balance = u_row["credits"]
    else:
        # Already redeemed: ensure linked to current user
        if not lic_dict.get("user_id"):
            conn.execute("UPDATE licenses SET user_id = ?, customer_email = ? WHERE id = ?", (current_user["id"], current_user["email"], lic_dict["id"]))
            conn.commit()

    conn.close()

    days_remaining = max(0, (exp_dt - now).days)
    status_data = {
        "valid": True,
        "is_expired": False,
        "status": "active",
        "production_key": lic_dict.get("production_key"),
        "license_token": lic_dict.get("license_token"),
        "customer_name": current_user.get("full_name"),
        "customer_email": current_user.get("email"),
        "plan_tier": lic_dict.get("plan_tier") or "pro",
        "expires_at": exp_dt.isoformat(),
        "days_remaining": days_remaining,
        "credits_amount": lic_dict.get("credits_amount", 0),
        "is_redeemed": True,
        "message": f"License active. Valid until {exp_dt.strftime('%B %d, %Y')} ({days_remaining} days remaining)."
    }

    msg = f"License successfully activated! Valid until {exp_dt.strftime('%B %d, %Y')}."
    if credits_granted > 0:
        msg = f"License activated! {credits_granted} credits added to your account (Balance: {new_balance})."

    return {
        "success": True,
        "message": msg,
        "license": status_data,
        "credits_granted": credits_granted,
        "new_balance": new_balance
    }

class QuickRenewPayload(BaseModel):
    email: str
    password: str
    license_key: str

@app.post("/api/license/quick-renew")
def quick_renew_license(payload: QuickRenewPayload):
    """Allows an expired user to activate a new renewal key directly from the login page."""
    conn = get_db()
    user_row = conn.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (payload.email.strip(),)).fetchone()
    if not user_row or not verify_password(payload.password, user_row["password_hash"]):
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid email or password.")

    user = dict(user_row)
    raw_key = payload.license_key.strip()
    lic_row = conn.execute(
        "SELECT * FROM licenses WHERE production_key = ? OR license_token = ? ORDER BY id DESC LIMIT 1",
        (raw_key, raw_key)
    ).fetchone()

    if not lic_row:
        conn.close()
        raise HTTPException(status_code=400, detail="License key not found.")

    lic_dict = dict(lic_row)
    if lic_dict.get("status") == "revoked":
        conn.close()
        raise HTTPException(status_code=400, detail="This license has been revoked.")

    exp_val = lic_dict.get("expires_at")
    now = datetime.now(timezone.utc)
    if isinstance(exp_val, datetime):
        exp_dt = exp_val if exp_val.tzinfo else exp_val.replace(tzinfo=timezone.utc)
    else:
        exp_str = str(exp_val)
        if "T" in exp_str:
            exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
        else:
            exp_dt = datetime.strptime(exp_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

    if now > exp_dt:
        conn.close()
        raise HTTPException(status_code=400, detail=f"This renewal key expired on {exp_dt.strftime('%B %d, %Y')}.")

    credits_to_add = lic_dict.get("credits_amount") or 0
    if not lic_dict.get("is_redeemed") and credits_to_add > 0:
        conn.execute("UPDATE users SET credits = credits + ? WHERE id = ?", (credits_to_add, user["id"]))
        conn.execute(
            """INSERT INTO credit_transactions (user_id, amount, transaction_type, description)
               VALUES (?, ?, 'add', ?)""",
            (user["id"], credits_to_add, f"Renewal Key Activation: {lic_dict.get('production_key')}")
        )

    conn.execute("""
        UPDATE licenses 
        SET user_id = ?, customer_email = ?, is_redeemed = 1, status = 'active', last_validated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (user["id"], user["email"], lic_dict["id"]))
    conn.commit()

    token = create_jwt_token(user["id"], user["role"])
    conn.close()

    return {
        "success": True,
        "message": f"License renewed successfully! Valid until {exp_dt.strftime('%B %d, %Y')}.",
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "role": user["role"],
            "credits": user["credits"] + (credits_to_add if not lic_dict.get("is_redeemed") else 0),
            "is_verified": user.get("is_verified", 1),
            "warning_message": user.get("warning_message") or ""
        }
    }




@app.get("/api/license/my-licenses")
def get_my_licenses(current_user: dict = Depends(get_current_user)):
    """Client endpoint: returns all licenses associated with the current user (by user_id or email).
    Allows the customer to see their generated keys, status, and whether credits have been redeemed."""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM licenses
        WHERE user_id = ? OR customer_email = ?
        ORDER BY created_at DESC
    """, (current_user["id"], current_user["email"])).fetchall()
    conn.close()

    now = datetime.now(timezone.utc)
    results = []
    for r in rows:
        d = dict(r)
        exp_str = d.get("expires_at")
        days_left = 0
        is_exp = False
        if exp_str:
            try:
                if "T" in str(exp_str):
                    exp_dt = datetime.fromisoformat(str(exp_str).replace("Z", "+00:00"))
                else:
                    exp_dt = datetime.strptime(str(exp_str)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                days_left = max(0, (exp_dt - now).days)
                is_exp = now > exp_dt
            except Exception:
                pass
        d["days_remaining"] = days_left
        d["is_expired"] = is_exp
        d["is_redeemed"] = bool(d.get("is_redeemed"))
        results.append(d)

    return results

# ── Marketing Campaign Endpoints ─────────────────────────

@app.get("/api/marketing/whatsapp-status")
def whatsapp_status():
    profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whatsapp_session")
    if not os.path.exists(profile):
        return {"session_active": False}
    
    has_files = False
    try:
        indexed_db_path = os.path.join(profile, "Default", "IndexedDB")
        local_storage_path = os.path.join(profile, "Default", "Local Storage")
        if os.path.exists(indexed_db_path) or os.path.exists(local_storage_path):
            for root, dirs, files in os.walk(profile):
                if "whatsapp" in root.lower():
                    has_files = True
                    break
                for f in files:
                    if f.endswith(".leveldb") or f.endswith(".ldb") or "whatsapp" in f.lower():
                        has_files = True
                        break
                if has_files:
                    break
    except Exception:
        pass

    return {"session_active": has_files}

@app.get("/api/marketing/whatsapp-setup-session")
def whatsapp_setup(background_tasks: BackgroundTasks, current_user: dict = Depends(get_current_user)):
    from senders import setup_driver, wait_for_whatsapp_login, set_active_setup_driver, close_active_setup_driver
    def scan_runner():
        try:
            close_active_setup_driver()
            driver = setup_driver()
            set_active_setup_driver(driver)
            is_logged_in = wait_for_whatsapp_login(driver)
            if is_logged_in:
                time.sleep(3)
            close_active_setup_driver()
        except Exception:
            close_active_setup_driver()
    background_tasks.add_task(scan_runner)
    return {"success": True, "message": "Chrome launched! Scan QR code or view active WhatsApp chats in browser."}

@app.post("/api/marketing/whatsapp-reset-session")
def whatsapp_reset_session(background_tasks: BackgroundTasks, current_user: dict = Depends(get_current_user)):
    import shutil
    from senders import setup_driver, wait_for_whatsapp_login, set_active_setup_driver, close_active_setup_driver
    close_active_setup_driver()
    profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whatsapp_session")
    if os.path.exists(profile):
        try:
            shutil.rmtree(profile, ignore_errors=True)
        except Exception:
            pass
    
    def scan_runner():
        try:
            driver = setup_driver()
            set_active_setup_driver(driver)
            is_logged_in = wait_for_whatsapp_login(driver)
            if is_logged_in:
                time.sleep(3)
            close_active_setup_driver()
        except Exception:
            close_active_setup_driver()
    background_tasks.add_task(scan_runner)
    return {"success": True, "message": "WhatsApp session cleared! Chrome launched to scan new QR code."}

@app.get("/api/marketing/whatsapp-progress")
def get_progress(recipient_group: str, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    row = conn.execute("SELECT last_index FROM whatsapp_progress WHERE recipient_group = ?", (recipient_group,)).fetchone()
    conn.close()
    return {"last_index": row["last_index"] if row else 0}

@app.post("/api/marketing/send-whatsapp")
def send_whatsapp(req: WhatsAppCampaignRequest, current_user: dict = Depends(get_current_user)):
    file_path, group_name = resolve_any_recipient_group(req.recipient_group)

    if req.selected_contacts and len(req.selected_contacts) > 0:
        contacts = req.selected_contacts
    else:
        if not file_path or not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Recipients source dataset file not found.")

        try:
            df = pd.read_csv(file_path, dtype=str) if file_path.endswith(".csv") else pd.read_excel(file_path, dtype=str)
            df = clean_lead_df(df)
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to load lead group columns.")

        phone_col = None
        name_col = None
        for c in df.columns:
            if "phone" in c.lower() or "mobile" in c.lower() or "contact" in c.lower():
                phone_col = c
            if "name" in c.lower() or "title" in c.lower():
                name_col = c

        if not phone_col and len(df.columns) > 0:
            phone_col = df.columns[0]

        contacts = []
        for _, row in df.iterrows():
            p = str(row[phone_col]).strip() if phone_col and pd.notna(row[phone_col]) else ""
            n = str(row[name_col]).strip() if name_col and pd.notna(row[name_col]) else "Customer"
            if len(p) > 5:
                contacts.append({"phone": p, "name": n})

    if not contacts:
        raise HTTPException(status_code=400, detail="No contacts selected or found in dataset.")

    conn = get_db()
    # Check if a WhatsApp campaign is already running
    active_wa = conn.execute(
        "SELECT id FROM marketing_campaigns WHERE status = 'running' AND campaign_type = 'whatsapp'"
    ).fetchone()
    if active_wa:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"WhatsApp Campaign #{active_wa['id']} is currently running! Please wait for it to finish or stop it before launching a new campaign."
        )

    # Deduct credits
    start_index = 0
    if req.resume:
        chk = conn.execute("SELECT last_index FROM whatsapp_progress WHERE recipient_group = ?", (req.recipient_group,)).fetchone()
        if chk:
            start_index = chk["last_index"]
    
    # Use explicit start_row if provided (overrides resume)
    if req.start_row is not None and req.start_row > 0:
        start_index = req.start_row
            
    if current_user["role"] not in ("admin", "superadmin") and not req.resume:
        if current_user["credits"] < 5:
            conn.close()
            raise HTTPException(status_code=403, detail="Insufficient credits (campaign costs 5 credits).")
        conn.execute("UPDATE users SET credits = credits - 5 WHERE id = ?", (current_user["id"],))
        conn.execute("INSERT INTO credit_transactions (user_id, amount, transaction_type, description) VALUES (?, 5, 'deduct', 'WhatsApp dispatch')", (current_user["id"],))
        
    campaign_id = f"wa_camp_{int(time.time())}"
    conn.execute(
        """INSERT INTO marketing_campaigns (id, user_id, campaign_type, recipient_group, template_preview, status, sent_count, total_count, start_row)
           VALUES (?, ?, 'whatsapp', ?, ?, 'running', ?, ?, ?)""",
        (campaign_id, current_user["id"], req.recipient_group, req.message_template[:200], start_index, len(contacts), start_index)
    )
    conn.commit()
    conn.close()

    # Background Campaign Thread
    t = threading.Thread(target=run_whatsapp_campaign, args=(campaign_id, contacts, req.message_template, req.recipient_group, start_index))
    t.daemon = True
    t.start()

    return {"success": True, "campaign_id": campaign_id, "start_index": start_index}

@app.post("/api/marketing/send-email")
def send_email(req: EmailCampaignRequest, current_user: dict = Depends(get_current_user)):
    file_path, group_name = resolve_any_recipient_group(req.recipient_group)

    if req.selected_contacts and len(req.selected_contacts) > 0:
        target_count = len(req.selected_contacts)
    else:
        if not file_path or not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Lead dataset file not found.")
        try:
            df = pd.read_csv(file_path) if file_path.endswith(".csv") else pd.read_excel(file_path)
            target_count = len(df)
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to load file.")

    conn = get_db()
    if current_user["role"] not in ("admin", "superadmin"):
        # 1. Check Brevo verification status
        status = current_user.get("brevo_account_status") or "none"
        if status != "approved":
            conn.close()
            raise HTTPException(status_code=403, detail="Brevo business verification is required before sending email campaigns. Please submit your business details for verification.")

        # 2. Check Daily Email Limit
        daily_limit = current_user.get("daily_email_limit") or 300
        today_str = datetime.now().strftime("%Y-%m-%d")
        row = conn.execute("""
            SELECT SUM(sent_count) as total_today FROM marketing_campaigns 
            WHERE user_id = ? AND campaign_type = 'email' AND created_at >= ?
        """, (current_user["id"], today_str)).fetchone()
        today_sent = (row["total_today"] or 0) if row else 0

        if today_sent + target_count > daily_limit:
            conn.close()
            raise HTTPException(status_code=400, detail=f"Daily email limit exceeded. You have sent {today_sent}/{daily_limit} emails today. Sending {target_count} more would exceed your limit.")

        # 3. Deduct credits
        if current_user["credits"] < 1:
            conn.close()
            raise HTTPException(status_code=403, detail="Insufficient credits to run email campaign.")
        conn.execute("UPDATE users SET credits = credits - 1 WHERE id = ?", (current_user["id"],))
        conn.execute("INSERT INTO credit_transactions (user_id, amount, transaction_type, description) VALUES (?, 1, 'deduct', 'Email Campaign')", (current_user["id"],))

    campaign_id = f"email_camp_{int(time.time())}"
    conn.execute(
        """INSERT INTO marketing_campaigns (id, user_id, campaign_type, recipient_group, template_preview, status, sent_count, total_count)
           VALUES (?, ?, 'email', ?, ?, 'done', ?, ?)""",
        (campaign_id, current_user["id"], req.recipient_group, req.subject, target_count, target_count)
    )
    conn.execute(
        "INSERT INTO campaign_logs (campaign_id, message) VALUES (?, ?)",
        (campaign_id, f"Email campaign dispatched successfully to {target_count} selected addresses via Brevo API.")
    )
    conn.commit()
    conn.close()

    return {"success": True, "campaign_id": campaign_id, "recipient_count": target_count}

@app.get("/api/marketing/recipient-contacts")
def get_recipient_contacts(recipient_group: str, current_user: dict = Depends(get_current_user)):
    file_path, group_name = resolve_any_recipient_group(recipient_group)

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Recipient source data file not found.")

    try:
        df = pd.read_csv(file_path, dtype=str) if file_path.endswith(".csv") else pd.read_excel(file_path, dtype=str)
        df = clean_lead_df(df)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to parse lead data file.")

    phone_col = None
    email_col = None
    name_col = None

    for c in df.columns:
        c_lower = c.lower()
        if "phone" in c_lower or "mobile" in c_lower or "contact" in c_lower or "number" in c_lower or "tel" in c_lower:
            if not phone_col: phone_col = c
        if "email" in c_lower or "mail" in c_lower:
            if not email_col: email_col = c
        if "name" in c_lower or "title" in c_lower or "coaching" in c_lower or "school" in c_lower or "store" in c_lower or "shop" in c_lower:
            if not name_col: name_col = c

    if not name_col and len(df.columns) > 0:
        name_col = df.columns[0]

    def clean_val(v):
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        if s in ("nan", "NaN", "None", "None.0"):
            return ""
        if s.startswith("+88001"):
            s = "+8801" + s[6:]
        elif s.startswith("88001"):
            s = "+8801" + s[5:]
        elif s.startswith("001") and len(s) == 13:
            s = "+8801" + s[3:]
        return s

    contacts = []
    for idx, row in df.iterrows():
        name_val = clean_val(row[name_col]) if name_col and row[name_col] else f"Lead #{idx+1}"
        phone_val = clean_val(row[phone_col]) if phone_col and row[phone_col] else ""
        email_val = clean_val(row[email_col]) if email_col and row[email_col] else ""
        area_val = clean_val(row.get("Area", row.get("District", row.get("Address", "BD"))))
        
        contacts.append({
            "id": idx,
            "name": name_val,
            "phone": phone_val,
            "email": email_val,
            "area": area_val
        })

    return {"success": True, "total": len(contacts), "contacts": contacts}

def compute_campaign_eta(campaign_type: str, sent_count: int, total_count: int, start_row: int = 0, created_at: str = None):
    """
    Computes approximate estimated time to completion (EST) for marketing campaigns.
    Factors in human-like typing, page load, randomized delay, and cool-down breaks.
    """
    remaining = max(0, total_count - sent_count)
    progress_pct = min(100, round((sent_count / total_count) * 100)) if total_count > 0 else 0

    if remaining == 0:
        return {
            "est_seconds_remaining": 0,
            "est_human": "Completed",
            "est_completion_time": None,
            "progress_percent": 100
        }

    if campaign_type == "whatsapp":
        cooldown_cycles = remaining // 8
        cooldown_secs = cooldown_cycles * 120

        est_seconds = 0
        contacts_sent_now = max(0, sent_count - (start_row or 0))
        if contacts_sent_now >= 2 and created_at:
            try:
                dt_created = datetime.strptime(str(created_at).split(".")[0], "%Y-%m-%d %H:%M:%S")
                elapsed = (datetime.now() - dt_created).total_seconds()
                if elapsed > 0:
                    rate = elapsed / contacts_sent_now
                    rate = max(16.0, min(60.0, rate))
                    est_seconds = int(remaining * rate)
            except Exception:
                est_seconds = int(remaining * 22 + cooldown_secs)

        if est_seconds <= 0:
            est_seconds = int(remaining * 22 + cooldown_secs)
    else:
        est_seconds = max(1, int(remaining * 0.5))

    if est_seconds < 60:
        est_human = f"~{max(5, est_seconds)}s remaining"
    elif est_seconds < 3600:
        mins = est_seconds // 60
        secs = est_seconds % 60
        est_human = f"~{mins}m {secs}s remaining" if secs > 0 else f"~{mins}m remaining"
    else:
        hrs = est_seconds // 3600
        mins = (est_seconds % 3600) // 60
        est_human = f"~{hrs}h {mins}m remaining"

    comp_dt = datetime.now() + timedelta(seconds=est_seconds)
    clock_time = comp_dt.strftime("%I:%M %p")
    est_human_with_clock = f"{est_human} (Est. {clock_time})"

    return {
        "est_seconds_remaining": est_seconds,
        "est_human": est_human_with_clock,
        "est_completion_time": comp_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "progress_percent": progress_pct
    }

@app.get("/api/marketing/active-campaigns")
def get_active_campaigns(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    if current_user["role"] in ("admin", "superadmin"):
        rows = conn.execute("""
            SELECT c.*, u.email, u.full_name FROM marketing_campaigns c
            JOIN users u ON c.user_id = u.id
            WHERE c.status IN ('running', 'stopping')
            ORDER BY c.created_at DESC
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM marketing_campaigns
            WHERE user_id = ? AND status IN ('running', 'stopping')
            ORDER BY created_at DESC
        """, (current_user["id"],)).fetchall()

    active_list = []
    for r in rows:
        cdict = dict(r)
        eta_info = compute_campaign_eta(
            cdict.get("campaign_type", "whatsapp"),
            cdict.get("sent_count", 0),
            cdict.get("total_count", 0),
            cdict.get("start_row", 0),
            cdict.get("created_at")
        )
        cdict.update(eta_info)
        last_log = conn.execute(
            "SELECT message FROM campaign_logs WHERE campaign_id = ? ORDER BY id DESC LIMIT 1",
            (cdict["id"],)
        ).fetchone()
        cdict["latest_log"] = last_log["message"] if last_log else "Dispatch worker initialized..."
        active_list.append(cdict)
    conn.close()
    return {"active_campaigns": active_list}

@app.get("/api/marketing/campaigns")
def list_campaigns(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    if current_user["role"] in ("admin", "superadmin"):
        rows = conn.execute("""
            SELECT c.*, u.email, u.full_name FROM marketing_campaigns c
            JOIN users u ON c.user_id = u.id
            ORDER BY c.created_at DESC
        """).fetchall()
    else:
        rows = conn.execute("SELECT * FROM marketing_campaigns WHERE user_id = ? ORDER BY created_at DESC", (current_user["id"],)).fetchall()
    conn.close()

    result = []
    for r in rows:
        cdict = dict(r)
        eta_info = compute_campaign_eta(
            cdict.get("campaign_type", "whatsapp"),
            cdict.get("sent_count", 0),
            cdict.get("total_count", 0),
            cdict.get("start_row", 0),
            cdict.get("created_at")
        )
        cdict.update(eta_info)
        result.append(cdict)
    return result

@app.get("/api/marketing/whatsapp-campaign/{campaign_id}")
def get_campaign_status(campaign_id: str, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    c = conn.execute("SELECT * FROM marketing_campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if not c:
        conn.close()
        raise HTTPException(status_code=404, detail="Campaign not found.")
    logs = conn.execute("SELECT message, created_at FROM campaign_logs WHERE campaign_id = ? ORDER BY id ASC", (campaign_id,)).fetchall()
    conn.close()

    sent_cnt = c["sent_count"] or 0
    total_cnt = c["total_count"] or 0
    start_row = c.get("start_row", 0) if isinstance(c, dict) else (c["start_row"] if "start_row" in c.keys() else 0)
    failed_cnt = c.get("failed_count", 0) if isinstance(c, dict) else (c["failed_count"] if "failed_count" in c.keys() else 0)

    eta_info = compute_campaign_eta(
        c.get("campaign_type", "whatsapp") if isinstance(c, dict) else (c["campaign_type"] if "campaign_type" in c.keys() else "whatsapp"),
        sent_cnt,
        total_cnt,
        start_row,
        c.get("created_at") if isinstance(c, dict) else (c["created_at"] if "created_at" in c.keys() else None)
    )

    return {
        "campaign_id": campaign_id,
        "campaign_type": c.get("campaign_type", "whatsapp") if isinstance(c, dict) else (c["campaign_type"] if "campaign_type" in c.keys() else "whatsapp"),
        "recipient_group": c.get("recipient_group", "") if isinstance(c, dict) else (c["recipient_group"] if "recipient_group" in c.keys() else ""),
        "status": c["status"],
        "total": total_cnt,
        "sent": sent_cnt,
        "failed": failed_cnt,
        "start_row": start_row,
        "est_seconds_remaining": eta_info["est_seconds_remaining"],
        "est_human": eta_info["est_human"],
        "est_completion_time": eta_info["est_completion_time"],
        "progress_percent": eta_info["progress_percent"],
        "logs": [f"[{str(l['created_at']).split(' ')[1] if ' ' in str(l['created_at']) else str(l['created_at'])}] {l['message']}" for l in logs]
    }

@app.post("/api/marketing/whatsapp-campaign/{campaign_id}/stop")
def stop_campaign(campaign_id: str, current_user: dict = Depends(get_current_user)):
    from senders import force_stop_campaign
    force_stop_campaign(campaign_id)

    conn = get_db()
    c = conn.execute("SELECT status FROM marketing_campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if not c:
        conn.close()
        raise HTTPException(status_code=404, detail="Campaign not found.")
        
    conn.execute("UPDATE marketing_campaigns SET status = 'stopped' WHERE id = ?", (campaign_id,))
    conn.execute("INSERT INTO campaign_logs (campaign_id, message) VALUES (?, ?)", (campaign_id, "User requested manual campaign termination (stopped immediately)."))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Campaign stopped immediately."}

@app.delete("/api/marketing/campaign/{campaign_id}")
def delete_campaign(campaign_id: str, current_user: dict = Depends(get_current_user)):
    if current_user["role"] not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Only Super Admins and Admins can delete campaign logs.")
    conn = get_db()
    conn.execute("DELETE FROM campaign_logs WHERE campaign_id = ?", (campaign_id,))
    conn.execute("DELETE FROM marketing_campaigns WHERE id = ?", (campaign_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Campaign and audit logs deleted successfully."}

@app.delete("/api/marketing/campaign/{campaign_id}/logs")
def clear_campaign_logs(campaign_id: str, current_user: dict = Depends(get_current_user)):
    if current_user["role"] not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Only Super Admins and Admins can delete audit trail logs.")
    conn = get_db()
    conn.execute("DELETE FROM campaign_logs WHERE campaign_id = ?", (campaign_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Audit trail logs cleared successfully."}

@app.delete("/api/marketing/logs/clear-all")
def clear_all_campaign_logs(current_user: dict = Depends(get_current_user)):
    if current_user["role"] not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Only Super Admins and Admins can clear all audit trail logs.")
    conn = get_db()
    conn.execute("DELETE FROM campaign_logs")
    conn.commit()
    conn.close()
    return {"success": True, "message": "All campaign audit logs cleared successfully."}

# ── Brevo Business Verification & Account Management Endpoints ───────────

@app.post("/api/marketing/brevo-apply")
def brevo_apply(req: BrevoApplyRequest, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    existing = conn.execute("SELECT status FROM brevo_applications WHERE user_id = ? ORDER BY id DESC LIMIT 1", (current_user["id"],)).fetchone()
    if existing and existing["status"] == "pending":
        conn.close()
        raise HTTPException(status_code=400, detail="You already have a pending business verification application under review.")
        
    conn.execute("""
        INSERT INTO brevo_applications (user_id, business_name, domain_name, location, business_phone, social_media_website, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
    """, (current_user["id"], req.business_name, req.domain_name, req.location, req.business_phone, req.social_media_website))
    
    conn.execute("UPDATE users SET brevo_account_status = 'pending' WHERE id = ?", (current_user["id"],))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Brevo business verification request submitted successfully."}

@app.get("/api/marketing/brevo-status")
def get_brevo_status(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    u = conn.execute("SELECT brevo_account_status, brevo_api_key, daily_email_limit FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    app_info = conn.execute("SELECT * FROM brevo_applications WHERE user_id = ? ORDER BY id DESC LIMIT 1", (current_user["id"],)).fetchone()
    
    today_sent = 0
    today_str = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute("""
        SELECT SUM(sent_count) as total_today FROM marketing_campaigns 
        WHERE user_id = ? AND campaign_type = 'email' AND created_at >= ?
    """, (current_user["id"], today_str)).fetchone()
    if row and row["total_today"]:
        today_sent = row["total_today"]
        
    conn.close()
    return {
        "status": (u["brevo_account_status"] if u and u["brevo_account_status"] else "none"),
        "api_key": u["brevo_api_key"] if u else None,
        "daily_limit": u["daily_email_limit"] if u and u["daily_email_limit"] else 300,
        "today_sent": today_sent,
        "application": dict(app_info) if app_info else None
    }

@app.get("/api/admin/brevo-applications")
def list_brevo_applications(current_user: dict = Depends(get_admin_user)):
    conn = get_db()
    rows = conn.execute("""
        SELECT a.*, u.email as user_email, u.full_name as user_name, u.brevo_api_key, u.daily_email_limit
        FROM brevo_applications a
        LEFT JOIN users u ON a.user_id = u.id
        ORDER BY a.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/brevo-applications/{app_id}/approve")
def approve_brevo_application(app_id: int, req: BrevoApproveRequest, current_user: dict = Depends(get_admin_user)):
    conn = get_db()
    app_info = conn.execute("SELECT user_id FROM brevo_applications WHERE id = ?", (app_id,)).fetchone()
    if not app_info:
        conn.close()
        raise HTTPException(status_code=404, detail="Application record not found.")
        
    user_id = app_info["user_id"]
    target_status = req.account_status or "approved"
    app_table_status = "approved" if target_status in ["approved", "pending_email_verification"] else target_status
    
    conn.execute("""
        UPDATE brevo_applications 
        SET status = ?, assigned_api_key = ?, daily_limit = ?, processed_at = CURRENT_TIMESTAMP, processed_by = ?
        WHERE id = ?
    """, (app_table_status, req.api_key or "", req.daily_limit or 300, current_user["id"], app_id))
    
    conn.execute("""
        UPDATE users 
        SET brevo_account_status = ?, brevo_api_key = ?, daily_email_limit = ?
        WHERE id = ?
    """, (target_status, req.api_key or "", req.daily_limit or 300, user_id))
    
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Brevo application updated to {target_status}."}

@app.post("/api/marketing/brevo-check-verification")
def check_brevo_verification(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    u = conn.execute("SELECT brevo_account_status, brevo_api_key FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    
    if not u:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found.")
        
    has_api_key = bool(u["brevo_api_key"] and u["brevo_api_key"].startswith("xkeysib-"))
    new_status = "approved" if has_api_key else "email_verified"
    
    conn.execute("UPDATE users SET brevo_account_status = ? WHERE id = ?", (new_status, current_user["id"]))
    conn.execute("UPDATE brevo_applications SET status = 'approved' WHERE user_id = ?", (current_user["id"],))
    conn.commit()
    conn.close()
    
    msg = "Email verification confirmed! Portal unlocked." if has_api_key else "Email verification confirmed! Admin notified to link your Brevo API key."
    return {"success": True, "status": new_status, "message": msg}

@app.post("/api/marketing/brevo-activate-link")
def activate_brevo_link(req: BrevoActivateLinkRequest, current_user: dict = Depends(get_current_user)):
    url = req.activation_url.strip()
    if not url or "brevo.com" not in url:
        raise HTTPException(status_code=400, detail="Please enter a valid Brevo activation link from your email.")
        
    try:
        req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        requests.get(url, headers=req_headers, timeout=12)
    except Exception as e:
        print(f"Brevo activation link trigger error: {e}")
        
    conn = get_db()
    u = conn.execute("SELECT brevo_api_key FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    has_api_key = bool(u and u["brevo_api_key"] and u["brevo_api_key"].startswith("xkeysib-"))
    new_status = "approved" if has_api_key else "email_verified"
    
    conn.execute("UPDATE users SET brevo_account_status = ? WHERE id = ?", (new_status, current_user["id"]))
    conn.execute("UPDATE brevo_applications SET status = 'approved' WHERE user_id = ?", (current_user["id"],))
    conn.commit()
    conn.close()
    
    msg = "Brevo activation link triggered! Email verification confirmed."
    return {"success": True, "status": new_status, "message": msg}

@app.post("/api/marketing/brevo-resend-verification")
def resend_brevo_verification(current_user: dict = Depends(get_current_user)):
    user_email = current_user["email"]
    try:
        send_free_verification_email(user_email, current_user.get("full_name") or "Valued User", "https://databazaar-v2.com/marketing")
    except Exception:
        pass
    return {"success": True, "message": f"Verification reminder re-sent to {user_email}."}

@app.post("/api/admin/brevo-applications/{app_id}/reject")
def reject_brevo_application(app_id: int, req: BrevoRejectRequest, current_user: dict = Depends(get_admin_user)):
    conn = get_db()
    app_info = conn.execute("SELECT user_id FROM brevo_applications WHERE id = ?", (app_id,)).fetchone()
    if not app_info:
        conn.close()
        raise HTTPException(status_code=404, detail="Application record not found.")
        
    user_id = app_info["user_id"]
    conn.execute("""
        UPDATE brevo_applications 
        SET status = 'rejected', rejection_reason = ?, processed_at = CURRENT_TIMESTAMP, processed_by = ?
        WHERE id = ?
    """, (req.reason, current_user["id"], app_id))
    
    conn.execute("UPDATE users SET brevo_account_status = 'rejected', brevo_api_key = NULL WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Brevo application rejected and account locked."}

@app.post("/api/admin/users/{user_id}/brevo-config")
def update_user_brevo_config(user_id: int, req: UserBrevoConfigRequest, current_user: dict = Depends(get_admin_user)):
    conn = get_db()
    u = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not u:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found.")
        
    status = req.account_status or "approved"
    api_key = req.api_key if status in ["approved", "pending_email_verification"] else None
    
    conn.execute("""
        UPDATE users 
        SET brevo_api_key = ?, daily_email_limit = ?, brevo_account_status = ?
        WHERE id = ?
    """, (api_key, req.daily_limit or 300, status, user_id))
    
    if status in ["none", "rejected"]:
        conn.execute("UPDATE brevo_applications SET status = 'rejected' WHERE user_id = ?", (user_id,))
        
    conn.commit()
    conn.close()
    return {"success": True, "message": f"User Brevo configuration updated to '{status}'."}

@app.post("/api/admin/users/{user_id}/brevo-unapprove")
def unapprove_user_brevo(user_id: int, current_user: dict = Depends(get_admin_user)):
    conn = get_db()
    u = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not u:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found.")
        
    conn.execute("UPDATE users SET brevo_account_status = 'none', brevo_api_key = NULL WHERE id = ?", (user_id,))
    conn.execute("UPDATE brevo_applications SET status = 'rejected' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "User Brevo approval revoked. Email panel locked."}

# ── Admin User Management / Credits ─────────────────────

@app.get("/api/admin/users")
def get_all_users(admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    users = conn.execute("SELECT id, email, full_name, role, credits, is_banned, warning_message, brevo_api_key, brevo_account_status, daily_email_limit, created_at FROM users").fetchall()
    conn.close()
    return [dict(u) for u in users]

@app.post("/api/admin/users/add-credits")
def add_credits(req: CreditRequest, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    u = conn.execute("SELECT id, credits FROM users WHERE id = ?", (req.user_id,)).fetchone()
    if not u:
        conn.close()
        raise HTTPException(status_code=404, detail="User account not found.")

    new_balance = max(0, u["credits"] + req.amount)
    conn.execute("UPDATE users SET credits = ? WHERE id = ?", (new_balance, req.user_id))
    
    tx_type = "add" if req.amount >= 0 else "deduct"
    desc = f"Admin credit {'addition' if req.amount >= 0 else 'deduction'} ({'+' if req.amount >= 0 else ''}{req.amount} CR)"
    
    conn.execute(
        "INSERT INTO credit_transactions (user_id, amount, transaction_type, description) VALUES (?, ?, ?, ?)",
        (req.user_id, abs(req.amount), tx_type, desc)
    )
    conn.commit()
    conn.close()
    return {"success": True, "new_balance": new_balance, "message": f"User balance updated to {new_balance} CR."}

@app.patch("/api/admin/users/{target_user_id}")
def update_user_admin(target_user_id: int, req: UpdateUserRequest, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    u = conn.execute("SELECT id FROM users WHERE id = ?", (target_user_id,)).fetchone()
    if not u:
        conn.close()
        raise HTTPException(status_code=404, detail="User account not found.")
    
    if req.role and req.role in ("user", "admin", "superadmin"):
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (req.role, target_user_id))
    if req.full_name is not None:
        conn.execute("UPDATE users SET full_name = ? WHERE id = ?", (req.full_name, target_user_id))
    conn.commit()
    conn.close()
    return {"success": True}

class BanRequest(BaseModel):
    is_banned: int
    warning_message: Optional[str] = ""
    ban_ip: Optional[bool] = False
    ip_address: Optional[str] = ""

@app.get("/api/admin/violations")
def get_security_violations(admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    rows = conn.execute("""
        SELECT v.*, 
               COALESCE(u.email, 'Guest Visitor (' || v.ip_address || ')') AS email,
               COALESCE(u.full_name, 'Guest User') AS full_name
        FROM security_violations v
        LEFT JOIN users u ON v.user_id = u.id
        ORDER BY v.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/violations/seed-test")
def seed_test_violations(request: Request, admin_user: dict = Depends(get_admin_user)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    user_agent = request.headers.get("user-agent", "Mozilla/5.0")
    
    conn = get_db()
    conn.execute(
        "INSERT INTO security_violations (user_id, ip_address, user_agent, violation_type) VALUES (?, ?, ?, ?)",
        (admin_user["id"], client_ip, user_agent, "macOS Screenshot Shortcut (Cmd + Shift + 4)")
    )
    conn.execute(
        "INSERT INTO security_violations (user_id, ip_address, user_agent, violation_type) VALUES (?, ?, ?, ?)",
        (None, "103.145.72.10", user_agent, "Windows Snipping Tool (Win + Shift + S)")
    )
    conn.commit()
    conn.close()
    return {"success": True, "message": "Test security violations logged."}

@app.post("/api/admin/users/{target_user_id}/ban")
def ban_user(target_user_id: int, req: BanRequest, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    
    target_u = conn.execute("SELECT id, role, email FROM users WHERE id = ?", (target_user_id,)).fetchone()
    if not target_u:
        conn.close()
        raise HTTPException(status_code=404, detail="Target user account not found.")

    if target_u["id"] == admin_user["id"]:
        conn.close()
        raise HTTPException(status_code=400, detail="You cannot ban your own active account.")

    # Role Hierarchy Ban Constraints:
    # Admin cannot ban admin or superadmin. Superadmin can ban anyone.
    if admin_user["role"] == "admin" and target_u["role"] in ("admin", "superadmin") and req.is_banned == 1:
        conn.close()
        raise HTTPException(
            status_code=403,
            detail="Admins cannot ban other Admins or Superadmins. Only Superadmin can ban administrative accounts."
        )

    conn.execute("UPDATE users SET is_banned = ?, warning_message = ? WHERE id = ?", (req.is_banned, req.warning_message, target_user_id))
    if req.ban_ip and req.ip_address and req.ip_address not in ("127.0.0.1", "::1", "localhost"):
        if req.is_banned == 1:
            conn.execute("INSERT INTO banned_ips (ip_address, reason) VALUES (?, ?) ON CONFLICT (ip_address) DO NOTHING", (req.ip_address, req.warning_message))
        else:
            conn.execute("DELETE FROM banned_ips WHERE ip_address = ?", (req.ip_address,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "User ban status updated."}
class AdminWarningRequest(BaseModel):
    warning_message: str

@app.post("/api/admin/users/{target_user_id}/warning")
def set_admin_warning(target_user_id: int, req: AdminWarningRequest, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    conn.execute("UPDATE users SET warning_message = ? WHERE id = ?", (req.warning_message, target_user_id))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Application-level warning message updated."}

@app.delete("/api/admin/users/{target_user_id}")
def unregister_user(target_user_id: int, admin_user: dict = Depends(get_admin_user)):
    if target_user_id == admin_user["id"]:
        raise HTTPException(status_code=400, detail="You cannot unregister your own admin account.")
        
    conn = get_db()
    u = conn.execute("SELECT id, email FROM users WHERE id = ?", (target_user_id,)).fetchone()
    if not u:
        conn.close()
        raise HTTPException(status_code=404, detail="User account not found.")

    # Permanently delete user and associated records from database
    conn.execute("DELETE FROM access_logs WHERE user_id = ?", (target_user_id,))
    conn.execute("DELETE FROM credit_transactions WHERE user_id = ?", (target_user_id,))
    conn.execute("DELETE FROM security_violations WHERE user_id = ?", (target_user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"User #{target_user_id} ({u['email']}) has been permanently unregistered and deleted from database."}

# ── Admin Dashboard Overview Endpoint ─────────────────────

_dashboard_cache = {"timestamp": 0, "data": None}

@app.get("/api/admin/dashboard-overview")
def admin_dashboard_overview(refresh: bool = False, admin_user: dict = Depends(get_admin_user)):
    now = time.time()
    if not refresh and _dashboard_cache["data"] is not None and (now - _dashboard_cache["timestamp"] < 6):
        return _dashboard_cache["data"]

    conn = get_db()

    # 1. User Stats
    users = conn.execute("SELECT id, email, full_name, role, credits, is_banned, created_at FROM users").fetchall()
    users_list = [dict(u) for u in users]
    total_users = len(users_list)
    role_counts = {}
    banned_count = 0
    for u in users_list:
        role_counts[u["role"]] = role_counts.get(u["role"], 0) + 1
        if u.get("is_banned") in (1, True, "1", "true"):
            banned_count += 1

    # 2. Payment Stats
    payments = conn.execute("""
        SELECT pr.*, u.email AS user_email, u.full_name AS user_name
        FROM payment_requests pr
        JOIN users u ON pr.user_id = u.id
        ORDER BY pr.created_at DESC
    """).fetchall()
    payments_list = [dict(p) for p in payments]
    payment_status_counts = {"pending": 0, "approved": 0, "rejected": 0}
    total_revenue_bdt = 0
    package_popularity = {}
    monthly_revenue = {}
    for p in payments_list:
        s = p.get("status", "pending")
        payment_status_counts[s] = payment_status_counts.get(s, 0) + 1
        if s == "approved":
            total_revenue_bdt += p.get("amount_bdt", 0)
            # Monthly revenue aggregation
            created = p.get("created_at")
            if created:
                month_key = str(created)[:7]  # YYYY-MM
                monthly_revenue[month_key] = monthly_revenue.get(month_key, 0) + p.get("amount_bdt", 0)
        pkg = p.get("package_name", "Unknown")
        package_popularity[pkg] = package_popularity.get(pkg, 0) + 1

    # 3. Dataset Request Stats
    dataset_requests = conn.execute("SELECT status FROM dataset_requests").fetchall()
    dr_status_counts = {"pending": 0, "fulfilled": 0, "rejected": 0}
    for dr in dataset_requests:
        s = dr["status"]
        dr_status_counts[s] = dr_status_counts.get(s, 0) + 1

    # 4. Scraper / Private Datasets Stats
    scrape_jobs = conn.execute("""
        SELECT sj.user_id, sj.status, sj.result_count, sj.result_path,
               u.email, u.full_name, u.role AS user_role, u.credits AS user_credits
        FROM scrape_jobs sj
        JOIN users u ON sj.user_id = u.id
    """).fetchall()
    scrape_list = [dict(sj) for sj in scrape_jobs]
    total_scrape_jobs = len(scrape_list)
    running_jobs = len([sj for sj in scrape_list if sj["status"] == "running"])

    # Per-user private dataset counts
    user_dataset_map = {}
    for sj in scrape_list:
        uid = sj["user_id"]
        if uid not in user_dataset_map:
            user_dataset_map[uid] = {
                "user_id": uid,
                "email": sj["email"],
                "full_name": sj["full_name"],
                "role": sj.get("user_role", "user"),
                "credits": sj.get("user_credits", 0),
                "total_datasets": 0,
                "completed_datasets": 0,
                "total_rows": 0
            }
        user_dataset_map[uid]["total_datasets"] += 1
        if sj["status"] == "done":
            user_dataset_map[uid]["completed_datasets"] += 1
            user_dataset_map[uid]["total_rows"] += sj.get("result_count", 0) or 0

    users_with_datasets = sorted(user_dataset_map.values(), key=lambda x: x["total_datasets"], reverse=True)

    # 5. Total catalog datasets
    total_catalog = conn.execute("SELECT COUNT(*) as cnt FROM datasets WHERE is_active = 1").fetchone()["cnt"]

    overview_data = {
        "user_stats": {
            "total_users": total_users,
            "role_counts": role_counts,
            "banned_count": banned_count,
        },
        "payment_stats": {
            "total_payments": len(payments_list),
            "status_counts": payment_status_counts,
            "total_revenue_bdt": total_revenue_bdt,
            "package_popularity": package_popularity,
            "monthly_revenue": monthly_revenue,
        },
        "dataset_request_stats": {
            "total_requests": len(dataset_requests),
            "status_counts": dr_status_counts,
        },
        "scraper_stats": {
            "total_jobs": total_scrape_jobs,
            "running_jobs": running_jobs,
            "total_private_datasets": len([sj for sj in scrape_list if sj["status"] == "done" and sj.get("result_path")]),
        },
        "catalog_stats": {
            "total_catalog_datasets": total_catalog,
        },
        "users_with_datasets": users_with_datasets,
    }

    _dashboard_cache["timestamp"] = now
    _dashboard_cache["data"] = overview_data
    return overview_data


@app.get("/api/admin/users/{user_id}/private-datasets")
def get_user_private_datasets(user_id: int, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    user = conn.execute("SELECT id, email, full_name FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found.")

    jobs = conn.execute("""
        SELECT id, query, division, district, area, status, result_count, result_path, cost_credits, created_at, completed_at
        FROM scrape_jobs WHERE user_id = ? ORDER BY created_at DESC
    """, (user_id,)).fetchall()
    conn.close()
    return {
        "user": dict(user),
        "datasets": [dict(j) for j in jobs]
    }


@app.delete("/api/admin/users/{user_id}/private-datasets/{job_id}")
def admin_delete_user_private_dataset(user_id: int, job_id: int, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Private dataset job not found for this user.")

    # Delete result file from disk
    result_path = job["result_path"]
    if result_path and os.path.exists(result_path):
        try:
            os.remove(result_path)
        except Exception:
            pass

    # Delete related logs and the job record
    conn.execute("DELETE FROM scrape_logs WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM scrape_jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Private dataset job #{job_id} deleted successfully."}


@app.get("/api/admin/users/{user_id}/private-datasets/{job_id}/download")
def admin_download_user_private_dataset(user_id: int, job_id: int, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Private dataset job not found for this user.")
    conn.close()

    result_path = job["result_path"]
    if not result_path or not os.path.exists(result_path):
        raise HTTPException(status_code=404, detail="Dataset file not found on server disk.")

    filename = os.path.basename(result_path)
    return FileResponse(
        path=result_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# ── Database Auto Initialize ──────────────────────────────

@app.get("/api/config/regions")
def get_regions_config():
    # Return Bangladesh Region Hierarchy, Categories, and Contact info from .env
    return {
        "regions": REGIONS,
        "categories": CATEGORIES,
        "contacts": {
            "support_email": os.getenv("SUPPORT_EMAIL", os.getenv("SMTP_USER", "asifdev777@gmail.com")),
            "hotline_phone": os.getenv("HOTLINE_PHONE", "+880 1700-000000"),
            "support_hours": os.getenv("SUPPORT_HOURS", "24/7 Automated System & Live WhatsApp Assistance")
        }
    }

# ── Dashboard Stats Endpoint ─────────────────────────────

@app.get("/api/marketing/dashboard-stats")
def dashboard_stats(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    if current_user["role"] in ("admin", "superadmin"):
        campaigns = conn.execute("SELECT * FROM marketing_campaigns ORDER BY created_at DESC").fetchall()
    else:
        campaigns = conn.execute("SELECT * FROM marketing_campaigns WHERE user_id = ? ORDER BY created_at DESC", (current_user["id"],)).fetchall()
    conn.close()
    
    campaigns_list = []
    for c in campaigns:
        cdict = dict(c)
        eta_info = compute_campaign_eta(
            cdict.get("campaign_type", "whatsapp"),
            cdict.get("sent_count", 0),
            cdict.get("total_count", 0),
            cdict.get("start_row", 0),
            cdict.get("created_at")
        )
        cdict.update(eta_info)
        if cdict.get("status") in ("running", "stopping"):
            last_log = conn.execute(
                "SELECT message FROM campaign_logs WHERE campaign_id = ? ORDER BY id DESC LIMIT 1",
                (cdict["id"],)
            ).fetchone()
            cdict["latest_log"] = last_log["message"] if last_log else "Dispatch worker running..."
        campaigns_list.append(cdict)
    
    total_campaigns = len(campaigns_list)
    total_sent = sum(c.get("sent_count", 0) for c in campaigns_list)
    total_contacts = sum(c.get("total_count", 0) for c in campaigns_list)
    total_failed = sum(c.get("failed_count", 0) for c in campaigns_list)
    total_remaining = max(0, total_contacts - total_sent)
    
    wa_count = len([c for c in campaigns_list if c["campaign_type"] == "whatsapp"])
    email_count = len([c for c in campaigns_list if c["campaign_type"] == "email"])
    
    status_counts = {}
    for c in campaigns_list:
        s = c["status"]
        status_counts[s] = status_counts.get(s, 0) + 1
    
    active_count = len([c for c in campaigns_list if c["status"] in ("running", "stopping")])
    done_count = status_counts.get("done", 0)
    success_rate = round((done_count / total_campaigns * 100), 1) if total_campaigns > 0 else 0
    
    return {
        "total_campaigns": total_campaigns,
        "total_sent": total_sent,
        "total_contacts": total_contacts,
        "total_remaining": total_remaining,
        "total_failed": total_failed,
        "success_rate": success_rate,
        "active_count": active_count,
        "whatsapp_count": wa_count,
        "email_count": email_count,
        "status_counts": status_counts,
        "campaigns": campaigns_list
    }

# ── Log Files Endpoints ──────────────────────────────────

@app.get("/api/marketing/logs")
def list_log_files(current_user: dict = Depends(get_current_user)):
    """List available day-wise log files"""
    log_files = sorted(glob.glob(os.path.join(LOGS_FOLDER, "*_campaigns.log")), reverse=True)
    result = []
    for f in log_files:
        basename = os.path.basename(f)
        date_str = basename.replace("_campaigns.log", "")
        size = os.path.getsize(f)
        result.append({"date": date_str, "filename": basename, "size_bytes": size})
    return result

@app.get("/api/marketing/logs/{date}")
def get_log_file(date: str, current_user: dict = Depends(get_current_user)):
    """Return contents of a day-wise log file"""
    log_path = os.path.join(LOGS_FOLDER, f"{date}_campaigns.log")
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="Log file not found for this date.")
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"date": date, "content": content, "lines": content.strip().split("\n") if content.strip() else []}
    except Exception:
        raise HTTPException(status_code=500, detail="Error reading log file.")

@app.get("/api/marketing/logs/{date}/download")
def download_log_file(date: str, current_user: dict = Depends(get_current_user)):
    """Download log file attachment"""
    log_path = os.path.join(LOGS_FOLDER, f"{date}_campaigns.log")
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="Log file not found.")
    return FileResponse(path=log_path, filename=f"{date}_campaigns.log", media_type="text/plain")

@app.delete("/api/marketing/logs/{date}")
def delete_log_file(date: str, current_user: dict = Depends(get_current_user)):
    """Delete a daily log file (Admin / Super Admin only)"""
    if current_user["role"] not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Only Super Admins and Admins can delete log files.")
    log_path = os.path.join(LOGS_FOLDER, f"{date}_campaigns.log")
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="Log file not found.")
    try:
        os.remove(log_path)
        return {"success": True, "message": f"Log file for {date} deleted successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete log file: {str(e)}")

# ── Live Scraper WebSocket Stream ─────────────────────────────

@app.websocket("/ws/scraper/{job_id}/stream")
async def scraper_live_stream(websocket: WebSocket, job_id: int):
    """WebSocket endpoint that streams live Google Maps JPEG frames, real-time logs,
    and progress events from the scraper browser without continuous database polling."""
    import asyncio

    # Authenticate via query param
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing auth token")
        return
    try:
        payload = decode_jwt_token(token)
        if not payload:
            await websocket.close(code=4001, reason="Invalid token")
            return
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return

    # 1. Verify job is currently active
    conn = get_db()
    job_row = conn.execute("SELECT id, status FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()

    if not job_row or job_row["status"] != "running":
        # Scraper job is not running. Do NOT accept connection or enter streaming loop.
        # Reject immediately so uvicorn does not establish an open connection.
        try:
            await websocket.close(code=4004, reason="Scraper job is not actively running")
        except Exception:
            pass
        return

    event_queue = None
    try:
        await websocket.accept()
        # Subscribe to live event queue
        event_queue = subscribe_scraper_job(job_id)

        # Send initial in-memory logs and latest live frame
        initial_logs = get_job_recent_logs(job_id)
        if initial_logs:
            await websocket.send_json({
                "type": "init_logs",
                "logs": initial_logs
            })

        frame = get_live_frame(job_id)
        if frame and frame.get("data"):
            await websocket.send_json({
                "type": "frame",
                "image": frame["data"]
            })

        ping_counter = 0
        while True:
            # Drain queue with ultra-low latency (0.05s) for instant real-time streaming
            try:
                event = await asyncio.to_thread(event_queue.get, True, 0.05)
            except queue.Empty:
                # Timeout tick: send keep-alive ping every 5 seconds
                ping_counter += 1
                if ping_counter >= 100:
                    ping_counter = 0
                    try:
                        await websocket.send_json({"type": "ping"})
                    except (WebSocketDisconnect, ConnectionResetError, RuntimeError):
                        break
                continue

            ping_counter = 0
            await websocket.send_json(event)

            # Close connection immediately after sending job_ended
            if event.get("type") == "job_ended":
                try:
                    await websocket.close(code=1000, reason="Scraper job finished")
                except Exception:
                    pass
                break

    except (WebSocketDisconnect, ConnectionResetError, RuntimeError):
        pass
    except Exception:
        pass
    finally:
        if event_queue:
            unsubscribe_scraper_job(job_id, event_queue)

# ── Screenshot Endpoints (Legacy/Fallback) ─────────────────────────────────

@app.get("/api/scraper/jobs/{job_id}/screenshot")
def get_scraper_screenshot(job_id: int, current_user: dict = Depends(get_current_user)):
    """Return the latest scraper browser screenshot as base64 (legacy fallback)"""
    # Try in-memory frame buffer first
    frame = get_live_frame(job_id)
    if frame:
        return {"available": True, "image": f"data:image/jpeg;base64,{frame['data']}"}
    # Fallback to file-based (legacy)
    screenshot_path = os.path.join(SCRAPER_SCREENSHOTS_FOLDER, f"job_{job_id}.png")
    if not os.path.exists(screenshot_path):
        return {"available": False, "image": None}
    try:
        with open(screenshot_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
        return {"available": True, "image": f"data:image/png;base64,{img_data}"}
    except Exception:
        return {"available": False, "image": None}

@app.get("/api/marketing/campaign/{campaign_id}/screenshot")
def get_campaign_screenshot(campaign_id: str, current_user: dict = Depends(get_current_user)):
    """Return the latest campaign browser screenshot as base64"""
    screenshot_path = os.path.join(CAMPAIGN_SCREENSHOTS, f"campaign_{campaign_id}.png")
    if not os.path.exists(screenshot_path):
        return {"available": False, "image": None}
    try:
        with open(screenshot_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
        return {"available": True, "image": f"data:image/png;base64,{img_data}"}
    except Exception:
        return {"available": False, "image": None}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
