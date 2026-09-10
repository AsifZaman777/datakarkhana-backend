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

# ── Brevo & Email Configuration ───────────────────────────
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
SMTP_USER = os.getenv("SMTP_USER", "asifdev777@gmail.com")

# ── Superadmin & Frontend URL Configuration ───────────────
FRONTEND_URL = os.getenv("FRONTEND_URL", "").rstrip("/")
SUPERADMIN_EMAIL = os.getenv("SUPERADMIN_EMAIL", "asifdev777@gmail.com")
SUPERADMIN_PASSWORD = os.getenv("SUPERADMIN_PASSWORD", "admin123")
SUPERADMIN_NAME = os.getenv("SUPERADMIN_NAME", "Asif Zaman (Superadmin)")

# ── bKash & Pathao Pay Pricing Configuration ───────────────
BKASH_NUMBER = os.getenv("BKASH_NUMBER", "+8801824500704")
BKASH_ACCOUNT_TYPE = os.getenv("BKASH_ACCOUNT_TYPE", "Personal (Send Money)")

PATHAO_NUMBER = os.getenv("PATHAO_NUMBER", "+8801824500704")
PATHAO_ACCOUNT_TYPE = os.getenv("PATHAO_ACCOUNT_TYPE", "Personal / Merchant")

def load_credit_packages_config():
    pkg_file = os.path.join(os.path.dirname(__file__), "packages.json")
    if os.path.exists(pkg_file):
        try:
            import json
            with open(pkg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
                elif isinstance(data, list):
                    return {"packages": data, "custom_package": {"name": "Custom Upgrade", "price_per_credit_bdt": 10, "min_credits": 10, "max_credits": 5000, "step": 10}}
        except Exception as e:
            print("[CONFIG PACKAGES LOAD ERROR]", e)
    return {
        "packages": [
            {
                "id": "starter",
                "name": "Starter Lead Pack",
                "credits": 50,
                "price_bdt": 500,
                "popular": False,
                "badge": "Starter",
                "description": "Ideal for small outreach campaigns & testing.",
                "features": ["50 Verified Lead Credits", "Full Phone & Email Access", "CSV & Excel File Exports"]
            },
            {
                "id": "pro",
                "name": "Pro Growth Pack",
                "credits": 200,
                "price_bdt": 1500,
                "popular": True,
                "badge": "Most Popular",
                "description": "Best value! Power your WhatsApp & Email campaigns.",
                "features": ["200 Verified Lead Credits", "CSV, Excel & PDF Exports", "WhatsApp Campaign Sender Tool"]
            },
            {
                "id": "enterprise",
                "name": "Enterprise Mega Pack",
                "credits": 500,
                "price_bdt": 3000,
                "popular": False,
                "badge": "Agency Choice",
                "description": "Maximum credits for high-volume agency scraping.",
                "features": ["500 Verified Lead Credits", "All Export Formats (Excel, CSV, PDF, JSON)", "Dedicated Account Manager"]
            }
        ],
        "custom_package": {
            "name": "Custom Upgrade",
            "price_per_credit_bdt": 10,
            "min_credits": 10,
            "max_credits": 5000,
            "step": 10,
            "description": "Select the exact credit amount your team requires:"
        }
    }

def load_credit_packages():
    cfg = load_credit_packages_config()
    return cfg.get("packages", [])

CREDIT_PACKAGES = load_credit_packages()

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
