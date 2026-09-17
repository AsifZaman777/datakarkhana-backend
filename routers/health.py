import os
import time
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse, FileResponse

from database import get_db, is_sqlite_active
from core.constants import SERVER_START_TIME

router = APIRouter(tags=["Health & System"])

@router.get("/health")
@router.get("/api/health")
def health_check():
    """Lightweight health check endpoint for uptime monitors, desktop dots, and Render keep-alive pings"""
    db_status = "ok"
    try:
        conn = get_db()
        conn.execute("SELECT 1;").fetchone()
        conn.close()
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"

    uptime_sec = int(time.time() - SERVER_START_TIME)
    uptime_str = f"{uptime_sec // 3600}h {(uptime_sec % 3600) // 60}m {uptime_sec % 60}s"
    db_mode = "sqlite" if is_sqlite_active() else "postgres"
    return {
        "status": "healthy" if "unhealthy" not in db_status else "degraded",
        "service": "MarketingOstad Backend",
        "database": "ok" if "unhealthy" not in db_status else db_status,
        "mode": db_mode,
        "uptime": uptime_str,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@router.get("/api/download/desktop")
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
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
