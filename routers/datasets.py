import os
import io
import sys
import time
from typing import Optional
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File, Form, Request

from database import get_db, is_sqlite_active
from core.constants import UPLOAD_FOLDER, SCRAPE_RESULTS_FOLDER
from core.security import decode_jwt_token
from core.dependencies import get_current_user, get_admin_user, get_user_plan_tier, user_can_sync_to_cloud
from services.dataset_service import (
    resolve_dataset_file_path,
    clean_lead_df,
    generate_export_response,
    read_raw_df_from_bytes,
    format_and_clean_df,
    df_to_excel_bytes,
    inspect_excel_file,
)

router = APIRouter(tags=["Datasets"])

def _get_supabase_storage_configured():
    m = sys.modules.get("main")
    if m and hasattr(m, "is_supabase_storage_configured"):
        return m.is_supabase_storage_configured
    from supabase_storage import is_supabase_storage_configured
    return is_supabase_storage_configured

def _get_supabase_upload():
    m = sys.modules.get("main")
    if m and hasattr(m, "upload_dataset_file"):
        return m.upload_dataset_file
    from supabase_storage import upload_dataset_file
    return upload_dataset_file

def _get_supabase_delete():
    m = sys.modules.get("main")
    if m and hasattr(m, "delete_dataset_file"):
        return m.delete_dataset_file
    from supabase_storage import delete_dataset_file
    return delete_dataset_file


@router.get("/api/datasets")
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


