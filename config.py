"""
MarketingOstad — Backend Configuration
Region hierarchy and categories for Bangladesh
"""
import os

# Auto-load .env file if present
_env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_file):
    with open(_env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip().strip("'").strip('"')

# ── Supabase PostgreSQL Database Configuration ────────────
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL") or os.getenv("POSTGRES_URL") or ""

# ── Supabase Storage Configuration (Buckets for Datasets) ────
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or ""
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "datasets").strip()

# ── Brevo & Email Configuration ───────────────────────────
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
SMTP_USER = os.getenv("SMTP_USER", "asifdev777@gmail.com")

# ── Superadmin & Frontend URL Configuration ───────────────
FRONTEND_MODE = os.getenv("FRONTEND_MODE", "local").strip().lower()  # "local" or "render"
FRONTEND_LOCAL_URL = os.getenv("FRONTEND_LOCAL_URL", "http://localhost:3000").rstrip("/")
FRONTEND_RENDER_URL = os.getenv("FRONTEND_RENDER_URL", "").rstrip("/")
FRONTEND_URL = os.getenv("FRONTEND_URL", "").rstrip("/") or (FRONTEND_RENDER_URL if FRONTEND_MODE in ("render", "production", "prod") else FRONTEND_LOCAL_URL)
SUPERADMIN_EMAIL = os.getenv("SUPERADMIN_EMAIL", "asifdev777@gmail.com")
SUPERADMIN_PASSWORD = os.getenv("SUPERADMIN_PASSWORD", "admin123")
SUPERADMIN_NAME = os.getenv("SUPERADMIN_NAME", "Asif Zaman (Superadmin)")

# ── bKash & Pathao Pay Pricing Configuration ───────────────
BKASH_NUMBER = os.getenv("BKASH_NUMBER", "+8801824500704")
BKASH_ACCOUNT_TYPE = os.getenv("BKASH_ACCOUNT_TYPE", "Personal (Send Money)")

PATHAO_NUMBER = os.getenv("PATHAO_NUMBER", "+8801824500704")
PATHAO_ACCOUNT_TYPE = os.getenv("PATHAO_ACCOUNT_TYPE", "Personal / Merchant")



# ── Dataset Categories ───────────────────────────────────
CATEGORIES = [
    "Coaching Center",
    "Pharmacy",
    "Medicine Store",
    "Hospital & Clinic",
    "Restaurant",
    "School & College",
    "Grocery Store",
    "Real Estate",
    "Gym & Fitness",
    "Salon & Beauty",
    "Other",
]

# ── Bangladesh Region Hierarchy ──────────────────────────
# Division → District → Sub-areas
REGIONS = {
    "Dhaka": {
        "Dhaka City": [
            "Mirpur", "Mirpur 1", "Mirpur 2", "Mirpur 10",
            "Mirpur 11", "Mirpur 12", "Mirpur 13", "Mirpur 14",
            "Pallabi", "Kafrul", "Shah Ali",
            "Uttara", "Uttara Sector 1-6", "Uttara Sector 7-14",
            "Dhanmondi", "Lalmatia", "Jigatola",
            "Gulshan", "Banani", "Baridhara",
            "Mohammadpur", "Adabor", "Shyamoli",
            "Motijheel", "Paltan", "Wari",
            "Farmgate", "Tejgaon", "Karwan Bazar",
            "Old Dhaka", "Lalbagh", "Hazaribagh",
            "Basundhara", "Badda", "Rampura",
            "Khilgaon", "Mugda", "Demra",
            "Jatrabari", "Jurain", "Postogola",
        ],
        "Gazipur": ["Tongi", "Gazipur Sadar", "Kaliakair", "Kapasia", "Sreepur"],
        "Narayanganj": ["Narayanganj Sadar", "Siddhirganj", "Fatulla", "Sonargaon", "Rupganj"],
        "Manikganj": ["Manikganj Sadar", "Singair", "Saturia", "Harirampur"],
        "Narsingdi": ["Narsingdi Sadar", "Palash", "Shibpur", "Monohardi"],
    },
    "Chittagong": {
        "Chittagong City": [
            "Agrabad", "Nasirabad", "GEC Circle", "Halishahar",
            "Pahartali", "Patenga", "Kotwali", "Chawkbazar",
        ],
        "Cox's Bazar": ["Cox's Bazar Sadar", "Teknaf", "Ukhia", "Ramu"],
        "Comilla": ["Comilla Sadar", "Daudkandi", "Muradnagar", "Laksam"],
    },
    "Rajshahi": {
        "Rajshahi City": ["Boalia", "Rajpara", "Motihar", "Shah Makhdum"],
        "Bogra": ["Bogra Sadar", "Sherpur", "Shibganj", "Gabtali"],
        "Rangpur": ["Rangpur Sadar", "Mithapukur", "Pirganj", "Badarganj"],
    },
    "Khulna": {
        "Khulna City": ["Khalishpur", "Sonadanga", "Boyra", "Daulatpur"],
        "Jessore": ["Jessore Sadar", "Benapole", "Jhikargachha", "Manirampur"],
    },
    "Sylhet": {
        "Sylhet City": ["Zindabazar", "Amberkhana", "Subid Bazar", "Kumargaon"],
        "Habiganj": ["Habiganj Sadar", "Chunarughat", "Madhabpur"],
    },
    "Barisal": {
        "Barisal City": ["Barisal Sadar", "Kotwali", "Band Road"],
        "Patuakhali": ["Patuakhali Sadar", "Kuakata", "Galachipa"],
    },
    "Mymensingh": {
        "Mymensingh City": ["Mymensingh Sadar", "Trishal", "Muktagachha"],
        "Jamalpur": ["Jamalpur Sadar", "Sherpur", "Islampur"],
    },
}
