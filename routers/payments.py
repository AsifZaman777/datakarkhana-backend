import os
import json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Form, UploadFile, File

from database import get_db
from core.constants import UPLOAD_FOLDER
from core.dependencies import get_current_user, get_admin_user, normalize_plan_tier, get_tier_permissions
from services.email_service import send_license_key_email
from license_service import generate_production_license
from schemas.payments import (
    SavePackagesPayload,
    PaymentRequestPayload,
    ApprovePaymentPayload,
)
from config import (
    BKASH_NUMBER,
    BKASH_ACCOUNT_TYPE,
    PATHAO_NUMBER,
    PATHAO_ACCOUNT_TYPE,
    load_credit_packages_config,
)

router = APIRouter(tags=["Payments & Packages"])

def get_payment_gateway_settings():
    conn = get_db()
    rows = conn.execute("SELECT setting_key, setting_value FROM payment_settings").fetchall()
    conn.close()
    
    settings_dict = {row["setting_key"]: row["setting_value"] for row in rows if row["setting_value"]}
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


@router.get("/api/config/payment-gateways")
@router.get("/api/payments/packages-config")
def get_payment_packages_config():
    return get_payment_gateway_settings()


@router.post("/api/admin/payment-settings")
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


@router.post("/api/admin/package-settings")
def save_admin_package_settings(req: SavePackagesPayload, admin_user: dict = Depends(get_admin_user)):
    if admin_user.get("role") not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Superadmin or Admin permission required.")

    pkg_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packages.json")
    try:
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


@router.get("/api/config/packages")
def get_packages_config():
    pkg_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packages.json")
    if os.path.exists(pkg_file):
        with open(pkg_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"packages": []}


@router.post("/api/payments/submit")
@router.post("/api/payments/submit-request")
def submit_payment_request(req: PaymentRequestPayload, current_user: dict = Depends(get_current_user)):
    if not req.transaction_id or not req.bkash_number:
        raise HTTPException(status_code=400, detail="Sender Phone Number and Transaction ID (TrxID) are required.")
        
    conn = get_db()
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


@router.get("/api/payments/my-requests")
def list_my_payment_requests(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM payment_requests WHERE user_id = ? ORDER BY created_at DESC", (current_user["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/api/admin/payments")
@router.get("/api/admin/payment-requests")
def list_admin_payment_requests(admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    rows = conn.execute("""
        SELECT pr.*, u.email AS user_email, u.full_name FROM payment_requests pr
        JOIN users u ON pr.user_id = u.id
        ORDER BY CASE WHEN pr.status = 'pending' THEN 0 ELSE 1 END, pr.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/api/admin/payment-requests/{request_id}/approve")
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

    user_row = conn.execute("SELECT email, full_name FROM users WHERE id = ?", (req["user_id"],)).fetchone()
    customer_email = user_row["email"] if user_row else ""
    customer_name = (user_row["full_name"] if user_row else "") or req.get("user_name") or "Customer"

    credits_amount = req["credits_requested"]
    plan_name = req["package_name"] or "Pro"
    tier = normalize_plan_tier(plan_name)

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
        plan_tier=tier,
        credits_amount=credits_amount
    )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """UPDATE payment_requests 
           SET status = 'approved', processed_at = ?, processed_by = ?, production_key = ?, license_expiry = ?
           WHERE id = ?""",
        (now_str, admin_user["id"], license_info["production_key"], license_info["expires_at"], request_id)
    )

    # Automatically configure sync and dataset permissions for Pro/Enterprise tier users
    tier_perm = get_tier_permissions(conn, tier)
    if tier in ("pro", "enterprise") or tier_perm.get("allow_sync"):
        target_quota = tier_perm.get("max_sync_files", 5)
        conn.execute(
            """UPDATE users 
               SET allow_sync = 1,
                   max_sync_files = CASE WHEN max_sync_files IS NULL OR max_sync_files < ? THEN ? ELSE max_sync_files END
               WHERE id = ?""",
            (target_quota, target_quota, req["user_id"])
        )

    conn.commit()
    conn.close()

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


@router.post("/api/admin/payment-requests/{request_id}/reject")
def reject_payment_request(request_id: int, rejection_reason: str = Form("Transaction ID mismatch or invalid payment"), admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    req = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
    if not req:
        conn.close()
        raise HTTPException(status_code=404, detail="Payment request not found.")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE payment_requests SET status = 'rejected', rejection_reason = ?, processed_at = ?, processed_by = ? WHERE id = ?",
        (rejection_reason, now_str, admin_user["id"], request_id)
    )
    conn.commit()
    conn.close()
    return {"success": True, "message": "Payment request rejected."}
