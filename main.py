import os
import time
import threading
from datetime import datetime
from typing import Optional

import requests
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import (
    FRONTEND_URL,
    FRONTEND_LOCAL_URL,
    FRONTEND_RENDER_URL,
)
from database import get_db, init_db, _request_db_conns
from core.constants import (
    UPLOAD_FOLDER,
    SCRAPE_RESULTS_FOLDER,
    SCRAPER_SCREENSHOTS_FOLDER,
    LOGS_FOLDER,
    SERVER_START_TIME,
)
from supabase_storage import (
    is_supabase_storage_configured,
    upload_dataset_file,
    download_dataset_file,
    delete_dataset_file,
    get_signed_url,
)
from routers import (
    health_router,
    auth_router,
    datasets_router,
    scraper_router,
    payments_router,
    licenses_router,
    marketing_router,
    admin_router,
    config_router,
)

# Initialize FastAPI application
app = FastAPI(title="MarketingOstad API Service")

# ── Middlewares ──────────────────────────────────────────

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


@app.middleware("http")
async def private_network_access_middleware(request: Request, call_next):
    """Respond to Chromium Private Network Access (PNA) preflight checks"""
    if request.method == "OPTIONS" and (
        request.headers.get("access-control-request-private-network")
        or request.headers.get("access-control-request-method")
    ):
        origin = request.headers.get("origin") or "*"
        response = Response(status_code=204)
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


# CORS settings
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
    allow_origin_regex=r"^https?://.*",
    allow_credentials=True,
    allow_headers=["*"],
    allow_methods=["*"],
    allow_private_network=True,
)

# Mount static files
app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")

# Include all modular routers
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(datasets_router)
app.include_router(scraper_router)
app.include_router(payments_router)
app.include_router(licenses_router)
app.include_router(marketing_router)
app.include_router(admin_router)
app.include_router(config_router)

# ── Background keep-alive & Startup ───────────────────────

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
            time.sleep(720)
            res = requests.get(ping_url, timeout=15)
            print(f"[KEEP-ALIVE] Ping {res.status_code} to {ping_url} at {datetime.now().strftime('%H:%M:%S')}")
        except Exception as err:
            print(f"[KEEP-ALIVE] Ping notice: {err}")


@app.on_event("startup")
def startup_event():
    t = threading.Thread(target=_keep_alive_loop, daemon=True)
    t.start()

    try:
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


# ── Re-exports for backward compatibility & testing ───────
from core.security import (
    hash_password,
    verify_password,
    create_jwt_token,
    decode_jwt_token,
    resolve_frontend_base_url,
)
from core.dependencies import (
    get_current_user,
    get_admin_user,
    get_optional_current_user,
    check_desktop_license,
    get_user_plan_tier,
    user_can_sync_to_cloud,
)
from schemas import *
from services import *
from license_service import (
    generate_production_license,
    verify_license,
    activate_license,
    get_current_license_status,
)
from scraper import (
    setup_driver,
    scrape_query,
    save_to_excel,
    get_live_frame,
    clear_scraper_frame,
    LIVE_FRAMES,
    is_job_stopped,
    stop_scraper_job,
    clear_job_stop,
    subscribe_scraper_job,
    unsubscribe_scraper_job,
    publish_scraper_event,
    add_job_log,
    get_job_recent_logs,
)
from senders import run_whatsapp_campaign, write_log_to_file, SCREENSHOTS_FOLDER as CAMPAIGN_SCREENSHOTS
from config import REGIONS, CATEGORIES


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
