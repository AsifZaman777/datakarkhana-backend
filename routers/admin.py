import os
import time
import shutil
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from database import get_db
from core.constants import UPLOAD_FOLDER
from core.dependencies import get_admin_user, get_user_plan_tier
from supabase_storage import is_supabase_storage_configured, get_bucket_name, get_effective_supabase_url
from schemas.admin import (
    CreditRequest,
    UpdateUserRequest,
    AllowSyncRequest,
    UploadLimitRequest,
    BanRequest,
    AdminWarningRequest,
)
from schemas.marketing import (
    BrevoApproveRequest,
    BrevoRejectRequest,
    UserBrevoConfigRequest,
)

router = APIRouter(tags=["Admin"])


# ── Promotion Requests Endpoints ─────────────────────────

@router.get("/api/admin/promotion-requests")
def list_promotion_requests(admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    # 1. From PostgreSQL datasets table
    rows = conn.execute("""
        SELECT d.id, d.name, d.category, d.division, d.district, d.area, d.row_count, d.file_path,
               d.promotion_status, d.proposed_name, d.proposed_category, d.uploaded_by AS user_id,
               d.source_job_id, d.created_at, u.email AS user_email, u.full_name
        FROM datasets d
        LEFT JOIN users u ON d.uploaded_by = u.id
        WHERE d.promotion_status = 'pending'
        ORDER BY d.created_at DESC
    """).fetchall()

    # 2. From scrape_jobs table (legacy/offline fallback)
    job_rows = conn.execute("""
        SELECT sj.id, sj.query AS name, 'Scraped Leads' AS category, sj.division, sj.district, sj.area, 
               sj.result_count AS row_count, sj.result_path AS file_path, sj.promotion_status, 
               sj.proposed_name, sj.proposed_category, sj.user_id, sj.id AS source_job_id, 
               sj.created_at, u.email AS user_email, u.full_name
        FROM scrape_jobs sj
        LEFT JOIN users u ON sj.user_id = u.id
        WHERE sj.promotion_status = 'pending'
        ORDER BY sj.created_at DESC
    """).fetchall()
    conn.close()

    result = []
    seen_job_ids = set()
    for r in rows:
        if r.get("source_job_id"):
            seen_job_ids.add(r["source_job_id"])
        result.append({
            "id": r["id"],
            "dataset_id": r["id"],
            "job_id": r["source_job_id"] or r["id"],
            "user_id": r["user_id"],
            "user_email": r["user_email"] or "Unknown User",
            "user_name": r["full_name"] or "User",
            "name": r["proposed_name"] or r["name"] or f"Dataset #{r['id']}",
            "category": r["proposed_category"] or r["category"] or "Scraped Leads",
            "status": r["promotion_status"] or "pending",
            "created_at": str(r["created_at"]) if r["created_at"] else "",
            "division": r["division"],
            "district": r["district"],
            "area": r["area"],
            "row_count": r["row_count"] or 0,
            "source_type": "dataset"
        })

    for j in job_rows:
        if j["id"] not in seen_job_ids:
            result.append({
                "id": j["id"],
                "dataset_id": None,
                "job_id": j["id"],
                "user_id": j["user_id"],
                "user_email": j["user_email"] or "Unknown User",
                "user_name": j["full_name"] or "User",
                "name": j["proposed_name"] or j["name"] or j.get("query") or "Scraped Leads",
                "category": j["proposed_category"] or j["category"] or "Scraped Leads",
                "status": j["promotion_status"] or "pending",
                "created_at": str(j["created_at"]) if j["created_at"] else "",
                "division": j["division"],
                "district": j["district"],
                "area": j["area"],
                "row_count": j["row_count"] or 0,
                "source_type": "job"
            })

    return result


@router.post("/api/admin/promotion-requests/{request_id}/approve")
def approve_promotion_request(request_id: int, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    # Check in datasets table first
    ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (request_id,)).fetchone()
    if ds:
        final_name = ds["proposed_name"] or ds["name"]
        final_cat = ds["proposed_category"] or ds["category"] or "General Business"
        conn.execute(
            """UPDATE datasets 
               SET is_active = 1, promotion_status = 'approved', name = ?, category = ? 
               WHERE id = ?""",
            (final_name, final_cat, request_id)
        )
        if ds.get("source_job_id"):
            try:
                conn.execute("UPDATE scrape_jobs SET promotion_status = 'approved' WHERE id = ?", (ds["source_job_id"],))
            except Exception:
                pass
        conn.commit()
        conn.close()
        return {"success": True, "message": f'Dataset "{final_name}" approved & published to public catalog in PostgreSQL!'}

    # If it was a scrape_job id
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (request_id,)).fetchone()
    if job:
        final_name = job["proposed_name"] or job["query"] or f"Dataset #{request_id}"
        final_cat = job["proposed_category"] or "General Business"
        new_filename = f"promoted_{request_id}_{int(time.time())}.xlsx"
        new_path = os.path.join(UPLOAD_FOLDER, new_filename)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        rel_path = f"uploads/{new_filename}"
        if job["result_path"] and os.path.exists(job["result_path"]):
            shutil.copy(job["result_path"], new_path)

        conn.execute(
            """INSERT INTO datasets (name, category, division, district, area, file_path, row_count, column_names, price_credits, is_active, uploaded_by, promotion_status, source_job_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'Name, Phone, Address, Website, Rating, Category, Maps URL, Query', 10, 1, ?, 'approved', ?)""",
            (final_name, final_cat, job["division"], job["district"], job["area"], rel_path, job["result_count"], job["user_id"], request_id)
        )
        conn.execute("UPDATE scrape_jobs SET promotion_status = 'approved' WHERE id = ?", (request_id,))
        conn.commit()
        conn.close()
        return {"success": True, "message": f'Dataset "{final_name}" approved & published to public catalog in PostgreSQL!'}

    conn.close()
    raise HTTPException(status_code=404, detail="Promotion request not found.")


