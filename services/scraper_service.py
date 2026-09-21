import os
import time
import gc
from typing import Optional

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
from scrapers.daraz_scraper import DarazScraper, save_daraz_to_excel

def run_background_scrape(job_id, queries, division, district, area, headless=False):
    def log_cb(msg):
        add_job_log(job_id, msg)
        try:
            db = get_db()
            db.execute("INSERT INTO scrape_logs (job_id, message) VALUES (?, ?)", (job_id, msg))
            db.commit()
            db.close()
        except Exception:
            pass

    driver = None
    all_results = []
    stopped_early = False

    try:
        log_cb("🚀 Initializing automated browser engine...")
        driver = setup_driver(headless=headless, log_cb=log_cb)
        log_cb("🌐 Browser session active. Ready for Google Maps scraping.")
        for idx, q in enumerate(queries):
            if is_job_stopped(job_id):
                stopped_early = True
                log_cb("⏹️ Manual stop requested. Exiting scraper loop...")
                break

            log_cb(f"─── [Query {idx+1}/{len(queries)}] Searching Google Maps: '{q}' ───")
            res = scrape_query(driver, q, log_cb, job_id=job_id)
            if res:
                all_results.extend(res)
            log_cb(f"─── [Query {idx+1}/{len(queries)}] Completed: Collected {len(res) if res else 0} items ───")

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

    publish_scraper_event(job_id, {
        "type": "job_ended",
        "status": final_status,
        "result_count": len(all_results)
    })
    clear_job_stop(job_id)


def run_background_daraz_scrape(
    job_id: int,
    query: str,
    pages: int = 1,
    max_items: Optional[int] = None,
    headless: bool = True
):
    """
    Run automated Daraz background scraper with live screenshot debugging,
    WebSocket progress streaming, and private catalogue Excel output.
    """
    def log_cb(msg):
        add_job_log(job_id, msg)
        try:
            db = get_db()
            db.execute("INSERT INTO scrape_logs (job_id, message) VALUES (?, ?)", (job_id, msg))
            db.commit()
            db.close()
        except Exception:
            pass

    scraper = DarazScraper(job_id=job_id, log_cb=log_cb, enable_frames=True)
    all_results = []
    stopped_early = False

    try:
        log_cb(f"🚀 Initializing Daraz E-Commerce Intelligence Engine (Query: '{query}', Max Pages: {pages})...")
        all_results = scraper.run(
            query=query,
            max_pages=pages,
            max_items=max_items,
            headless=headless
        )
        if is_job_stopped(job_id):
            stopped_early = True
    except Exception as ex:
        log_cb(f"⚠️ Daraz Scraper encountered error: {ex}")
    finally:
        clear_scraper_frame(job_id)

    # Save results to Excel even if interrupted early
    if all_results:
        filename = f"job_daraz_{job_id}_{int(time.time())}.xlsx"
        result_path = os.path.join(SCRAPE_RESULTS_FOLDER, filename)
        saved_count = save_daraz_to_excel(all_results, result_path)
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

        status_msg = "stopped manually" if stopped_early else f"completed scanning {pages} pages"
        log_cb(f"🎉 Daraz Engine {status_msg}! Preserved {saved_count} products into your private catalogue.")
    else:
        final_status = "stopped" if stopped_early else "failed"
        db = get_db()
        db.execute(
            "UPDATE scrape_jobs SET status = ?, error_message = 'No products parsed from Daraz before process ended.' WHERE id = ?",
            (final_status, job_id)
        )
        db.commit()
        db.close()
        log_cb("Daraz Scraper execution finished: 0 products collected.")

    publish_scraper_event(job_id, {
        "type": "job_ended",
        "status": final_status,
        "result_count": len(all_results)
    })
    clear_job_stop(job_id)
