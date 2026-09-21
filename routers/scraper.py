import os
import re
import time
import math
import queue
import shutil
import base64
import asyncio
from typing import Optional, List
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Header, BackgroundTasks, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from database import get_db
from core.constants import UPLOAD_FOLDER, SCRAPE_RESULTS_FOLDER, SCRAPER_SCREENSHOTS_FOLDER
from core.security import decode_jwt_token
from core.dependencies import get_current_user, check_desktop_license, get_user_plan_tier, get_user_effective_permissions
from services.email_service import send_custom_notification
from services.scraper_service import run_background_scrape, run_background_daraz_scrape
from services.marketing_service import resolve_any_recipient_group
from schemas.scraper import ScrapeRequest, DarazScrapeRequest
from schemas.datasets import DatasetRequestCreate, DatasetRequestStatusUpdate
from scraper import (
    stop_scraper_job,
    get_live_frame,
    subscribe_scraper_job,
    unsubscribe_scraper_job,
    get_job_recent_logs,
)
from routers.auth import sync_credit_deduction_to_cloud

router = APIRouter(tags=["Scraper & Requests"])

@router.post("/api/scraper/requests")
@router.post("/api/requests/submit")
@router.post("/api/requests")
def submit_dataset_request(req: DatasetRequestCreate, current_user: dict = Depends(get_current_user)):
    name_query = getattr(req, "category_query", None) or getattr(req, "name", "")
    if not name_query or not name_query.strip():
        raise HTTPException(status_code=400, detail="Required data / category query cannot be empty.")
    
    phone = getattr(req, "phone", None) or current_user.get("email", "")
    business_name = getattr(req, "business_name", None) or ""
    notes = getattr(req, "additional_notes", None) or getattr(req, "notes", None) or ""

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO dataset_requests (
            user_id, user_email, full_name, phone, business_name,
            category_query, division, district, area, additional_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        current_user["id"], current_user["email"], current_user.get("full_name", "User"),
        str(phone).strip(), str(business_name).strip(),
        str(name_query).strip(), (req.division or "").strip(),
        (req.district or "").strip(), (req.area or "").strip(),
        str(notes).strip()
    ))
    conn.commit()
    request_id = cursor.lastrowid
    conn.close()
    return {"success": True, "request_id": request_id, "message": "Dataset request submitted successfully! Our data team will review and drop this dataset into the Public Catalog."}


