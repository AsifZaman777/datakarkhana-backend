from typing import Dict, Type, Optional, List, Any, Callable
from selenium.webdriver.remote.webdriver import WebDriver

from scrapers.base import BaseScraper
from scrapers.google_maps import GoogleMapsScraper
from scrapers.facebook import FacebookScraper
from scrapers.linkedin import LinkedInScraper
from scrapers.instagram import InstagramScraper
from scrapers.yellowpages import YellowPagesScraper

class ScraperRegistry:
    """
    Central registry and factory for all decoupled platform scrapers.
    New platforms can easily be added by registering a new BaseScraper subclass.
    """
    _registry: Dict[str, Type[BaseScraper]] = {
        "google_maps": GoogleMapsScraper,
        "facebook": FacebookScraper,
        "linkedin": LinkedInScraper,
        "instagram": InstagramScraper,
        "yellowpages": YellowPagesScraper,
    }

    @classmethod
    def register(cls, platform_id: str, scraper_cls: Type[BaseScraper]):
        cls._registry[platform_id.lower()] = scraper_cls

    @classmethod
    def get_scraper(
        cls,
        platform_id: Optional[str],
        driver: WebDriver,
        log_cb: Optional[Callable[[str], None]] = None,
        job_id: Optional[int] = None,
    ) -> BaseScraper:
        key = (platform_id or "google_maps").lower().strip()
        scraper_cls = cls._registry.get(key, GoogleMapsScraper)
        return scraper_cls(driver=driver, log_cb=log_cb, job_id=job_id)

    @classmethod
    def list_platforms(cls) -> List[Dict[str, Any]]:
        """Return metadata for all registered platforms for UI and API clients"""
        platforms = []
        for key, scraper_cls in cls._registry.items():
            platforms.append({
                "id": scraper_cls.platform_id,
                "name": scraper_cls.platform_name,
                "requires_auth": scraper_cls.requires_auth,
                "description": scraper_cls.description,
                "icon": scraper_cls.icon,
            })
        return platforms
