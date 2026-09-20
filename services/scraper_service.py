import os
import time
import gc

from database import get_db
from core.constants import SCRAPE_RESULTS_FOLDER
from scraper import (
    setup_driver,
    scrape_query,
    save_to_excel,
    clear_scraper_frame,
    is_job_stopped,
    clear_job_stop,
    publish_scraper_event,
    add_job_log,
)

from scrapers.registry import ScraperRegistry

def run_background_scrape(
    job_id,
    queries,
    division,
    district,
    area,
    headless=False,
    platform="google_maps",
    credentials=None,
    max_results=50
):
    def log_cb(msg):
        # 1. Stream log line instantly in real-time to active WebSocket subscribers
        add_job_log(job_id, msg)
        # 2. Persist immediately to database
        try:
            db = get_db()
            db.execute("INSERT INTO scrape_logs (job_id, message) VALUES (?, ?)", (job_id, msg))
            db.commit()
            db.close()
        except Exception:
            pass

    def flush_db_logs():
        pass

    driver = None
    all_results = []
    stopped_early = False

    try:
        log_cb("🚀 Initializing automated browser engine...")
        driver = setup_driver(headless=headless, log_cb=log_cb)

        # Initialize decoupled scraper from Registry
        scraper = ScraperRegistry.get_scraper(
            platform_id=platform,
            driver=driver,
            log_cb=log_cb,
            job_id=job_id
        )
        log_cb(f"🌐 [{scraper.platform_name} Engine] Active session ready.")

        # Authenticate if credentials supplied or required
        if credentials or scraper.requires_auth:
            cred_dict = credentials.model_dump() if hasattr(credentials, "model_dump") else (credentials if isinstance(credentials, dict) else {})
            if cred_dict and (cred_dict.get("username") or cred_dict.get("password")):
                log_cb(f"🔐 [{scraper.platform_name}] Running authentication...")
                scraper.authenticate(cred_dict)
            elif scraper.requires_auth:
                log_cb(f"ℹ️ [{scraper.platform_name}] No credentials provided. Running in guest exploration mode.")

        for idx, q in enumerate(queries):
            if is_job_stopped(job_id):
                stopped_early = True
                log_cb("⏹️ Manual stop requested. Exiting scraper loop...")
                break

            log_cb(f"─── [Query {idx+1}/{len(queries)}] Searching {scraper.platform_name}: '{q}' ───")
            res = scraper.search_and_extract(q, max_results=max_results or 50)
            if res:
                all_results.extend(res)
            log_cb(f"─── [Query {idx+1}/{len(queries)}] Completed: Collected {len(res) if res else 0} leads ───")

            if is_job_stopped(job_id):
                stopped_early = True
                log_cb("⏹️ Manual stop requested. Exiting scraper loop...")
                break

    except Exception as ex:
        log_cb(f"⚠️ Scraper thread encountered exception / interruption: {ex}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            gc.collect()
        clear_scraper_frame(job_id)

    # Save any results collected so far (even if stopped or interrupted!)
    if all_results:
        filename = f"job_{job_id}_{int(time.time())}.xlsx"
        result_path = os.path.join(SCRAPE_RESULTS_FOLDER, filename)
        saved_count = save_to_excel(all_results, result_path)
        if saved_count is None:
            saved_count = len(all_results)

        final_status = "stopped" if stopped_early else "done"
        db = get_db()
        db.execute(
            "UPDATE scrape_jobs SET status = ?, result_path = ?, result_count = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (final_status, result_path, saved_count, job_id)
        )
        db.commit()
        db.close()

        status_msg = "stopped manually" if stopped_early else f"completed all {len(queries)} queries"
        log_cb(f"🎉 Scraper {status_msg}! Preserved {saved_count} total business records into your private catalogue dataset.")
    else:
        final_status = "stopped" if stopped_early else "failed"
        db = get_db()
        db.execute("UPDATE scrape_jobs SET status = ?, error_message = 'No records parsed before process ended.' WHERE id = ?", (final_status, job_id))
        db.commit()
        db.close()
        log_cb("Scraper execution halted: 0 records collected.")

    flush_db_logs()
    publish_scraper_event(job_id, {
        "type": "job_ended",
        "status": final_status,
        "result_count": len(all_results)
    })
    clear_job_stop(job_id)
