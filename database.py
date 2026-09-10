import os
import re
import time
import gc
import contextvars
import psycopg2
import psycopg2.pool
import psycopg2.extras
from config import DATABASE_URL, SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD, SUPERADMIN_NAME
from auth import hash_password

TABLES_WITH_AUTO_ID = {
    "users", "datasets", "scrape_jobs", "access_logs", "credit_transactions",
    "scrape_logs", "campaign_logs", "dataset_requests", "security_violations",
    "payment_requests", "brevo_applications"
}

_INSERT_TABLE_RE = re.compile(r"^\s*INSERT\s+INTO\s+([a-zA-Z0-9_]+)", re.IGNORECASE)
_INSERT_OR_REPLACE_RE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+([a-zA-Z0-9_]+)", re.IGNORECASE)
_INSERT_OR_IGNORE_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+([a-zA-Z0-9_]+)", re.IGNORECASE)

CONFLICT_KEYS = {
    "payment_settings": ("setting_key", "setting_value = EXCLUDED.setting_value"),
    "whatsapp_progress": ("recipient_group", "last_index = EXCLUDED.last_index, updated_at = CURRENT_TIMESTAMP"),
    "banned_ips": ("ip_address", None),  # None means DO NOTHING
}

def get_clean_database_url() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL") or os.getenv("POSTGRES_URL") or DATABASE_URL
    if not url:
        return ""
    url = url.strip().strip("'").strip('"')
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

# ── Connection Pool Management ─────────────────────────────────
_db_pool = None
_request_db_conns = contextvars.ContextVar("_request_db_conns", default=None)

def get_pool():
    global _db_pool
    if _db_pool is None or _db_pool.closed:
        url = get_clean_database_url()
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set in backend/.env. "
                "Please configure your Supabase PostgreSQL connection string, e.g.:\n"
                "DATABASE_URL=postgresql://postgres:[PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres"
            )
        _db_pool = psycopg2.pool.ThreadedConnectionPool(minconn=5, maxconn=80, dsn=url)
    return _db_pool

def get_db():
    pool = get_pool()
    raw_conn = None
    max_retries = 20
    for attempt in range(max_retries):
        try:
            raw_conn = pool.getconn()
            if raw_conn.closed != 0:
                pool.putconn(raw_conn, close=True)
                raw_conn = None
                continue
            break
        except psycopg2.pool.PoolError:
            # Pool momentarily saturated: force GC to clean dropped wrappers and retry
            gc.collect()
            time.sleep(0.02)
            if attempt == max_retries - 1:
                # Fallback: create standalone direct connection instead of raising PoolError
                try:
                    raw_conn = psycopg2.connect(get_clean_database_url())
                    wrapper = PostgresConnectionWrapper(raw_conn, None)
                    active_conns = _request_db_conns.get()
                    if active_conns is not None:
                        active_conns.append(wrapper)
                    return wrapper
                except Exception:
                    raise
        except Exception:
            if raw_conn is not None:
                try:
                    pool.putconn(raw_conn, close=True)
                except Exception:
                    pass
                raw_conn = None
            if attempt >= 2:
                raise
            time.sleep(0.02)

    if raw_conn is None:
        try:
            raw_conn = pool.getconn()
        except psycopg2.pool.PoolError:
            raw_conn = psycopg2.connect(get_clean_database_url())
            wrapper = PostgresConnectionWrapper(raw_conn, None)
            active_conns = _request_db_conns.get()
            if active_conns is not None:
                active_conns.append(wrapper)
            return wrapper

    wrapper = PostgresConnectionWrapper(raw_conn, pool)

    # Automatically track with current HTTP request context if inside a web request
    active_conns = _request_db_conns.get()
    if active_conns is not None:
        active_conns.append(wrapper)

    return wrapper

# ── Schema Initialization & Migrations ─────────────────────────
def init_db():
    url = get_clean_database_url()
    if not url:
        print("[DATABASE WARNING] DATABASE_URL is not configured in backend/.env. Supabase PostgreSQL initialization skipped.")
        return

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

    # Clean up local SQLite db if present
    local_db_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "databazaar.db")
    if os.path.exists(local_db_file):
        try:
            os.remove(local_db_file)
            print("[OK] Removed local databazaar.db file.")
        except Exception as e:
            print("[WARNING] Could not remove local databazaar.db:", e)

    print("[OK] Supabase PostgreSQL database schema initialized successfully.")

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
