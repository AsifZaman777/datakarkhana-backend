from typing import Optional, List, Dict, Any
from pydantic import BaseModel

class ScrapeCredentials(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    cookies: Optional[str] = None

class ScrapeRequest(BaseModel):
    query: Optional[str] = None
    queries: Optional[List[str]] = None
    platform: Optional[str] = "google_maps"
    credentials: Optional[ScrapeCredentials] = None
    division: Optional[str] = None
    district: Optional[str] = None
    area: Optional[str] = None
    headless: Optional[bool] = False
    max_results: Optional[int] = 50