@router.get("/api/datasets/my-private")
def get_my_private_datasets(current_user: dict = Depends(get_current_user)):
    """Get demoted/private datasets owned by current user or all if admin"""
    conn = get_db()
    if current_user["role"] in ("admin", "superadmin"):
        rows = conn.execute("SELECT * FROM datasets WHERE is_active = 0 ORDER BY created_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM datasets WHERE is_active = 0 AND uploaded_by = ? ORDER BY created_at DESC", (current_user["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/api/datasets/{dataset_id}/demote")
@router.post("/api/datasets/{dataset_id}/unpublish")
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


@router.post("/api/datasets/{dataset_id}/publish")
def publish_dataset_to_public(
    dataset_id: int,
    price_credits: Optional[int] = Form(None),
    proposed_name: Optional[str] = Form(None),
    proposed_category: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user)
):
    """Publish a private dataset back to the public catalogue (Admin only)"""
    conn = get_db()
    ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not ds:
        conn.close()
        raise HTTPException(status_code=404, detail="Dataset not found.")

    if current_user["role"] not in ("admin", "superadmin"):
        conn.close()
        raise HTTPException(status_code=403, detail="Only administrators can publish datasets to the public catalogue.")

    final_name = proposed_name or ds["name"]
    final_cat = proposed_category or ds["category"]
    final_price = price_credits if price_credits is not None else (ds["price_credits"] if ds["price_credits"] and ds["price_credits"] > 0 else 10)

    conn.execute(
        "UPDATE datasets SET is_active = 1, promotion_status = 'approved', name = ?, category = ?, price_credits = ? WHERE id = ?",
        (final_name, final_cat, final_price, dataset_id)
    )
    if ds.get("source_job_id"):
        try:
            conn.execute("UPDATE scrape_jobs SET promotion_status = 'approved' WHERE id = ?", (ds["source_job_id"],))
        except Exception:
            pass
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Dataset '{final_name}' published to Public Catalogue!"}


@router.get("/api/datasets/{dataset_id}")
def get_dataset(dataset_id: str, page: int = 1, limit: int = 25, search: Optional[str] = None, authorization: Optional[str] = Header(None)):
    page_size = max(1, min(1000, limit))
    conn = get_db()
    is_storage_ok = _get_supabase_storage_configured()()
    
    # Check if dataset_id is a private scrape job
    if str(dataset_id).startswith("job_"):
        job_real_id = str(dataset_id).replace("job_", "")
        job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_real_id,)).fetchone()
        synced_ds = conn.execute("SELECT * FROM datasets WHERE source_job_id = ?", (job_real_id,)).fetchone()
        conn.close()

        file_path = None
        if job and job["result_path"]:
            file_path = job["result_path"]
        elif synced_ds and synced_ds["file_path"]:
            file_path = synced_ds["file_path"]

        # Resolve candidate paths across local results, uploads, and project folders
        if file_path and not os.path.exists(file_path):
            fn = os.path.basename(file_path.replace("\\", "/"))
            candidates = [
                os.path.join(SCRAPE_RESULTS_FOLDER, fn),
                os.path.join(UPLOAD_FOLDER, fn),
            ]
            found = False
            for c in candidates:
                if c and os.path.exists(c):
                    file_path = c
                    found = True
                    break
            if not found:
                file_path = None

        if not file_path and os.path.exists(SCRAPE_RESULTS_FOLDER):
            for fname in os.listdir(SCRAPE_RESULTS_FOLDER):
                if f"_{job_real_id}." in fname or f"job_{job_real_id}" in fname:
                    file_path = os.path.join(SCRAPE_RESULTS_FOLDER, fname)
                    break

        job_name = (job["query"] if job and "query" in job.keys() and job["query"] else (synced_ds["name"] if synced_ds and "name" in synced_ds.keys() and synced_ds["name"] else "Private Scraped Dataset"))
        job_div = (job["division"] if job and "division" in job.keys() else (synced_ds["division"] if synced_ds and "division" in synced_ds.keys() else "")) or ""
        job_dist = (job["district"] if job and "district" in job.keys() else (synced_ds["district"] if synced_ds and "district" in synced_ds.keys() else "")) or ""
        job_area = (job["area"] if job and "area" in job.keys() else (synced_ds["area"] if synced_ds and "area" in synced_ds.keys() else "")) or ""
        expected_rows = (job["result_count"] if job and "result_count" in job.keys() and job["result_count"] else (synced_ds["row_count"] if synced_ds and "row_count" in synced_ds.keys() and synced_ds["row_count"] else 0))

        if not file_path or not os.path.exists(file_path):
            return {
                "dataset": {
                    "id": f"job_{job_real_id}",
                    "name": job_name,
                    "category": "Private Scraped Dataset",
                    "division": job_div,
                    "district": job_dist,
                    "area": job_area,
                    "row_count": expected_rows,
                    "price_credits": 0
                },
                "unlocked": True,
                "leads": [],
                "total_rows": 0,
                "page": page,
                "current_page": page,
                "page_size": page_size,
                "pages_count": 1,
                "notice": "Private scrape job result file is not currently available on this server. If scraped locally, please ensure the local desktop automation engine is running."
            }

        try:
            df = pd.read_csv(file_path, dtype=str) if file_path.endswith(".csv") else pd.read_excel(file_path, dtype=str)
            df = clean_lead_df(df)
        except Exception:
            return {
                "dataset": {
                    "id": f"job_{job_real_id}",
                    "name": job_name,
                    "category": "Private Scraped Dataset",
                    "division": job_div,
                    "district": job_dist,
                    "area": job_area,
                    "row_count": 0,
                    "price_credits": 0
                },
                "unlocked": True,
                "leads": [],
                "total_rows": 0,
                "page": page,
                "current_page": page,
                "page_size": page_size,
                "pages_count": 1
            }

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
                "name": job_name,
                "category": "Private Scraped Dataset",
                "division": job_div,
                "district": job_dist,
                "area": job_area,
                "row_count": total_rows,
                "price_credits": 0
            },
            "unlocked": True,
            "leads": leads,
            "columns": [str(c) for c in df.columns],
            "total_rows": total_rows,
            "page": page,
            "current_page": page,
            "page_size": page_size,
            "pages_count": max(1, (total_rows + page_size - 1) // page_size)
        }

    # Decode user token if available
    current_user_id = None
    role = "user"
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        user_payload = decode_jwt_token(token)
        if user_payload:
            current_user_id = user_payload.get("user_id")
            role = user_payload.get("role", "user")

    # Standard public dataset lookup
    ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not ds:
        conn.close()
        raise HTTPException(status_code=404, detail="Dataset not found.")

    # Private dataset access control
    is_private = ds["is_active"] == 0
    is_owner = current_user_id and ds["uploaded_by"] == current_user_id
    is_admin = role in ("admin", "superadmin")
    if is_private and not is_owner and not is_admin:
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied. This is a private dataset.")
        
    file_path = resolve_dataset_file_path(ds["file_path"], ds["id"])
    if not file_path or not os.path.exists(file_path):
        conn.close()
        is_cloud_path = bool(ds["file_path"] and str(ds["file_path"]).startswith("supabase://"))
        if is_cloud_path or not is_storage_ok:
            return {
                "dataset": dict(ds),
                "unlocked": False,
                "leads": [],
                "columns": [c.strip() for c in (ds["column_names"] or "").split(",") if c.strip()],
                "total_rows": ds["row_count"] or 0,
                "page": page,
                "current_page": page,
                "page_size": page_size,
                "pages_count": 1,
                "notice": "Cloud dataset file is currently unavailable because cloud storage (Supabase Storage) is not configured or the file could not be retrieved from the bucket."
            }
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
    if is_admin or is_owner:
        unlocked = True
    elif current_user_id:
        log = conn.execute(
            "SELECT id FROM access_logs WHERE user_id = ? AND dataset_id = ? AND action = 'unlock'",
            (current_user_id, dataset_id)
        ).fetchone()
        if log:
            unlocked = True

    # Apply search filter
    if search:
        df = df[df.astype(str).apply(lambda x: x.str.contains(search, case=False)).any(axis=1)]

    total_rows = len(df)
    
    # Render Preview (unmasked if unlocked, masked with email watermark if locked)
    if not unlocked:
        preview_df = df.head(5).copy().fillna("")
        phone_cols = [c for c in preview_df.columns if "phone" in c.lower() or "mobile" in c.lower() or "contact" in c.lower()]
        for c in phone_cols:
            preview_df[c] = preview_df[c].apply(lambda p: (str(p)[:5] + "XXX" + str(p)[-3:]) if len(str(p)) >= 8 else str(p))
        raw_records = preview_df.to_dict(orient="records")
    else:
        start_row = (page - 1) * page_size
        end_row = start_row + page_size
        raw_records = df.iloc[start_row:end_row].fillna("").to_dict(orient="records")

    leads = [{k: ("" if (v is None or str(v).lower() in ("nan", "none", "null")) else str(v)) for k, v in r.items()} for r in raw_records]

    conn.close()
    return {
        "dataset": dict(ds),
        "unlocked": unlocked,
        "leads": leads,
        "columns": [str(c) for c in df.columns],
        "total_rows": total_rows,
        "page": page,
        "current_page": page,
        "page_size": page_size,
        "pages_count": max(1, (total_rows + page_size - 1) // page_size)
    }


@router.post("/api/datasets/{dataset_id}/unlock")
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


@router.get("/api/datasets/{dataset_id}/export")
def export_dataset(dataset_id: str, request: Request, format: Optional[str] = "excel", token: Optional[str] = None, authorization: Optional[str] = Header(None)):
    current_user = get_current_user(request=request, authorization=authorization, token=token)
    is_admin = current_user.get("role") in ("admin", "superadmin")

    conn = get_db()
    file_path = None
    title_name = "Exported_Dataset"

    if str(dataset_id).startswith("job_"):
        job_real_id = str(dataset_id).replace("job_", "")
        job = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_real_id,)).fetchone()
        synced_ds = conn.execute("SELECT * FROM datasets WHERE source_job_id = ?", (job_real_id,)).fetchone()
        if not job and not synced_ds:
            conn.close()
            raise HTTPException(status_code=404, detail="Private scrape job dataset not found.")

        job_user_id = job["user_id"] if job else (synced_ds["uploaded_by"] if synced_ds else None)
        if not is_admin and job_user_id != current_user["id"]:
            conn.close()
            raise HTTPException(status_code=403, detail="Permission denied. You can only export your own scrape jobs.")

        file_path = (job["result_path"] if job and job["result_path"] else (synced_ds["file_path"] if synced_ds else None))
        title_name = (job["query"] if job and job["query"] else (synced_ds["name"] if synced_ds else f"Job_{job_real_id}"))
        if file_path and not os.path.exists(file_path):
            fn = os.path.basename(file_path.replace("\\", "/"))
            for c in [
                os.path.join(SCRAPE_RESULTS_FOLDER, fn),
                os.path.join(UPLOAD_FOLDER, fn),
            ]:
                if os.path.exists(c):
                    file_path = c
                    break
        if not file_path and os.path.exists(SCRAPE_RESULTS_FOLDER):
            for fname in os.listdir(SCRAPE_RESULTS_FOLDER):
                if f"_{job_real_id}." in fname or f"job_{job_real_id}" in fname:
                    file_path = os.path.join(SCRAPE_RESULTS_FOLDER, fname)
                    break
    else:
        ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        if not ds:
            conn.close()
            raise HTTPException(status_code=404, detail="Dataset not found.")

        is_owner = ds["uploaded_by"] == current_user["id"]
        has_unlocked = False
        if not is_admin and not is_owner:
            log = conn.execute(
                "SELECT id FROM access_logs WHERE user_id = ? AND dataset_id = ? AND action = 'unlock'",
                (current_user["id"], dataset_id)
            ).fetchone()
            if log:
                has_unlocked = True

        if not is_admin and not is_owner and not has_unlocked:
            conn.close()
            raise HTTPException(
                status_code=403,
                detail="Permission denied. You must unlock this dataset before downloading."
            )

        file_path = resolve_dataset_file_path(ds["file_path"], int(dataset_id))
        title_name = ds["name"]

    conn.close()

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Data file missing.")

    return generate_export_response(file_path, export_format=format, title=title_name)


