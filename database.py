import os
import re
import time
import gc
import sqlite3
import contextvars
import psycopg2
import psycopg2.pool
import psycopg2.extras
from config import DATABASE_URL, SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD, SUPERADMIN_NAME
from auth import hash_password

TABLES_WITH_AUTO_ID = {
    "users", "datasets", "scrape_jobs", "access_logs", "credit_transactions",
    "scrape_logs", "campaign_logs", "dataset_requests", "security_violations",
    "payment_requests", "brevo_applications", "licenses"
}

_INSERT_TABLE_RE = re.compile(r"^\s*INSERT\s+INTO\s+([a-zA-Z0-9_]+)", re.IGNORECASE)
_INSERT_OR_REPLACE_RE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+([a-zA-Z0-9_]+)", re.IGNORECASE)
_INSERT_OR_IGNORE_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+([a-zA-Z0-9_]+)", re.IGNORECASE)

CONFLICT_KEYS = {
    "payment_settings": ("setting_key", "setting_value = EXCLUDED.setting_value"),
    "whatsapp_progress": ("recipient_group", "last_index = EXCLUDED.last_index, updated_at = CURRENT_TIMESTAMP"),
    "banned_ips": ("ip_address", None),  # None means DO NOTHING
}

def get_local_sqlite_path() -> str:
    """Returns a reliable, writable path for SQLite across all operating systems."""
    custom_dir = os.getenv("DATAKARKHANA_DATA_DIR")
    if custom_dir:
        try:
            os.makedirs(custom_dir, exist_ok=True)
            return os.path.join(custom_dir, "datakarkhana_local.db")
        except Exception:
            pass

    appdata = os.getenv("APPDATA")
    if appdata:
        try:
            target_dir = os.path.join(appdata, "datakarkhana")
            os.makedirs(target_dir, exist_ok=True)
            return os.path.join(target_dir, "datakarkhana_local.db")
        except Exception:
            pass

    home_dir = os.path.expanduser("~/.datakarkhana")
    try:
        os.makedirs(home_dir, exist_ok=True)
        return os.path.join(home_dir, "datakarkhana_local.db")
    except Exception:
        pass

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "datakarkhana_local.db")

def get_clean_database_url() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL") or os.getenv("POSTGRES_URL") or DATABASE_URL
    if not url:
        return ""
    url = url.strip().strip("'").strip('"')
    # Filter out template placeholders from .env.example or unconfigured environments
    invalid_markers = ["[YOUR-PASSWORD]", "[PROJECT-REF]", "your_password", "<password>", "example.com", "your_brevo"]
    if any(marker in url for marker in invalid_markers):
        return ""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # If using Supabase pooler, switch port 5432 to 6543 (transaction mode) to avoid EMAXCONNSESSION (pool_size limit)
    if "pooler.supabase.com:5432" in url:
        url = url.replace("pooler.supabase.com:5432", "pooler.supabase.com:6543")
    # Append sslmode=require for Supabase cloud connections if not already specified
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url

def is_sqlite_active() -> bool:
    url = get_clean_database_url()
    return not bool(url and not url.startswith("sqlite"))

def convert_placeholders(sql: str) -> str:
    """Replaces '?' with '%s' outside single/double quoted literals."""
    res = []
    in_single = False
    in_double = False
    escaped = False
    for ch in sql:
        if ch == '\\' and (in_single or in_double):
            escaped = not escaped
            res.append(ch)
            continue
        if ch == "'" and not in_double and not escaped:
            in_single = not in_single
        elif ch == '"' and not in_single and not escaped:
            in_double = not in_double
        elif ch == '?' and not in_single and not in_double:
            res.append('%s')
            escaped = False
            continue
        escaped = False
        res.append(ch)
    return ''.join(res)

def convert_placeholders_to_qmark(sql: str) -> str:
    """Replaces '%s' with '?' outside single/double quoted literals for SQLite."""
    res = []
    in_single = False
    in_double = False
    escaped = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == '\\' and (in_single or in_double):
            escaped = not escaped
            res.append(ch)
            i += 1
            continue
        if ch == "'" and not in_double and not escaped:
            in_single = not in_single
        elif ch == '"' and not in_single and not escaped:
            in_double = not in_double
        elif ch == '%' and i + 1 < n and sql[i+1] == 's' and not in_single and not in_double:
            res.append('?')
            escaped = False
            i += 2
            continue
        escaped = False
        res.append(ch)
        i += 1
    return ''.join(res)

