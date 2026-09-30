import os
import time


def _resolve_data_dir() -> str:
    r"""
    Returns a writable directory for all user-generated data (uploads, logs,
    scrape results, WhatsApp sessions, license files).

    Priority:
      1. DATAKARKHANA_DATA_DIR env var -- set by Electron main.js via app.getPath("userData")
      2. Backend directory itself      -- writable in development (not inside Program Files)
      3. %APPDATA%\datakarkhana        -- Windows user data fallback (always writable)
      4. ~/.datakarkhana               -- macOS / Linux fallback
    """
    # 1. Electron sets this to the user's Electron app data folder
    custom = os.getenv("DATAKARKHANA_DATA_DIR", "").strip()
    if custom:
        try:
            os.makedirs(custom, exist_ok=True)
            return custom
        except Exception:
            pass

    # 2. Development: backend directory is writable (not inside Program Files)
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        probe = os.path.join(backend_dir, ".write_probe")
        with open(probe, "w") as f:
            f.write("1")
        os.remove(probe)
        return backend_dir
    except Exception:
        pass

    # 3. Windows %APPDATA%\datakarkhana
    appdata = os.getenv("APPDATA", "")
    if appdata:
        try:
            d = os.path.join(appdata, "datakarkhana")
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            pass

    # 4. macOS / Linux ~/.datakarkhana
    home_d = os.path.expanduser("~/.datakarkhana")
    try:
        os.makedirs(home_d, exist_ok=True)
        return home_d
    except Exception:
        pass

    # Last resort: same directory as this file (may fail for read-only installs)
    return backend_dir


# Immutable source root (Python source tree -- may be read-only in packaged builds)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Writable data root (always safe to create files here)
DATA_DIR = _resolve_data_dir()

UPLOAD_FOLDER              = os.path.join(DATA_DIR, "uploads")
SCRAPE_RESULTS_FOLDER      = os.path.join(DATA_DIR, "scrape_results")
SCRAPER_SCREENSHOTS_FOLDER = os.path.join(SCRAPE_RESULTS_FOLDER, "screenshots")
LOGS_FOLDER                = os.path.join(DATA_DIR, "logs")

# Create directories at import time; swallow errors so the server can still
# start and show a useful error rather than crashing with PermissionError.
for _d in (UPLOAD_FOLDER, SCRAPE_RESULTS_FOLDER, SCRAPER_SCREENSHOTS_FOLDER, LOGS_FOLDER):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception as _e:
        print(f"[CORE WARNING] Could not create directory {_d!r}: {_e}")

SERVER_START_TIME = time.time()
