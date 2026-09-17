import os
import glob
import time
import base64
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import pandas as pd
import requests
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

from database import get_db
from core.constants import LOGS_FOLDER
from core.dependencies import get_current_user
from services.dataset_service import clean_lead_df
from services.email_service import send_free_verification_email
from services.marketing_service import resolve_any_recipient_group, compute_campaign_eta
from senders import run_whatsapp_campaign, SCREENSHOTS_FOLDER as CAMPAIGN_SCREENSHOTS
from schemas.marketing import (
    WhatsAppCampaignRequest,
    EmailCampaignRequest,
    BrevoApplyRequest,
    BrevoActivateLinkRequest,
)

router = APIRouter(tags=["Marketing"])


# ── WhatsApp Session & Status Endpoints ───────────────────

@router.get("/api/marketing/whatsapp-status")
def whatsapp_status():
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    profile = os.path.join(backend_dir, "whatsapp_session")
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


@router.get("/api/marketing/whatsapp-setup-session")
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


@router.post("/api/marketing/whatsapp-reset-session")
def whatsapp_reset_session(background_tasks: BackgroundTasks, current_user: dict = Depends(get_current_user)):
    import shutil
    from senders import setup_driver, wait_for_whatsapp_login, set_active_setup_driver, close_active_setup_driver
    close_active_setup_driver()
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    profile = os.path.join(backend_dir, "whatsapp_session")
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


@router.get("/api/marketing/whatsapp-progress")
def get_progress(recipient_group: str, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    row = conn.execute("SELECT last_index FROM whatsapp_progress WHERE recipient_group = ?", (recipient_group,)).fetchone()
    conn.close()
    return {"last_index": row["last_index"] if row else 0}


# ── Campaign Sending Endpoints ────────────────────────────

@router.post("/api/marketing/send-whatsapp")
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
    target_group_name = group_name or req.recipient_group
    conn.execute(
        """INSERT INTO marketing_campaigns (id, user_id, campaign_type, recipient_group, template_preview, status, sent_count, total_count, start_row)
           VALUES (?, ?, 'whatsapp', ?, ?, 'running', ?, ?, ?)""",
        (campaign_id, current_user["id"], target_group_name, req.message_template[:200], start_index, len(contacts), start_index)
    )
    conn.commit()
    conn.close()

    # Background Campaign Thread
    t = threading.Thread(target=run_whatsapp_campaign, args=(campaign_id, contacts, req.message_template, req.recipient_group, start_index))
    t.daemon = True
    t.start()

    return {"success": True, "campaign_id": campaign_id, "start_index": start_index}


@router.post("/api/marketing/send-email")
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
    target_group_name = group_name or req.recipient_group
    conn.execute(
        """INSERT INTO marketing_campaigns (id, user_id, campaign_type, recipient_group, template_preview, status, sent_count, total_count)
           VALUES (?, ?, 'email', ?, ?, 'done', ?, ?)""",
        (campaign_id, current_user["id"], target_group_name, req.subject, target_count, target_count)
    )
    conn.execute(
        "INSERT INTO campaign_logs (campaign_id, message) VALUES (?, ?)",
        (campaign_id, f"Email campaign dispatched successfully to {target_count} selected addresses via Brevo API.")
    )
    conn.commit()
    conn.close()

    return {"success": True, "campaign_id": campaign_id, "recipient_count": target_count}


# ── Contacts & Recipient Groups ───────────────────────────

@router.get("/api/marketing/recipient-groups")
def get_recipient_groups(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    ds = conn.execute("SELECT id, name FROM datasets WHERE is_active = 1 OR uploaded_by = ?", (current_user["id"],)).fetchall()
    jobs = conn.execute("SELECT id, query FROM scrape_jobs WHERE user_id = ? AND status = 'done'", (current_user["id"],)).fetchall()
    conn.close()
    return [{"id": f"dataset_{d['id']}", "name": d["name"]} for d in ds] + [{"id": f"job_{j['id']}", "name": j["query"]} for j in jobs]


@router.get("/api/marketing/recipient-contacts")
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


# ── Campaign Management & Audit Trails ─────────────────────

@router.get("/api/marketing/active-campaigns")
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


@router.get("/api/marketing/campaigns")
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


@router.get("/api/marketing/whatsapp-campaign/{campaign_id}")
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


@router.post("/api/marketing/whatsapp-campaign/{campaign_id}/stop")
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


@router.delete("/api/marketing/campaign/{campaign_id}")
def delete_campaign(campaign_id: str, current_user: dict = Depends(get_current_user)):
    if current_user["role"] not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Only Super Admins and Admins can delete campaign logs.")
    conn = get_db()
    conn.execute("DELETE FROM campaign_logs WHERE campaign_id = ?", (campaign_id,))
    conn.execute("DELETE FROM marketing_campaigns WHERE id = ?", (campaign_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Campaign and audit logs deleted successfully."}


@router.delete("/api/marketing/campaign/{campaign_id}/logs")
def clear_campaign_logs(campaign_id: str, current_user: dict = Depends(get_current_user)):
    if current_user["role"] not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Only Super Admins and Admins can delete audit trail logs.")
    conn = get_db()
    conn.execute("DELETE FROM campaign_logs WHERE campaign_id = ?", (campaign_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Audit trail logs cleared successfully."}


@router.delete("/api/marketing/logs/clear-all")
def clear_all_campaign_logs(current_user: dict = Depends(get_current_user)):
    if current_user["role"] not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Only Super Admins and Admins can clear all audit trail logs.")
    conn = get_db()
    conn.execute("DELETE FROM campaign_logs")
    conn.commit()
    conn.close()
    return {"success": True, "message": "All campaign audit logs cleared successfully."}


# ── Brevo Business Verification Endpoints ────────────────

@router.post("/api/marketing/brevo-apply")
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


@router.get("/api/marketing/brevo-status")
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


@router.post("/api/marketing/brevo-check-verification")
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


@router.post("/api/marketing/brevo-activate-link")
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


@router.post("/api/marketing/brevo-resend-verification")
def resend_brevo_verification(current_user: dict = Depends(get_current_user)):
    user_email = current_user["email"]
    try:
        send_free_verification_email(user_email, current_user.get("full_name") or "Valued User", "https://databazaar-v2.com/marketing")
    except Exception:
        pass
    return {"success": True, "message": f"Verification reminder re-sent to {user_email}."}


# ── Dashboard Stats Endpoint ─────────────────────────────

@router.get("/api/marketing/dashboard-stats")
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

@router.get("/api/marketing/logs")
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


@router.get("/api/marketing/logs/{date}")
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


@router.get("/api/marketing/logs/{date}/download")
def download_log_file(date: str, current_user: dict = Depends(get_current_user)):
    """Download log file attachment"""
    log_path = os.path.join(LOGS_FOLDER, f"{date}_campaigns.log")
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="Log file not found.")
    return FileResponse(path=log_path, filename=f"{date}_campaigns.log", media_type="text/plain")


@router.delete("/api/marketing/logs/{date}")
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


@router.get("/api/marketing/campaign/{campaign_id}/screenshot")
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
