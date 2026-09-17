import os
from fastapi import APIRouter

from config import REGIONS, CATEGORIES

router = APIRouter(tags=["Config"])

@router.get("/api/config/regions")
def get_regions_config():
    """Return Bangladesh Region Hierarchy, Categories, and Contact info from .env"""
    return {
        "regions": REGIONS,
        "categories": CATEGORIES,
        "contacts": {
            "support_email": os.getenv("SUPPORT_EMAIL", os.getenv("SMTP_USER", "asifdev777@gmail.com")),
            "hotline_phone": os.getenv("HOTLINE_PHONE", "+880 1700-000000"),
            "support_hours": os.getenv("SUPPORT_HOURS", "24/7 Automated System & Live WhatsApp Assistance")
        }
    }