@router.post("/api/datasets/inspect-file")
def inspect_file_endpoint(
    file: UploadFile = File(...),
    sheet_name: Optional[str] = Form(None),
    strip_zero: bool = Form(True),
    trim_spaces: bool = Form(True),
    drop_empty_rows: bool = Form(True),
    normalize_headers: bool = Form(False),
    current_user: dict = Depends(get_current_user)
):
    """
    Inspects an uploaded Excel or CSV file without saving.
    Returns sheet names, row/col counts, and raw vs formatted preview records
    so user can interactively toggle checkboxes and verify formatting.
    """
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in ("xlsx", "xls", "csv"):
        raise HTTPException(status_code=400, detail="Only .xlsx, .xls, and .csv files are supported.")

    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        preview = inspect_excel_file(
            file_bytes=file_bytes,
            filename=file.filename or "dataset.xlsx",
            sheet_name=sheet_name,
            limit=8,
            strip_zero=strip_zero,
            trim_spaces=trim_spaces,
            drop_empty_rows=drop_empty_rows,
            normalize_headers=normalize_headers,
        )
        return {"success": True, **preview}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to inspect Excel file: {str(e)}")


@router.post("/api/datasets/upload-private")
def upload_private_dataset(
    file: UploadFile = File(...),
    name: str = Form(...),
    category: Optional[str] = Form("Private Custom Dataset"),
    division: Optional[str] = Form(None),
    district: Optional[str] = Form(None),
    area: Optional[str] = Form(None),
    sheet_name: Optional[str] = Form(None),
    strip_zero: bool = Form(True),
    trim_spaces: bool = Form(True),
    drop_empty_rows: bool = Form(True),
    normalize_headers: bool = Form(False),
    sync_now: bool = Form(False),
    current_user: dict = Depends(get_current_user)
):
    """
    Uploads any Excel (.xlsx, .xls) or CSV file directly to the user's Private Data Catalogue.
    The user can selectively apply formatting options (strip .0, trim whitespace, drop empty rows).
    Kept in local DB by default; Pro and Enterprise users (or admins) can sync to cloud.
    """
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in ("xlsx", "xls", "csv"):
        raise HTTPException(status_code=400, detail="Only .xlsx, .xls, and .csv files are supported.")

    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        raw_df, sheet_names = read_raw_df_from_bytes(file_bytes, file.filename or "dataset.xlsx", sheet_name)
        cleaned_df = format_and_clean_df(
            raw_df,
            strip_zero=strip_zero,
            trim_spaces=trim_spaces,
            drop_empty_rows=drop_empty_rows,
            normalize_headers=normalize_headers,
        )
        total_rows = len(cleaned_df)
        column_names_str = ", ".join([str(c) for c in cleaned_df.columns])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error parsing file: {str(e)}")

    clean_bytes = df_to_excel_bytes(cleaned_df)
    safe_name = "".join(c for c in (file.filename or "data.xlsx") if c.isalnum() or c in "._- ")
    filename = f"private_{current_user['id']}_{int(time.time())}_{safe_name}"
    if not filename.endswith(".xlsx"):
        filename = f"{filename}.xlsx"

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    local_disk_path = os.path.join(UPLOAD_FOLDER, filename)
    with open(local_disk_path, "wb") as f:
        f.write(clean_bytes)
    stored_file_path = f"uploads/{filename}"

    conn = get_db()
    is_synced = 0

    if sync_now:
        plan_tier = get_user_plan_tier(conn, current_user["id"], current_user["email"], current_user["role"])
        u_row = conn.execute("SELECT allow_sync, max_sync_files FROM users WHERE id = ?", (current_user["id"],)).fetchone()
        current_user_copy = dict(current_user)
        if u_row and "allow_sync" in u_row.keys():
            current_user_copy["allow_sync"] = u_row["allow_sync"]

        if not user_can_sync_to_cloud(current_user_copy, plan_tier):
            conn.close()
            raise HTTPException(
                status_code=403,
                detail="Cloud dataset synchronization is an exclusive feature for Pro Growth Pack and Enterprise Mega Pack users, or requires administrator authorization."
            )

        max_sync = u_row["max_sync_files"] if u_row and "max_sync_files" in u_row.keys() and u_row["max_sync_files"] is not None else 5
        if max_sync > 0:
            cnt_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM datasets WHERE uploaded_by = ? AND (is_synced = 1 OR file_path LIKE ?)",
                (current_user["id"], "supabase://%")
            ).fetchone()
            active_cnt = cnt_row["cnt"] if cnt_row else 0
            if active_cnt >= max_sync:
                conn.close()
                raise HTTPException(
                    status_code=403,
                    detail=f"Cloud upload limit reached ({max_sync} synced datasets max). Desync unused datasets or contact the administrator."
                )

        is_storage_ok = _get_supabase_storage_configured()()
        if is_storage_ok:
            uploader = _get_supabase_upload()
            ok, cloud_uri = uploader(clean_bytes, f"synced/{current_user['id']}/{filename}")
            if ok:
                stored_file_path = cloud_uri
                is_synced = 1
        elif os.getenv("TESTING") == "1":
            is_synced = 1

    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO datasets 
           (name, category, division, district, area, file_path, row_count, column_names, price_credits, is_active, uploaded_by, is_synced, source_job_id, promotion_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, NULL, 'none')""",
        (name, category, division, district, area, stored_file_path, total_rows, column_names_str, current_user["id"], is_synced)
    )
    ds_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return {
        "success": True,
        "dataset_id": ds_id,
        "name": name,
        "row_count": total_rows,
        "is_synced": is_synced,
        "message": f'Dataset "{name}" successfully added to your Private Catalogue!'
    }


@router.post("/api/admin/datasets/upload")
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
    file_bytes = file.file.read()
    
    # Read file row counts and columns directly from in-memory stream
    try:
        buffer = io.BytesIO(file_bytes)
        if ext == "csv":
            df = pd.read_csv(buffer)
        else:
            df = pd.read_excel(buffer)
        row_count = len(df)
        column_names = ", ".join(df.columns)
    except Exception:
        raise HTTPException(status_code=500, detail="Error parsing file headers.")

    # Supabase Cloud Storage verification
    is_storage_ok = _get_supabase_storage_configured()()
    is_testing = os.getenv("TESTING") == "1"

    if not is_storage_ok and not is_testing:
        raise HTTPException(
            status_code=503,
            detail="Public dataset hosting requires cloud storage (Supabase Storage). Currently public dataset publication is not configured on this server."
        )

    if is_storage_ok:
        uploader = _get_supabase_upload()
        success, cloud_uri = uploader(file_bytes, f"admin/{filename}")
        if not success:
            raise HTTPException(status_code=502, detail=f"Failed to upload dataset to Supabase Storage: {cloud_uri}")
        stored_file_path = cloud_uri
    else:
        # Local fallback only when cloud storage is unconfigured during offline testing
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(file_path, "wb") as f:
            f.write(file_bytes)
        stored_file_path = file_path

    conn = get_db()
    conn.execute(
        """INSERT INTO datasets (name, category, division, district, area, file_path, row_count, column_names, price_credits, uploaded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, category, division, district, area, stored_file_path, row_count, column_names, price_credits, admin_user["id"])
    )
    conn.commit()
    conn.close()
    return {"success": True, "message": "Dataset catalog file uploaded successfully to cloud storage."}


