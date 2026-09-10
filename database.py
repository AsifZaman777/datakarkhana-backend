import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "databazaar.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user' CHECK(role IN ('user', 'admin', 'superadmin')),
            credits INTEGER DEFAULT 5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Check if existing users table needs schema migration for superadmin
    try:
        cursor.execute("SELECT check_clause FROM sqlite_master WHERE type='table' AND name='users'")
        sql_info = cursor.fetchone()
        if sql_info and "superadmin" not in str(sql_info[0]):
            print("[MIGRATING SCHEMA] Updating users table CHECK constraint for superadmin role...")
            cursor.execute("DROP TABLE IF EXISTS users")
            cursor.execute("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    full_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT DEFAULT 'user' CHECK(role IN ('user', 'admin', 'superadmin')),
                    credits INTEGER DEFAULT 5,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
    except Exception:
        pass

    # Datasets table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS datasets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            uploaded_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (uploaded_by) REFERENCES users(id)
        )
    """)

    # Scrape jobs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scrape_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            query TEXT NOT NULL,
            division TEXT,
            district TEXT,
            area TEXT,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'running', 'done', 'failed')),
            result_path TEXT,
            result_count INTEGER DEFAULT 0,
            cost_credits INTEGER DEFAULT 20,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Access logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS access_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            dataset_id INTEGER,
            scrape_job_id INTEGER,
            action TEXT NOT NULL,
            ip_address TEXT,
            accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (dataset_id) REFERENCES datasets(id),
            FOREIGN KEY (scrape_job_id) REFERENCES scrape_jobs(id)
        )
    """)

    # Credit transactions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credit_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            transaction_type TEXT CHECK(transaction_type IN ('add', 'deduct')),
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Scraper logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scrape_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES scrape_jobs(id)
        )
    """)

    # WhatsApp progress tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS whatsapp_progress (
            recipient_group TEXT PRIMARY KEY,
            last_index INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Marketing campaigns table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS marketing_campaigns (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            campaign_type TEXT NOT NULL CHECK(campaign_type IN ('email', 'whatsapp')),
            recipient_group TEXT NOT NULL,
            template_preview TEXT,
            status TEXT DEFAULT 'running' CHECK(status IN ('running', 'done', 'failed', 'stopping', 'stopped')),
            sent_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            total_count INTEGER NOT NULL,
            start_row INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Marketing campaign logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS campaign_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES marketing_campaigns(id)
        )
    """)

    # Dataset Requests portal table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dataset_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Security Violations
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS security_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            ip_address TEXT,
            user_agent TEXT,
            violation_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Payment Requests table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            package_name TEXT NOT NULL,
            credits_requested INTEGER NOT NULL,
            amount_bdt REAL NOT NULL,
            bkash_number TEXT NOT NULL,
            transaction_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
            rejection_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP,
            processed_by INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (processed_by) REFERENCES users(id)
        )
    """)

    # Banned IPs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS banned_ips (
            ip_address TEXT PRIMARY KEY,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migration: Add new columns to existing tables if missing
    try:
        cursor.execute("ALTER TABLE marketing_campaigns ADD COLUMN failed_count INTEGER DEFAULT 0")
    except Exception:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE marketing_campaigns ADD COLUMN start_row INTEGER DEFAULT 0")
    except Exception:
        pass  # Column already exists
        
    # User ban & verification tracking migrations
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN warning_message TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN verification_token TEXT")
    except Exception:
        pass
    # Scrape jobs promotion tracking migrations
    try:
        cursor.execute("ALTER TABLE scrape_jobs ADD COLUMN promotion_status TEXT DEFAULT 'none'")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE scrape_jobs ADD COLUMN proposed_name TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE scrape_jobs ADD COLUMN proposed_category TEXT")
    except Exception:
        pass

    # Payment Requests table migrations
    try:
        cursor.execute("ALTER TABLE payment_requests ADD COLUMN payment_method TEXT DEFAULT 'bkash'")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE payment_requests ADD COLUMN user_name TEXT")
    except Exception:
        pass

    # Dynamic Payment Gateway Settings Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT
        )
    """)

    # Brevo Business Verification Applications Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS brevo_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
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
            processed_by INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (processed_by) REFERENCES users(id)
        )
    """)

    # Brevo Account & Limit Migrations
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN brevo_api_key TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN brevo_account_status TEXT DEFAULT 'none'")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN daily_email_limit INTEGER DEFAULT 300")
    except Exception:
        pass
    try:
        cursor.execute("UPDATE users SET is_banned = 0, is_verified = 1, warning_message = '' WHERE role = 'admin' OR email = 'admin@marketingostad.com' OR email = 'admin@databazaar.com'")
        cursor.execute("DELETE FROM banned_ips")
    except Exception:
        pass

    conn.commit()
    conn.close()
    print("[OK] Database schema initialized.")

def reset_db_only_superadmin():
    conn = get_db()
    cursor = conn.cursor()
    
    # Wipe all existing database tables
    for table in ["security_violations", "banned_ips", "access_logs", "credit_transactions", "scrape_logs", "scrape_jobs", "campaign_logs", "marketing_campaigns", "whatsapp_progress"]:
        try:
            cursor.execute(f"DELETE FROM {table}")
        except Exception:
            pass

    # Recreate users table to update CHECK constraint for superadmin role
    cursor.execute("DROP TABLE IF EXISTS users")
    cursor.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user' CHECK(role IN ('user', 'admin', 'superadmin')),
            credits INTEGER DEFAULT 5,
            is_banned INTEGER DEFAULT 0,
            warning_message TEXT,
            is_verified INTEGER DEFAULT 0,
            verification_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Insert only Superadmin user from env config
    from auth import hash_password
    from config import SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD, SUPERADMIN_NAME
    
    pwd_hash = hash_password(SUPERADMIN_PASSWORD)

    cursor.execute("""
        INSERT INTO users (email, full_name, password_hash, role, credits, is_verified, is_banned)
        VALUES (?, ?, ?, 'superadmin', 99999, 1, 0)
    """, (SUPERADMIN_EMAIL, SUPERADMIN_NAME, pwd_hash))

    conn.commit()
    conn.close()
    print(f"[OK] DATABASE CLEANED! ONLY Superadmin ({SUPERADMIN_EMAIL} / {SUPERADMIN_PASSWORD}) is registered.")

if __name__ == "__main__":
    init_db()
    reset_db_only_superadmin()
