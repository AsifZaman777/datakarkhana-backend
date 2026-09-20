from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, Callable
import re
import time
from selenium.webdriver.remote.webdriver import WebDriver

class BaseScraper(ABC):
    """
    Abstract Base Class for all decoupled platform scrapers.
    Every platform scraper implements authentication (if required),
    search bar query execution, and standardized lead record extraction.
    """
    platform_id: str = "base"
    platform_name: str = "Base Platform"
    requires_auth: bool = False
    description: str = "Base lead scraper"
    icon: str = "globe"

    def __init__(
        self,
        driver: WebDriver,
        log_cb: Optional[Callable[[str], None]] = None,
        job_id: Optional[int] = None,
    ):
        self.driver = driver
        self.log_cb = log_cb or print
        self.job_id = job_id

    def log(self, message: str):
        """Emit a formatted log message to terminal, database, and WebSocket"""
        if self.log_cb:
            try:
                self.log_cb(message)
            except Exception:
                pass

    def push_frame(self):
        """Capture and publish live browser frame to active WebSocket subscribers"""
        if self.job_id and self.driver:
            try:
                from scraper import push_scraper_frame
                push_scraper_frame(self.driver, self.job_id)
            except Exception:
                pass

    def is_stopped(self) -> bool:
        """Check if user pressed Stop button in the console"""
        if self.job_id:
            try:
                from scraper import is_job_stopped
                return is_job_stopped(self.job_id)
            except Exception:
                pass
        return False

    def emit_progress(self, count: int, action: str, item: Optional[Dict[str, Any]] = None):
        """Publish real-time extraction progress event"""
        if self.job_id:
            try:
                from scraper import publish_scraper_event
                publish_scraper_event(self.job_id, {
                    "type": "progress",
                    "count": count,
                    "action": action,
                    "item": item or {}
                })
            except Exception:
                pass

    @abstractmethod
    def authenticate(self, credentials: Optional[Dict[str, str]] = None) -> bool:
        """
        Execute login flow if the platform requires credentials.
        Returns True if authenticated or if no auth is needed, False on auth failure.
        """
        pass

    @abstractmethod
    def search_and_extract(self, query: str, max_results: int = 50) -> List[Dict[str, Any]]:
        """
        Navigate to platform search bar, execute query search, and extract leads.
        Returns a list of standardized lead dictionaries.
        """
        pass

    @staticmethod
    def extract_emails(text: str) -> List[str]:
        """Extract valid email addresses from any free-form text or page source"""
        if not text:
            return []
        pattern = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
        matches = re.findall(pattern, text)
        clean = []
        for m in matches:
            m_lower = m.lower().strip('.')
            if not any(ignored in m_lower for ignored in ['example.com', 'email.com', '.png', '.jpg', '.svg', '.webp']):
                if m_lower not in clean:
                    clean.append(m_lower)
        return clean

    @staticmethod
    def extract_phone_numbers(text: str) -> str:
        """Extract phone number (supporting Bangladesh and international numbers)"""
        if not text:
            return ""
        from scraper import extract_phone
        return extract_phone(text)

    def normalize_lead(
        self,
        name: str,
        phone: str = "",
        email: str = "",
        category: str = "",
        address: str = "",
        website: str = "",
        profile_url: str = "",
        rating: str = "",
        query: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Returns a normalized dictionary schema consistent across all platforms.
        This guarantees Excel export, CRM pipelines, and WhatsApp/Email campaigns work seamlessly.
        """
        lead = {
            "Name": (name or "").strip(),
            "Phone": (phone or "").strip(),
            "Email": (email or "").strip(),
            "Category": (category or "").strip(),
            "Address": (address or "").strip(),
            "Website": (website or "").strip(),
            "Rating": (rating or "").strip(),
            "Profile URL": (profile_url or "").strip(),
            "Platform": self.platform_name,
            "Query": (query or "").strip(),
        }
        if extra:
            for k, v in extra.items():
                if k not in lead and v:
                    lead[k] = str(v).strip()
        return lead