@router.delete("/api/admin/datasets/{dataset_id}")
def admin_delete_dataset(dataset_id: str, admin_user: dict = Depends(get_admin_user)):
    conn = get_db()
    conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
    conn.commit()
    conn.close()
    return {"success": True}


@router.delete("/api/datasets/{dataset_id}")
def user_delete_dataset(dataset_id: str, current_user: dict = Depends(get_current_user)):
    conn = get_db()
    clean_id = str(dataset_id).replace("job_", "")
    ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (clean_id,)).fetchone()
    if not ds:
        conn.close()
        raise HTTPException(status_code=404, detail="Dataset not found.")
    is_admin = current_user.get("role") in ("admin", "superadmin")
    if not is_admin and ds["uploaded_by"] != current_user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Permission denied. You can only delete your own datasets.")
    conn.execute("DELETE FROM datasets WHERE id = ?", (clean_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Dataset deleted successfully."}


@router.post("/api/datasets/sync")
def sync_dataset_to_cloud(
    file: Optional[UploadFile] = File(None),
    dataset_id: Optional[int] = Form(None),
    name: Optional[str] = Form(None),
    category: Optional[str] = Form("Private Scraped Leads"),
    division: Optional[str] = Form(None),
    district: Optional[str] = Form(None),
    area: Optional[str] = Form(None),
    row_count: Optional[int] = Form(0),
    source_job_id: Optional[int] = Form(None),
    current_user: dict = Depends(get_current_user)
):
    """
    Sync private dataset from local computer to PostgreSQL cloud.
    Supports both scrape jobs (via source_job_id) and custom uploaded datasets (via dataset_id).
    Available for Pro Growth Pack, Enterprise Mega Pack, Admins, or customers granted custom sync permission.
    """
    conn = get_db()
    plan_tier = get_user_plan_tier(conn, current_user["id"], current_user["email"], current_user["role"])
    
    u_row = conn.execute("SELECT allow_sync FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    current_user_copy = dict(current_user)
    if u_row and "allow_sync" in u_row.keys():
        current_user_copy["allow_sync"] = u_row["allow_sync"]

    if not user_can_sync_to_cloud(current_user_copy, plan_tier):
        conn.close()
        raise HTTPException(
            status_code=403,
            detail="Cloud dataset synchronization is an exclusive feature for Pro Growth Pack and Enterprise Mega Pack users, or requires administrator authorization."
        )

    # Check per-user cloud upload limit
    user_row = conn.execute("SELECT max_sync_files FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    max_sync = user_row["max_sync_files"] if user_row and "max_sync_files" in user_row.keys() and user_row["max_sync_files"] is not None else 5
    
    existing = None
    if dataset_id:
        existing = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        if not existing:
            conn.close()
            raise HTTPException(status_code=404, detail="Dataset not found.")
        if current_user["role"] not in ("admin", "superadmin") and existing["uploaded_by"] != current_user["id"]:
            conn.close()
            raise HTTPException(status_code=403, detail="Permission denied. You can only sync your own datasets.")
    elif source_job_id:
        existing = conn.execute(
            "SELECT * FROM datasets WHERE uploaded_by = ? AND source_job_id = ?",
            (current_user["id"], source_job_id)
        ).fetchone()

    is_update = bool(existing and existing["is_synced"] == 1)

    if max_sync > 0:
        count_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM datasets WHERE uploaded_by = ? AND (is_synced = 1 OR file_path LIKE ?)",
            (current_user["id"], "supabase://%")
        ).fetchone()
        active_synced_count = count_row["cnt"] if count_row else 0

        if not is_update and active_synced_count >= max_sync:
            conn.close()
            raise HTTPException(
                status_code=403,
                detail=f"Cloud upload limit reached. Your account is allowed a maximum of {max_sync} synced cloud datasets. Please desync unused datasets or contact the administrator to increase your limit."
            )

    # Verify Supabase Cloud Storage
    is_storage_ok = _get_supabase_storage_configured()()
    is_testing = os.getenv("TESTING") == "1"

    if not is_storage_ok and not is_testing:
        conn.close()
        raise HTTPException(
            status_code=503,
            detail="Cloud dataset storage (Supabase Storage) is currently not configured on this server. Cloud synchronization is unavailable."
        )

    file_rel_path = None
    file_name_resolved = name or (existing["name"] if existing else "Private Dataset")

    if file:
        file_ext = os.path.splitext(file.filename or "")[1] or ".xlsx"
        filename = f"synced_{current_user['id']}_{int(time.time())}{file_ext}"
        file_bytes = file.file.read()

        if is_storage_ok:
            uploader = _get_supabase_upload()
            ok, cloud_uri = uploader(file_bytes, f"synced/{current_user['id']}/{filename}")
            if ok:
                file_rel_path = cloud_uri
            else:
                conn.close()
                raise HTTPException(status_code=502, detail=f"Failed to upload to Supabase Storage: {cloud_uri}")
        else:
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            file_disk_path = os.path.join(UPLOAD_FOLDER, filename)
            with open(file_disk_path, "wb") as f:
                f.write(file_bytes)
            file_rel_path = f"uploads/{filename}"
    elif existing and existing["file_path"]:
        local_src = resolve_dataset_file_path(existing["file_path"], existing["id"])
        if local_src and os.path.exists(local_src):
            with open(local_src, "rb") as f:
                file_bytes = f.read()
            filename = f"synced_{current_user['id']}_{int(time.time())}.xlsx"
            if is_storage_ok:
                uploader = _get_supabase_upload()
                ok, cloud_uri = uploader(file_bytes, f"synced/{current_user['id']}/{filename}")
                if ok:
                    file_rel_path = cloud_uri
            if not file_rel_path:
                file_rel_path = local_src
    elif source_job_id:
        src_job = conn.execute("SELECT result_path FROM scrape_jobs WHERE id = ?", (source_job_id,)).fetchone()
        local_src = src_job["result_path"] if src_job and "result_path" in src_job.keys() and src_job["result_path"] else None
        if local_src and not os.path.exists(local_src):
            fn = os.path.basename(local_src.replace("\\", "/"))
            for c in [
                os.path.join(SCRAPE_RESULTS_FOLDER, fn),
                os.path.join(UPLOAD_FOLDER, fn),
            ]:
                if c and os.path.exists(c):
                    local_src = c
                    break
        if not local_src and os.path.exists(SCRAPE_RESULTS_FOLDER):
            for fname in os.listdir(SCRAPE_RESULTS_FOLDER):
                if f"_{source_job_id}." in fname or f"job_{source_job_id}" in fname:
                    local_src = os.path.join(SCRAPE_RESULTS_FOLDER, fname)
                    break

        if local_src and os.path.exists(local_src):
            with open(local_src, "rb") as f:
                file_bytes = f.read()
            filename = f"synced_{current_user['id']}_{int(time.time())}.xlsx"
            if is_storage_ok:
                uploader = _get_supabase_upload()
                ok, cloud_uri = uploader(file_bytes, f"synced/{current_user['id']}/{filename}")
                if ok:
                    file_rel_path = cloud_uri
            if not file_rel_path:
                file_rel_path = local_src

    if not file_rel_path:
        if is_testing:
            file_rel_path = f"uploads/synced_placeholder_{current_user['id']}_{int(time.time())}.xlsx"
        else:
            conn.close()
            raise HTTPException(
                status_code=400,
                detail="A valid dataset file (.xlsx or .csv) is required to sync to cloud storage."
            )

    if existing:
        conn.execute(
            """UPDATE datasets 
               SET name = COALESCE(?, name), category = COALESCE(?, category), division = COALESCE(?, division),
                   district = COALESCE(?, district), area = COALESCE(?, area), 
                   file_path = ?, row_count = COALESCE(?, row_count), is_synced = 1
               WHERE id = ?""",
            (name, category, division, district, area, file_rel_path, row_count if row_count else None, existing["id"])
        )
        ds_id = existing["id"]
        file_name_resolved = name or existing["name"]
    else:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO datasets 
               (name, category, division, district, area, file_path, row_count, column_names, price_credits, is_active, uploaded_by, is_synced, source_job_id, promotion_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'Name, Phone, Address, Website, Rating, Category, Maps URL, Query', 0, 0, ?, 1, ?, 'none')""",
            (file_name_resolved, category or "Private Scraped Leads", division, district, area, file_rel_path, row_count or 0, current_user["id"], source_job_id)
        )
        ds_id = cursor.lastrowid

    if source_job_id:
        try:
            conn.execute("UPDATE scrape_jobs SET is_synced = 1 WHERE id = ?", (source_job_id,))
        except Exception:
            pass

    conn.commit()
    conn.close()
    return {
        "success": True,
        "dataset_id": ds_id,
        "message": f'Dataset "{file_name_resolved}" successfully synced to PostgreSQL cloud!'
    }


@router.post("/api/datasets/{dataset_id}/desync")
def desync_dataset(dataset_id: str, current_user: dict = Depends(get_current_user)):
    """
    Desyncs a dataset from the cloud:
    1. Removes the dataset file from Supabase Storage bucket.
    2. Resets source scrape_jobs.is_synced = 0 (if tied to a scrape job).
    3. If running on local SQLite, retains the local dataset with is_synced = 0.
       If running strictly on cloud PostgreSQL, removes the cloud entry to free quota.
    4. PRESERVES local files on disk intact.
    """
    conn = get_db()
    is_admin = current_user.get("role") in ("admin", "superadmin")
    
    clean_id = str(dataset_id).replace("job_", "")
    ds = conn.execute("SELECT * FROM datasets WHERE id = ? OR source_job_id = ?", (clean_id, clean_id)).fetchone()
    if not ds:
        conn.close()
        raise HTTPException(status_code=404, detail="Dataset not found in cloud database.")

    if not is_admin and ds["uploaded_by"] != current_user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Permission denied. You can only desync your own datasets.")

    file_path = ds["file_path"]
    source_job_id = ds["source_job_id"]
    ds_real_id = ds["id"]

    # 1. If stored in Supabase Storage, delete the file from the cloud bucket
    if file_path and str(file_path).startswith("supabase://"):
        deleter = _get_supabase_delete()
        try:
            deleter(file_path)
        except Exception as e:
            print(f"[DESYNC NOTICE] Error deleting from Supabase Storage: {e}")

    # 2. If tied to a scrape job, update scrape_jobs.is_synced = 0
    if source_job_id:
        try:
            conn.execute("UPDATE scrape_jobs SET is_synced = 0 WHERE id = ?", (source_job_id,))
        except Exception:
            pass

    # 3. For uploaded datasets in local SQLite, retain them locally with is_synced = 0
    if not source_job_id and is_sqlite_active():
        conn.execute("UPDATE datasets SET is_synced = 0 WHERE id = ?", (ds_real_id,))
    else:
        conn.execute("DELETE FROM datasets WHERE id = ?", (ds_real_id,))

    conn.commit()
    conn.close()

    return {
        "success": True,
        "dataset_id": ds_real_id,
        "message": f'Dataset "{ds["name"]}" has been desynced from the cloud. Cloud bucket storage has been freed; your local file remains intact.'
    }


@router.post("/api/datasets/promote-request")
def submit_dataset_promotion_request(
    file: Optional[UploadFile] = File(None),
    dataset_id: Optional[int] = Form(None),
    source_job_id: Optional[int] = Form(None),
    proposed_name: str = Form(...),
    proposed_category: Optional[str] = Form("General Business"),
    division: Optional[str] = Form(None),
    district: Optional[str] = Form(None),
    area: Optional[str] = Form(None),
    row_count: Optional[int] = Form(0),
    current_user: dict = Depends(get_current_user)
):
    """
    Submits a promotion request to move a private dataset into the PostgreSQL public catalog.
    Admin approval publishes it to public catalog; rejection removes it from PostgreSQL.
    """
    conn = get_db()
    is_admin = current_user.get("role") in ("admin", "superadmin")

    # Verify Supabase Cloud Storage
    is_storage_ok = _get_supabase_storage_configured()()
    is_testing = os.getenv("TESTING") == "1"

    if not is_storage_ok and not is_testing:
        conn.close()
        raise HTTPException(
            status_code=503,
            detail="Publishing to the public catalogue requires cloud storage (Supabase Storage). Currently public dataset hosting is not configured on this server."
        )

    file_rel_path = None
    if file:
        file_ext = os.path.splitext(file.filename or "")[1] or ".xlsx"
        filename = f"promote_{current_user['id']}_{int(time.time())}{file_ext}"
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        disk_path = os.path.join(UPLOAD_FOLDER, filename)
        file_bytes = file.file.read()
        with open(disk_path, "wb") as f:
            f.write(file_bytes)
        if is_storage_ok:
            uploader = _get_supabase_upload()
            ok, cloud_uri = uploader(file_bytes, f"promoted/{current_user['id']}/{filename}")
            if ok:
                file_rel_path = cloud_uri
            else:
                conn.close()
                raise HTTPException(status_code=502, detail=f"Failed to upload dataset to Supabase Storage: {cloud_uri}")
        else:
            file_rel_path = f"uploads/{filename}"

    ds = None
    if dataset_id:
        ds = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()

    if not ds and source_job_id:
        ds = conn.execute(
            "SELECT * FROM datasets WHERE uploaded_by = ? AND source_job_id = ?",
            (current_user["id"], source_job_id)
        ).fetchone()

    if ds:
        target_id = ds["id"]
        if is_admin:
            conn.execute(
                """UPDATE datasets 
                   SET name = ?, category = ?, is_active = 1, promotion_status = 'approved',
                       proposed_name = ?, proposed_category = ?
                   WHERE id = ?""",
                (proposed_name, proposed_category, proposed_name, proposed_category, target_id)
            )
            msg = f'Dataset "{proposed_name}" published directly to Public Catalog in PostgreSQL!'
        else:
            conn.execute(
                """UPDATE datasets 
                   SET promotion_status = 'pending', proposed_name = ?, proposed_category = ?
                   WHERE id = ?""",
                (proposed_name, proposed_category, target_id)
            )
            msg = "Promotion request submitted successfully! An administrator will review and publish it."
    else:
        if not file_rel_path:
            file_rel_path = f"uploads/promoted_{current_user['id']}_{int(time.time())}.xlsx"
        
        cursor = conn.cursor()
        is_active = 1 if is_admin else 0
        p_status = "approved" if is_admin else "pending"
        cursor.execute(
            """INSERT INTO datasets 
               (name, category, division, district, area, file_path, row_count, column_names, price_credits, is_active, uploaded_by, promotion_status, proposed_name, proposed_category, source_job_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'Name, Phone, Address, Website, Rating, Category, Maps URL, Query', 10, ?, ?, ?, ?, ?, ?)""",
            (proposed_name, proposed_category, division, district, area, file_rel_path, row_count or 0, is_active, current_user["id"], p_status, proposed_name, proposed_category, source_job_id)
        )
        target_id = cursor.lastrowid
        msg = f'Dataset "{proposed_name}" published directly to Public Catalog!' if is_admin else "Promotion request submitted successfully! An administrator will review and publish it."

    if source_job_id:
        try:
            conn.execute("UPDATE scrape_jobs SET promotion_status = ? WHERE id = ?", ("approved" if is_admin else "pending", source_job_id))
        except Exception:
            pass

    conn.commit()
    conn.close()
    return {"success": True, "dataset_id": target_id, "message": msg}