def translate_query(query: str, allow_returning: bool = True):
    """
    Translates an SQLite-style query into standard PostgreSQL:
    1. Converts unquoted '?' placeholders to '%s'
    2. Converts SQLite 'INSERT OR REPLACE INTO' and 'INSERT OR IGNORE INTO' to PostgreSQL 'ON CONFLICT ...'
    3. Adds 'RETURNING id' for auto-increment tables so cursor.lastrowid works seamlessly.
    """
    sql = query.strip()

    # Convert INSERT OR REPLACE
    m_replace = _INSERT_OR_REPLACE_RE.match(sql)
    if m_replace:
        tbl = m_replace.group(1).lower()
        sql = re.sub(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", sql, flags=re.IGNORECASE)
        if tbl in CONFLICT_KEYS:
            key_col, update_clause = CONFLICT_KEYS[tbl]
            if update_clause:
                sql = f"{sql} ON CONFLICT ({key_col}) DO UPDATE SET {update_clause}"
            else:
                sql = f"{sql} ON CONFLICT ({key_col}) DO NOTHING"

    # Convert INSERT OR IGNORE
    m_ignore = _INSERT_OR_IGNORE_RE.match(sql)
    if m_ignore:
        tbl = m_ignore.group(1).lower()
        sql = re.sub(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.IGNORECASE)
        if tbl in CONFLICT_KEYS:
            key_col, _ = CONFLICT_KEYS[tbl]
            sql = f"{sql} ON CONFLICT ({key_col}) DO NOTHING"
        else:
            sql = f"{sql} ON CONFLICT DO NOTHING"

    # Check for RETURNING id on auto-id tables
    has_returning = False
    m_insert = _INSERT_TABLE_RE.match(sql)
    if allow_returning and m_insert:
        tbl = m_insert.group(1).lower()
        if tbl in TABLES_WITH_AUTO_ID and "returning" not in sql.lower():
            sql = f"{sql.rstrip(';')} RETURNING id"
            has_returning = True

    # Convert placeholders ? -> %s
    sql = convert_placeholders(sql)
    return sql, has_returning

class PostgresCursorWrapper:
    def __init__(self, raw_cursor):
        self._cursor = raw_cursor
        self.lastrowid = None

    def execute(self, query, params=None):
        sql, has_returning_id = translate_query(query)
        clean_params = None
        if params is not None:
            if isinstance(params, (list, tuple)):
                clean_params = tuple(int(p) if isinstance(p, bool) else p for p in params)
            elif isinstance(params, dict):
                clean_params = {k: int(v) if isinstance(v, bool) else v for k, v in params.items()}
            else:
                clean_params = (int(params) if isinstance(params, bool) else params,)

        if clean_params is not None:
            self._cursor.execute(sql, clean_params)
        else:
            self._cursor.execute(sql)

        if has_returning_id:
            try:
                row = self._cursor.fetchone()
                if row:
                    self.lastrowid = row[0]
            except Exception:
                self.lastrowid = None
        else:
            self.lastrowid = None
        return self

    def executemany(self, query, seq_of_params):
        sql, _ = translate_query(query, allow_returning=False)
        return self._cursor.executemany(sql, seq_of_params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        if size is not None:
            return self._cursor.fetchmany(size)
        return self._cursor.fetchmany()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def close(self):
        return self._cursor.close()

    def __iter__(self):
        return iter(self._cursor)

class PostgresConnectionWrapper:
    def __init__(self, raw_conn, pool=None):
        self._conn = raw_conn
        self._pool = pool
        self._closed = False

    def cursor(self, *args, **kwargs):
        # DictCursor provides both key and index access, fully compatible with sqlite3.Row
        raw_cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        return PostgresCursorWrapper(raw_cur)

    def execute(self, query, params=None):
        cur = self.cursor()
        cur.execute(query, params)
        return cur

    def commit(self):
        if not self._closed and self._conn and not self._conn.closed:
            return self._conn.commit()

    def rollback(self):
        if not self._closed and self._conn and not self._conn.closed:
            return self._conn.rollback()

    def close(self):
        if not self._closed:
            self._closed = True
            conn = self._conn
            pool = self._pool
            self._conn = None
            if pool and conn:
                try:
                    if not conn.closed:
                        conn.rollback()
                    pool.putconn(conn)
                except Exception:
                    try:
                        pool.putconn(conn, close=True)
                    except Exception:
                        pass
            elif conn and not conn.closed:
                try:
                    conn.close()
                except Exception:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

class SqliteCursorWrapper:
    def __init__(self, raw_cursor):
        self._cursor = raw_cursor

    def execute(self, query, params=None):
        sql = query.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        # Replace %s with ? outside quotes for SQLite compatibility
        if "%s" in sql:
            sql = convert_placeholders_to_qmark(sql)

        if params is not None:
            if isinstance(params, (list, tuple)):
                clean_params = tuple(int(p) if isinstance(p, bool) else p for p in params)
            elif isinstance(params, dict):
                clean_params = {k: int(v) if isinstance(v, bool) else v for k, v in params.items()}
            else:
                clean_params = (int(params) if isinstance(params, bool) else params,)
            return self._cursor.execute(sql, clean_params)
        return self._cursor.execute(sql)

    def executemany(self, query, seq_of_params):
        return self._cursor.executemany(query, seq_of_params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        if size is not None:
            return self._cursor.fetchmany(size)
        return self._cursor.fetchmany()

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def close(self):
        return self._cursor.close()

    def __iter__(self):
        return iter(self._cursor)

class SqliteConnectionWrapper:
    def __init__(self, raw_conn):
        self._conn = raw_conn
        self._conn.row_factory = sqlite3.Row

    def cursor(self):
        return SqliteCursorWrapper(self._conn.cursor())

    def execute(self, query, params=None):
        cur = self.cursor()
        cur.execute(query, params)
        return cur

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        self.close()

# ── Connection Pool Management ─────────────────────────────────
_db_pool = None
_request_db_conns = contextvars.ContextVar("_request_db_conns", default=None)

def reset_pool():
    global _db_pool
    if _db_pool is not None:
        try:
            _db_pool.closeall()
        except Exception:
            pass
        _db_pool = None

def get_pool():
    global _db_pool
    if _db_pool is None or _db_pool.closed:
        url = get_clean_database_url()
        if not url:
            return None
        try:
            _db_pool = psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=80, dsn=url)
        except Exception as e:
            print(f"[DATABASE NOTICE] Unable to create PostgreSQL pool ({e}).")
            _db_pool = None
            return None
    return _db_pool

def get_sqlite_db():
    db_path = get_local_sqlite_path()
    raw_conn = sqlite3.connect(db_path, check_same_thread=False)
    wrapper = SqliteConnectionWrapper(raw_conn)
    active_conns = _request_db_conns.get()
    if active_conns is not None:
        active_conns.append(wrapper)
    return wrapper

def get_db():
    url = get_clean_database_url()
    # If no Postgres URL configured or local SQLite requested, use zero-config SQLite
    if not url or url.startswith("sqlite"):
        return get_sqlite_db()

    pool = None
    try:
        pool = get_pool()
    except Exception as e:
        print(f"[DATABASE NOTICE] PostgreSQL connection failed ({e}). Operating in Local SQLite fallback mode.")
        return get_sqlite_db()

    if pool is None:
        return get_sqlite_db()

    raw_conn = None
    max_retries = 3
    for attempt in range(max_retries):
        try:
            raw_conn = pool.getconn()
            if raw_conn.closed != 0:
                pool.putconn(raw_conn, close=True)
                raw_conn = None
                continue

            # Quick ping to verify socket connection is still alive (reconnects after overnight timeout)
            try:
                with raw_conn.cursor() as test_cur:
                    test_cur.execute("SELECT 1;")
            except Exception:
                try:
                    pool.putconn(raw_conn, close=True)
                except Exception:
                    pass
                raw_conn = None
                if attempt >= 1:
                    reset_pool()
                    pool = get_pool()
                    if pool is None:
                        return get_sqlite_db()
                continue

            break
        except psycopg2.pool.PoolError:
            gc.collect()
            time.sleep(0.02)
            if attempt == max_retries - 1:
                try:
                    raw_conn = psycopg2.connect(get_clean_database_url())
                    wrapper = PostgresConnectionWrapper(raw_conn, None)
                    active_conns = _request_db_conns.get()
                    if active_conns is not None:
                        active_conns.append(wrapper)
                    return wrapper
                except Exception:
                    return get_sqlite_db()
        except Exception:
            if raw_conn is not None:
                try:
                    pool.putconn(raw_conn, close=True)
                except Exception:
                    pass
                raw_conn = None
            if attempt >= 1:
                reset_pool()
                try:
                    pool = get_pool()
                    if pool is None:
                        return get_sqlite_db()
                except Exception:
                    return get_sqlite_db()
            time.sleep(0.02)

    if raw_conn is None:
        try:
            raw_conn = pool.getconn()
        except Exception:
            try:
                raw_conn = psycopg2.connect(get_clean_database_url())
                wrapper = PostgresConnectionWrapper(raw_conn, None)
                active_conns = _request_db_conns.get()
                if active_conns is not None:
                    active_conns.append(wrapper)
                return wrapper
            except Exception as err:
                print(f"[DATABASE NOTICE] PostgreSQL connection failed ({err}). Operating in Local SQLite fallback mode.")
                return get_sqlite_db()

    wrapper = PostgresConnectionWrapper(raw_conn, pool)
    active_conns = _request_db_conns.get()
    if active_conns is not None:
        active_conns.append(wrapper)
    return wrapper

# ── Schema Initialization & Migrations ─────────────────────────
def init_db():
    url = get_clean_database_url()
    is_postgres = bool(url and not url.startswith("sqlite"))

    conn = get_db()
    cursor = conn.cursor()

    # 1. Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user' CHECK(role IN ('user', 'admin', 'superadmin')),
            credits INTEGER DEFAULT 5,
            is_banned INTEGER DEFAULT 0,
            warning_message TEXT,
            is_verified INTEGER DEFAULT 0,
            verification_token TEXT,
            brevo_api_key TEXT,
            brevo_account_status TEXT DEFAULT 'none',
            daily_email_limit INTEGER DEFAULT 300,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 2. Datasets table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS datasets (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            division TEXT,
            district TEXT,
            area TEXT,
            file_path TEXT NOT NULL,
            row_count INTEGER DEFAULT 0,
            column_names TEXT,
            price_credits INTEGER DEFAULT 10,
            is_active INTEGER DEFAULT 1,
            uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 3. Scrape jobs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scrape_jobs (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            query TEXT NOT NULL,
            division TEXT,
            district TEXT,
            area TEXT,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'running', 'done', 'failed')),
            result_path TEXT,
            result_count INTEGER DEFAULT 0,
            cost_credits INTEGER DEFAULT 20,
            error_message TEXT,
            promotion_status TEXT DEFAULT 'none',
            proposed_name TEXT,
            proposed_category TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        );
    """)

    # 4. Access logs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS access_logs (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            dataset_id INTEGER REFERENCES datasets(id) ON DELETE SET NULL,
            scrape_job_id INTEGER REFERENCES scrape_jobs(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            ip_address TEXT,
            accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 5. Credit transactions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credit_transactions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            amount INTEGER NOT NULL,
            transaction_type TEXT CHECK(transaction_type IN ('add', 'deduct')),
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 6. Scrape logs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scrape_logs (
            id SERIAL PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES scrape_jobs(id) ON DELETE CASCADE,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 7. WhatsApp progress tracking table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS whatsapp_progress (
            recipient_group TEXT PRIMARY KEY,
            last_index INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 8. Marketing campaigns table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS marketing_campaigns (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            campaign_type TEXT NOT NULL CHECK(campaign_type IN ('email', 'whatsapp')),
            recipient_group TEXT NOT NULL,
            template_preview TEXT,
            status TEXT DEFAULT 'running' CHECK(status IN ('running', 'done', 'failed', 'stopping', 'stopped')),
            sent_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            total_count INTEGER NOT NULL,
            start_row INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 9. Campaign logs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS campaign_logs (
            id SERIAL PRIMARY KEY,
            campaign_id TEXT NOT NULL REFERENCES marketing_campaigns(id) ON DELETE CASCADE,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 10. Dataset requests portal table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dataset_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            user_email TEXT NOT NULL,
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            business_name TEXT,
            category_query TEXT NOT NULL,
            division TEXT,
            district TEXT,
            area TEXT,
            additional_notes TEXT,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'fulfilled', 'rejected')),
            admin_notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 11. Security violations table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS security_violations (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            ip_address TEXT,
            user_agent TEXT,
            violation_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 12. Payment requests table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            package_name TEXT NOT NULL,
            credits_requested INTEGER NOT NULL,
            amount_bdt REAL NOT NULL,
            bkash_number TEXT NOT NULL,
            transaction_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
            rejection_reason TEXT,
            payment_method TEXT DEFAULT 'bkash',
            user_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP,
            processed_by INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
    """)

    # 13. Banned IPs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS banned_ips (
            ip_address TEXT PRIMARY KEY,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 14. Payment gateway settings table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT
        );
    """)

    # 15. Brevo applications table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS brevo_applications (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            business_name TEXT NOT NULL,
            domain_name TEXT NOT NULL,
            location TEXT NOT NULL,
            business_phone TEXT NOT NULL,
            social_media_website TEXT NOT NULL,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
            rejection_reason TEXT,
            assigned_api_key TEXT,
            daily_limit INTEGER DEFAULT 300,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP,
            processed_by INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
    """)

    # 16. Desktop Production Licenses table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            customer_name TEXT,
            customer_email TEXT,
            payment_request_id INTEGER REFERENCES payment_requests(id) ON DELETE SET NULL,
            production_key TEXT UNIQUE NOT NULL,
            license_token TEXT NOT NULL,
            plan_tier TEXT DEFAULT 'pro',
            credits_amount INTEGER DEFAULT 0,
            is_redeemed INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active' CHECK(status IN ('active', 'expired', 'revoked')),
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_validated_at TIMESTAMP
        );
    """)

    # Migrations for payment_requests (add production_key & license_expiry)
    try:
        cursor.execute("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS production_key TEXT;")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS license_expiry TIMESTAMP;")
    except Exception:
        pass

    # Migrations for licenses (add credits_amount & is_redeemed for deferred credit activation)
    try:
        cursor.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS credits_amount INTEGER DEFAULT 0;")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS is_redeemed INTEGER DEFAULT 0;")
    except Exception:
        pass

    # Seed or ensure Superadmin user exists
    try:
        pwd_hash = hash_password(SUPERADMIN_PASSWORD)
        cursor.execute("""
            INSERT INTO users (email, full_name, password_hash, role, credits, is_verified, is_banned)
            VALUES (%s, %s, %s, 'superadmin', 99999, 1, 0)
            ON CONFLICT (email) DO UPDATE SET
                role = 'superadmin',
                credits = 99999,
                is_verified = 1,
                is_banned = 0;
        """, (SUPERADMIN_EMAIL, SUPERADMIN_NAME, pwd_hash))
    except Exception as e:
        print("[SUPERADMIN SEED NOTICE]", e)

    try:
        admin_pwd = hash_password("admin123")
        cursor.execute("""
            INSERT INTO users (email, full_name, password_hash, role, credits, is_verified, is_banned)
            VALUES ('admin@databazaar.com', 'Admin Databazaar', %s, 'admin', 9999, 1, 0)
            ON CONFLICT (email) DO UPDATE SET
                role = 'admin',
                credits = 9999,
                is_verified = 1,
                is_banned = 0;
        """, (admin_pwd,))
    except Exception:
        pass

    conn.commit()
    conn.close()

    if is_postgres:
        print("[OK] Supabase PostgreSQL database schema initialized successfully.")
    else:
        print("[OK] Local SQLite database schema initialized successfully (datakarkhana_local.db).")

def reset_db_only_superadmin():
    conn = get_db()
    cursor = conn.cursor()

    tables = [
        "security_violations", "banned_ips", "access_logs", "credit_transactions",
        "scrape_logs", "scrape_jobs", "campaign_logs", "marketing_campaigns",
        "whatsapp_progress", "payment_requests", "dataset_requests", "brevo_applications"
    ]
    for table in tables:
        try:
            cursor.execute(f"TRUNCATE TABLE {table} CASCADE;")
        except Exception:
            try:
                cursor.execute(f"DELETE FROM {table};")
            except Exception:
                pass

    try:
        cursor.execute("DELETE FROM users WHERE role != 'superadmin';")
    except Exception:
        pass

    pwd_hash = hash_password(SUPERADMIN_PASSWORD)
    cursor.execute("""
        INSERT INTO users (email, full_name, password_hash, role, credits, is_verified, is_banned)
        VALUES (%s, %s, %s, 'superadmin', 99999, 1, 0)
        ON CONFLICT (email) DO UPDATE SET
            role = 'superadmin',
            credits = 99999,
            is_verified = 1,
            is_banned = 0;
    """, (SUPERADMIN_EMAIL, SUPERADMIN_NAME, pwd_hash))

    conn.commit()
    conn.close()
    print(f"[OK] Supabase Database cleaned! Superadmin ({SUPERADMIN_EMAIL}) verified.")

if __name__ == "__main__":
    init_db()