@router.post("/api/admin/promotion-requests/{request_id}/reject")
def reject_promotion_request(request_id: int, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (request_id,)).fetchone()
    if ds:
        file_path = ds["file_path"]
        source_job_id = ds.get("source_job_id")
        
        # Completely remove from PostgreSQL datasets table
        conn.execute("DELETE FROM datasets WHERE id = ?", (request_id,))
        
        # Remove file from cloud uploads folder if it exists
        if file_path:
            backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            full_path = os.path.join(backend_dir, file_path.replace("\\", "/"))
            if os.path.exists(full_path):
                try:
                    os.remove(full_path)
                except Exception:
                    pass

        # Preserve in user's local DB and mark rejected so user knows
        if source_job_id:
            try:
                conn.execute("UPDATE scrape_jobs SET promotion_status = 'rejected' WHERE id = ?", (source_job_id,))
            except Exception:
                pass

        conn.commit()
        conn.close()
        return {
            "success": True, 
            "message": "Promotion request rejected. The dataset was removed from PostgreSQL cloud database and remains usable only in the customer's local DB."
        }

    # If it was a scrape_job id
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (request_id,)).fetchone()
    if job:
        conn.execute("UPDATE scrape_jobs SET promotion_status = 'rejected' WHERE id = ?", (request_id,))
        conn.commit()
        conn.close()
        return {
            "success": True, 
            "message": "Promotion request rejected. The dataset was removed from PostgreSQL and remains usable only in local DB."
        }

    conn.close()
    raise HTTPException(status_code=404, detail="Promotion request not found.")


# ── Brevo Administration Endpoints ───────────────────────

@router.get("/api/admin/brevo-applications")
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


@router.post("/api/admin/brevo-applications/{app_id}/approve")
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


@router.post("/api/admin/brevo-applications/{app_id}/reject")
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


@router.post("/api/admin/users/{user_id}/brevo-config")
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


@router.post("/api/admin/users/{user_id}/brevo-unapprove")
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


# ── Admin User Management & Sync Quotas ──────────────────

@router.get("/api/admin/users")
def get_all_users(admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    users = conn.execute("SELECT id, email, full_name, role, credits, is_banned, warning_message, brevo_api_key, brevo_account_status, daily_email_limit, allow_sync, max_sync_files, created_at FROM users ORDER BY id ASC").fetchall()
    result = []
    for u in users:
        u_dict = dict(u)
        u_dict["plan_tier"] = get_user_plan_tier(conn, u_dict["id"], u_dict["email"], u_dict["role"])
        u_dict["max_sync_files"] = u_dict.get("max_sync_files") if u_dict.get("max_sync_files") is not None else 5
        count_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM datasets WHERE uploaded_by = ? AND (is_synced = 1 OR file_path LIKE ?)",
            (u_dict["id"], "supabase://%")
        ).fetchone()
        u_dict["synced_files_count"] = count_row["cnt"] if count_row else 0
        result.append(u_dict)
    conn.close()
    return result


