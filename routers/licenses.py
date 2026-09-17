from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException

from database import get_db
from core.security import verify_password, create_jwt_token
from core.dependencies import get_admin_user, get_current_user, get_optional_current_user
from license_service import generate_production_license, get_current_license_status, verify_license
from schemas.licenses import (
    AdminGenerateLicensePayload,
    AdminExtendLicensePayload,
    ActivateLicensePayload,
    QuickRenewPayload,
)

router = APIRouter(tags=["Licenses"])


# ── Desktop Production License & Admin Key Management Endpoints ──

@router.get("/api/admin/licenses")
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


@router.post("/api/admin/licenses/generate")
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


@router.post("/api/admin/licenses/{license_id}/extend")
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


@router.post("/api/admin/licenses/{license_id}/revoke")
def admin_revoke_license(license_id: int, admin_user: dict = Depends(get_admin_user)):
    """Admin endpoint: revoke an active license"""
    conn = get_db()
    conn.execute("UPDATE licenses SET status = 'revoked' WHERE id = ?", (license_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "License revoked successfully."}


# ── Client-facing Desktop License Status & Activation Endpoints ──

@router.get("/api/license/status")
def get_license_status_endpoint(current_user: Optional[dict] = Depends(get_optional_current_user)):
    """Client endpoint: returns current desktop app license status dynamically for the calling user"""
    return get_current_license_status(current_user)


@router.post("/api/license/activate")
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


@router.post("/api/license/quick-renew")
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


@router.get("/api/license/my-licenses")
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