@router.get("/api/requests/my-requests")
def get_my_dataset_requests(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM dataset_requests WHERE user_id = ? ORDER BY created_at DESC", (current_user["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/api/requests/admin/list")
def list_dataset_requests(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin authorization required.")
    conn = get_db()
    rows = conn.execute("SELECT dr.*, u.credits FROM dataset_requests dr JOIN users u ON dr.user_id = u.id ORDER BY dr.created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/api/requests/admin/{request_id}/status")
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

    notify_channel = getattr(req, "notify_channel", "none")
    custom_message = getattr(req, "custom_message", "")
    if notify_channel and notify_channel != "none" and custom_message and custom_message.strip():
        subj = f"Dataset Request #{request_id} Update: {req.status.upper()}"
        send_custom_notification(
            recipient_email=req_row["user_email"],
            recipient_phone=req_row["phone"],
            subject=subj,
            message=custom_message.strip(),
            channel=notify_channel
        )

    return {"success": True, "message": f"Dataset request status updated to '{req.status}'!"}


@router.post("/api/scraper/scrape")
def trigger_scrape(
    req: ScrapeRequest,
    background_tasks: BackgroundTasks,
    request: Request,
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
        auth_header = request.headers.get("authorization", "")
        raw_tok = auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else None
        sync_credit_deduction_to_cloud(raw_tok, cost, f"Google Maps Scraper Run ({len(query_list)} queries)")
    
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


@router.post("/api/scraper/daraz/scrape")
def trigger_daraz_scrape(
    req: DarazScrapeRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    current_user: dict = Depends(get_current_user),
    _license_valid: bool = Depends(check_desktop_license)
):
    query = req.query.strip() if req.query else ""
    if not query:
        raise HTTPException(status_code=400, detail="Please provide a valid product search query for Daraz.")

    pages = max(1, req.pages or 1)
    # 20 credits per 10 pages (1-10 pages = 20 credits, 11-20 pages = 40 credits)
    cost = math.ceil(pages / 10.0) * 20

    conn = get_db()
    if current_user["role"] not in ("admin", "superadmin"):
        if current_user["credits"] < cost:
            conn.close()
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient credits to run Daraz scraper (requires {cost} credits for {pages} pages)."
            )

        conn.execute("UPDATE users SET credits = credits - ? WHERE id = ?", (cost, current_user["id"]))
        conn.execute(
            "INSERT INTO credit_transactions (user_id, amount, transaction_type, description) VALUES (?, ?, 'deduct', ?)",
            (current_user["id"], cost, f"Daraz Scraper Run ({pages} pages, query: '{query}')")
        )
        auth_header = request.headers.get("authorization", "")
        raw_tok = auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else None
        sync_credit_deduction_to_cloud(raw_tok, cost, f"Daraz Scraper Run ({pages} pages, query: '{query}')")

    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO scrape_jobs (user_id, query, scraper_type, status, cost_credits) VALUES (?, ?, 'daraz', 'running', ?)",
        (current_user["id"], query, cost)
    )
    conn.commit()
    job_id = cursor.lastrowid
    conn.close()

    background_tasks.add_task(
        run_background_daraz_scrape,
        job_id,
        query,
        pages,
        req.max_items,
        req.headless if req.headless is not None else True
    )
    return {"success": True, "job_id": job_id}


@router.get("/api/scraper/daraz/jobs")
def get_daraz_jobs(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    if current_user["role"] in ("admin", "superadmin"):
        jobs = conn.execute(
            "SELECT sj.*, u.email FROM scrape_jobs sj JOIN users u ON sj.user_id = u.id WHERE sj.scraper_type = 'daraz' ORDER BY sj.created_at DESC"
        ).fetchall()
    else:
        jobs = conn.execute(
            "SELECT * FROM scrape_jobs WHERE user_id = ? AND scraper_type = 'daraz' ORDER BY created_at DESC",
            (current_user["id"],)
        ).fetchall()
    conn.close()
    return [dict(j) for j in jobs]


@router.get("/api/scraper/jobs")
def get_jobs(scraper_type: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    type_clause = ""
    params = []
    if scraper_type:
        type_clause = "WHERE sj.scraper_type = ?" if current_user["role"] in ("admin", "superadmin") else "AND scraper_type = ?"
        params.append(scraper_type)

    if current_user["role"] in ("admin", "superadmin"):
        query = f"SELECT sj.*, u.email FROM scrape_jobs sj JOIN users u ON sj.user_id = u.id {type_clause} ORDER BY sj.created_at DESC"
        jobs = conn.execute(query, tuple(params) if params else None).fetchall()
    else:
        query = f"SELECT * FROM scrape_jobs WHERE user_id = ? {type_clause} ORDER BY created_at DESC"
        all_params = [current_user["id"]] + params
        jobs = conn.execute(query, tuple(all_params)).fetchall()
    conn.close()
    return [dict(j) for j in jobs]


@router.get("/api/scraper/jobs/{job_id}/status")
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


@router.delete("/api/scraper/jobs/{job_id}")
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


@router.post("/api/scraper/jobs/{job_id}/stop")
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


@router.get("/api/scraper/jobs/{job_id}/data")
def get_job_scraped_data(job_id: int, current_user: dict = Depends(get_current_user)):
    """Return parsed leads or product data from the job's excel spreadsheet for private catalog viewing"""
    conn = get_db()
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if not job:
        raise HTTPException(status_code=404, detail="Scrape job not found.")

    if current_user["role"] not in ("admin", "superadmin") and job["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Permission denied.")
    
    job_dict = dict(job)
    scraper_type = job_dict.get("scraper_type") or "google_maps"

    if not job["result_path"] or not os.path.exists(job["result_path"]):
        return {"job_id": job_id, "query": job["query"], "scraper_type": scraper_type, "count": 0, "data": []}

    try:
        df = pd.read_excel(job["result_path"])
        df = df.fillna("")
        records = df.to_dict(orient="records")
        return {
            "job_id": job_id,
            "query": job["query"],
            "scraper_type": scraper_type,
            "division": job_dict.get("division") or "",
            "district": job_dict.get("district") or "",
            "area": job_dict.get("area") or "",
            "count": len(records),
            "data": records
        }
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"Failed to parse job Excel data: {ex}")


@router.get("/api/scraper/jobs/{job_id}/download")
def download_job_excel(job_id: int, request: Request, token: Optional[str] = None, authorization: Optional[str] = Header(None)):
    """Download scraped Excel dataset file (restricted to Pro/Enterprise/Admin for Daraz e-commerce data)"""
    current_user = get_current_user(request=request, authorization=authorization, token=token)
    conn = get_db()
    job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Scrape job record not found.")

    if current_user["role"] not in ("admin", "superadmin") and job["user_id"] != current_user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Permission denied.")

    job_dict = dict(job)
    is_daraz = (job_dict.get("scraper_type") == "daraz") or ("_daraz_" in str(job_dict.get("result_path") or ""))
    if is_daraz and current_user["role"] not in ("admin", "superadmin"):
        eff_perms = get_user_effective_permissions(conn, current_user)
        if not eff_perms.get("allow_daraz_download"):
            conn.close()
            raise HTTPException(
                status_code=403,
                detail="Downloading raw Daraz Excel spreadsheets is available exclusively for Pro and Enterprise subscribers. You can view all records directly in your Private Catalogue."
            )

    conn.close()

    file_path = job["result_path"] if job and "result_path" in job.keys() and job["result_path"] else None
    if file_path and not os.path.exists(file_path):
        fn = os.path.basename(file_path.replace("\\", "/"))
        for candidate in [
            os.path.join(SCRAPE_RESULTS_FOLDER, fn),
            os.path.join(UPLOAD_FOLDER, fn),
        ]:
            if candidate and os.path.exists(candidate):
                file_path = candidate
                break

    if not file_path and os.path.exists(SCRAPE_RESULTS_FOLDER):
        for fname in os.listdir(SCRAPE_RESULTS_FOLDER):
            if f"_{job_id}." in fname or f"job_{job_id}" in fname or f"job_daraz_{job_id}" in fname:
                file_path = os.path.join(SCRAPE_RESULTS_FOLDER, fname)
                break

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Result file not found or job incomplete.")

    sanitized_query = re.sub(r'[^a-zA-Z0-9_\-]', '_', job["query"] or "data")
    prefix = "daraz" if is_daraz else "scraped"
    filename = f"{prefix}_{sanitized_query}_job{job_id}.xlsx"
    return FileResponse(
        file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename
    )


@router.post("/api/scraper/jobs/{job_id}/promote")
@router.post("/api/scraper/jobs/{job_id}/request-promote")
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
        raise HTTPException(status_code=400, detail="Cannot promote job because result file is missing.")

    if job["status"] not in ("done", "stopped"):
        conn.close()
        raise HTTPException(status_code=400, detail="Job must be completed or stopped before promoting.")

    is_admin = current_user["role"] in ("admin", "superadmin")
    if not is_admin and job["user_id"] != current_user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have permission to promote this dataset.")

    final_name = name or job["proposed_name"] or job["query"] or f"Scraped Dataset #{job_id}"
    final_category = category or job["proposed_category"] or "Scraped Leads"

    # Forward to PostgreSQL datasets table
    existing_ds = conn.execute("SELECT id FROM datasets WHERE source_job_id = ? OR file_path LIKE ?", (job_id, f"%promoted_{job_id}_%")).fetchone()

    new_filename = f"promoted_{job_id}_{int(time.time())}.xlsx"
    new_path = os.path.join(UPLOAD_FOLDER, new_filename)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    if job["result_path"] and os.path.exists(job["result_path"]):
        shutil.copy(job["result_path"], new_path)
    rel_path = f"uploads/{new_filename}"

    if is_admin:
        if existing_ds:
            conn.execute("UPDATE datasets SET is_active = 1, promotion_status = 'approved', name = ?, category = ? WHERE id = ?", (final_name, final_category, existing_ds["id"]))
            ds_id = existing_ds["id"]
        else:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO datasets (name, category, division, district, area, file_path, row_count, column_names, price_credits, is_active, uploaded_by, promotion_status, source_job_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'Name, Phone, Address, Website, Rating, Category, Maps URL, Query', 10, 1, ?, 'approved', ?)""",
                (final_name, final_category, job["division"], job["district"], job["area"], rel_path, job["result_count"], job["user_id"], job_id)
            )
            ds_id = cursor.lastrowid
        conn.execute("UPDATE scrape_jobs SET promotion_status = 'approved', proposed_name = ?, proposed_category = ? WHERE id = ?", (final_name, final_category, job_id))
        conn.commit()
        conn.close()
        return {"success": True, "dataset_id": ds_id, "message": f"Dataset '{final_name}' published directly to Public Catalog!"}
    else:
        if existing_ds:
            conn.execute("UPDATE datasets SET promotion_status = 'pending', proposed_name = ?, proposed_category = ? WHERE id = ?", (final_name, final_category, existing_ds["id"]))
            ds_id = existing_ds["id"]
        else:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO datasets (name, category, division, district, area, file_path, row_count, column_names, price_credits, is_active, uploaded_by, promotion_status, proposed_name, proposed_category, source_job_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'Name, Phone, Address, Website, Rating, Category, Maps URL, Query', 10, 0, ?, 'pending', ?, ?, ?)""",
                (final_name, final_category, job["division"], job["district"], job["area"], rel_path, job["result_count"], job["user_id"], final_name, final_category, job_id)
            )
            ds_id = cursor.lastrowid
        conn.execute("UPDATE scrape_jobs SET promotion_status = 'pending', proposed_name = ?, proposed_category = ? WHERE id = ?", (final_name, final_category, job_id))
        conn.commit()
        conn.close()
        return {"success": True, "dataset_id": ds_id, "message": "Promotion request submitted successfully! An administrator will review and publish it."}


@router.get("/api/scraper/jobs/{job_id}/screenshot")
def get_scraper_screenshot(job_id: int, current_user: dict = Depends(get_current_user)):
    """Return the latest scraper browser screenshot as base64 (legacy fallback)"""
    frame = get_live_frame(job_id)
    if frame:
        return {"available": True, "image": f"data:image/jpeg;base64,{frame['data']}"}
    screenshot_path = os.path.join(SCRAPER_SCREENSHOTS_FOLDER, f"job_{job_id}.png")
    if not os.path.exists(screenshot_path):
        return {"available": False, "image": None}
    try:
        with open(screenshot_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
        return {"available": True, "image": f"data:image/png;base64,{img_data}"}
    except Exception:
        return {"available": False, "image": None}


@router.websocket("/ws/scraper/{job_id}/stream")
async def scraper_live_stream(websocket: WebSocket, job_id: int):
    """WebSocket endpoint that streams live Google Maps JPEG frames, real-time logs,
    and progress events from the scraper browser without continuous database polling."""
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
        try:
            await websocket.close(code=4004, reason="Scraper job is not actively running")
        except Exception:
            pass
        return

    event_queue = None
    try:
        await websocket.accept()
        event_queue = subscribe_scraper_job(job_id)

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
            try:
                event = await asyncio.to_thread(event_queue.get, True, 0.05)
            except queue.Empty:
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