@router.post("/api/admin/users/{target_user_id}/allow-sync")
def set_user_allow_sync(target_user_id: int, req: AllowSyncRequest, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    u = conn.execute("SELECT id, email FROM users WHERE id = ?", (target_user_id,)).fetchone()
    if not u:
        conn.close()
        raise HTTPException(status_code=404, detail="User account not found.")
    
    val = 1 if req.allow_sync else 0
    conn.execute("UPDATE users SET allow_sync = ? WHERE id = ?", (val, target_user_id))
    conn.commit()
    conn.close()
    return {
        "success": True, 
        "allow_sync": val, 
        "message": f"Cloud sync permission {'enabled' if val == 1 else 'disabled'} for {u['email']}."
    }


@router.post("/api/admin/users/{target_user_id}/upload-limit")
def set_user_upload_limit(target_user_id: int, req: UploadLimitRequest, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    u = conn.execute("SELECT id, email FROM users WHERE id = ?", (target_user_id,)).fetchone()
    if not u:
        conn.close()
        raise HTTPException(status_code=404, detail="User account not found.")

    new_limit = max(0, req.max_sync_files)
    conn.execute("UPDATE users SET max_sync_files = ? WHERE id = ?", (new_limit, target_user_id))
    conn.commit()
    conn.close()
    return {
        "success": True,
        "max_sync_files": new_limit,
        "message": f"Cloud upload limit set to {new_limit} datasets for {u['email']}."
    }


@router.get("/api/admin/users/{target_user_id}/datasets")
def get_user_datasets(target_user_id: int, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    u = conn.execute("SELECT id, email, full_name FROM users WHERE id = ?", (target_user_id,)).fetchone()
    if not u:
        conn.close()
        raise HTTPException(status_code=404, detail="User account not found.")

    datasets = conn.execute(
        """SELECT id, name, category, division, district, area, row_count, file_path, 
                  is_synced, is_active, promotion_status, source_job_id, created_at 
           FROM datasets 
           WHERE uploaded_by = ? 
           ORDER BY id DESC""",
        (target_user_id,)
    ).fetchall()
    
    result = []
    for d in datasets:
        d_dict = dict(d)
        d_dict["is_cloud_stored"] = bool(d_dict.get("file_path") and str(d_dict["file_path"]).startswith("supabase://"))
        result.append(d_dict)

    conn.close()
    return {
        "user": dict(u),
        "total": len(result),
        "datasets": result
    }


@router.get("/api/admin/storage/overview")
def get_storage_overview(admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    
    total_cloud_ds = conn.execute(
        "SELECT COUNT(*) as cnt, COALESCE(SUM(row_count), 0) as total_rows FROM datasets WHERE is_synced = 1 OR file_path LIKE ?",
        ("supabase://%",)
    ).fetchone()
    
    breakdown = conn.execute(
        """SELECT u.id, u.email, u.full_name, u.allow_sync, COALESCE(u.max_sync_files, 5) as max_sync_files,
                  COUNT(d.id) as synced_files, COALESCE(SUM(d.row_count), 0) as total_leads
           FROM users u
           LEFT JOIN datasets d ON d.uploaded_by = u.id AND (d.is_synced = 1 OR d.file_path LIKE ?)
           GROUP BY u.id, u.email, u.full_name, u.allow_sync, u.max_sync_files
           ORDER BY synced_files DESC, u.id ASC""",
        ("supabase://%",)
    ).fetchall()
    
    conn.close()
    return {
        "bucket_name": get_bucket_name(),
        "supabase_configured": is_supabase_storage_configured(),
        "supabase_url": get_effective_supabase_url(),
        "total_cloud_datasets": total_cloud_ds["cnt"] if total_cloud_ds else 0,
        "total_cloud_rows": total_cloud_ds["total_rows"] if total_cloud_ds else 0,
        "users": [dict(b) for b in breakdown]
    }


@router.post("/api/admin/users/add-credits")
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


@router.patch("/api/admin/users/{target_user_id}")
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


@router.post("/api/admin/users/{target_user_id}/ban")
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


@router.post("/api/admin/users/{target_user_id}/warning")
def set_admin_warning(target_user_id: int, req: AdminWarningRequest, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    conn.execute("UPDATE users SET warning_message = ? WHERE id = ?", (req.warning_message, target_user_id))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Application-level warning message updated."}


@router.delete("/api/admin/users/{target_user_id}")
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


# ── Private Datasets Management (Admin) ───────────────────

@router.get("/api/admin/users/{user_id}/private-datasets")
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


@router.delete("/api/admin/users/{user_id}/private-datasets/{job_id}")
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
    return {"success": True, "message": "Private dataset deleted successfully."}


@router.get("/api/admin/users/{user_id}/private-datasets/{job_id}/download")
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


# ── Security Violations Endpoints ─────────────────────────

@router.get("/api/admin/violations")
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


@router.post("/api/admin/violations/seed-test")
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


# ── Admin Dashboard Overview Endpoint ─────────────────────

_dashboard_cache = {"timestamp": 0, "data": None}

@router.get("/api/admin/dashboard-overview")
@router.get("/api/admin/overview")
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
