from routers.health import router as health_router
from routers.auth import router as auth_router
from routers.datasets import router as datasets_router
from routers.scraper import router as scraper_router
from routers.payments import router as payments_router
from routers.licenses import router as licenses_router
from routers.marketing import router as marketing_router
from routers.admin import router as admin_router
from routers.config import router as config_router

__all__ = [
    "health_router",
    "auth_router",
    "datasets_router",
    "scraper_router",
    "payments_router",
    "licenses_router",
    "marketing_router",
    "admin_router",
    "config_router",
]
