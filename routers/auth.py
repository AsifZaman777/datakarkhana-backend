import os
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status

from database import get_db, is_sqlite_active
from core.security import hash_password, verify_password, create_jwt_token, decode_jwt_token
from core.dependencies import get_current_user, get_user_plan_tier
from services.email_service import generate_otp, send_free_verification_email
from schemas.auth import (
    RegisterRequest,
    LoginRequest,
    VerifyOtpRequest,
    ResendVerificationRequest,
    DeductCreditRequest,
    ViolationRequest,
)

router = APIRouter(tags=["Authentication & Security"])

@router.post("/api/auth/register")
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


@router.post("/api/auth/verify-otp")
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


@router.get("/api/auth/verify-email")
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


@router.post("/api/auth/resend-verification")
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


@router.post("/api/auth/login")
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

    plan_tier = get_user_plan_tier(conn, user["id"], user["email"], user["role"])
    allow_sync = user.get("allow_sync", 0)
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
            "is_banned": user.get("is_banned", 0),
            "warning_message": user.get("warning_message") or "",
            "allow_sync": allow_sync,
            "plan_tier": plan_tier
        }
    }


@router.get("/api/auth/me")
def me(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    plan_tier = get_user_plan_tier(conn, current_user["id"], current_user["email"], current_user["role"])
    u_row = conn.execute("SELECT allow_sync FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    allow_sync = u_row["allow_sync"] if u_row and "allow_sync" in u_row.keys() else current_user.get("allow_sync", 0)
    conn.close()
    return {
        "id": current_user["id"],
        "email": current_user["email"],
        "full_name": current_user["full_name"],
        "role": current_user["role"],
        "credits": current_user["credits"],
        "is_verified": current_user.get("is_verified", 1),
        "is_banned": current_user.get("is_banned", 0),
        "warning_message": current_user.get("warning_message") or "",
        "allow_sync": allow_sync,
        "plan_tier": plan_tier
    }


@router.post("/api/user/deduct-credits")
def deduct_user_credits(
    req: DeductCreditRequest,
    current_user: dict = Depends(get_current_user)
):
    """Authoritative cloud endpoint to deduct credits from user account"""
    if req.amount <= 0:
        return {"success": True, "credits": current_user.get("credits", 0)}
    if current_user.get("role") in ("admin", "superadmin"):
        return {"success": True, "credits": current_user.get("credits", 99999)}

    conn = get_db()
    try:
        user = conn.execute("SELECT credits FROM users WHERE id = ?", (current_user["id"],)).fetchone()
        current_credits = user["credits"] if user else 0
        if current_credits < req.amount:
            conn.close()
            raise HTTPException(status_code=403, detail=f"Insufficient credits (has {current_credits}, requires {req.amount}).")
        conn.execute("UPDATE users SET credits = credits - ? WHERE id = ?", (req.amount, current_user["id"]))
        conn.execute(
            "INSERT INTO credit_transactions (user_id, amount, transaction_type, description) VALUES (?, ?, 'deduct', ?)",
            (current_user["id"], req.amount, req.description)
        )
        conn.commit()
        updated_user = conn.execute("SELECT credits FROM users WHERE id = ?", (current_user["id"],)).fetchone()
        return {"success": True, "credits": updated_user["credits"] if updated_user else current_credits - req.amount}
    finally:
        conn.close()


def sync_credit_deduction_to_cloud(auth_token: Optional[str], amount: int, description: str):
    """If running in local SQLite mode, syncs credit deduction to Render Cloud API"""
    if not auth_token or not is_sqlite_active():
        return
    cloud_url = os.getenv("RENDER_EXTERNAL_URL") or "https://datakarkhana-backend.onrender.com"
    try:
        import requests
        requests.post(
            f"{cloud_url.rstrip('/')}/api/user/deduct-credits",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"amount": amount, "description": description},
            timeout=5
        )
    except Exception as e:
        print(f"[CLOUD CREDIT DEDUCT NOTICE] {e}")


@router.post("/api/security/log-violation")
def log_security_violation(req: ViolationRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    
    user_id = None
    auth_header = request.headers.get("authorization", "")
    if auth_header and auth_header.startswith("Bearer "):
        tok = auth_header.split(" ", 1)[1]
        payload = decode_jwt_token(tok)
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
