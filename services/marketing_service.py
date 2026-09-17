import os
from datetime import datetime, timedelta
from typing import Optional

from database import get_db
from core.constants import UPLOAD_FOLDER, SCRAPE_RESULTS_FOLDER
from services.dataset_service import resolve_dataset_file_path

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
            group_name = jb["query"] or "Scraped Leads"
    else:
        ds = conn.execute("SELECT * FROM datasets WHERE name = ? OR id = ?", (recipient_group, recipient_group)).fetchone()
        if ds:
            file_path = resolve_dataset_file_path(ds["file_path"], ds["id"])
            group_name = ds["name"]
        else:
            jb = conn.execute("SELECT * FROM scrape_jobs WHERE query = ? OR id = ?", (recipient_group, recipient_group)).fetchone()
            if jb:
                file_path = jb["result_path"]
                group_name = jb["query"] or "Scraped Leads"
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


def compute_campaign_eta(campaign_type: str, sent_count: int, total_count: int, start_row: int = 0, created_at: Optional[str] = None):
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
