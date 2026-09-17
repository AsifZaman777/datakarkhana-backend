from typing import Optional, List
from pydantic import BaseModel

class ScrapeRequest(BaseModel):
    query: Optional[str] = None
    queries: Optional[List[str]] = None
    division: Optional[str] = None
    district: Optional[str] = None
    area: Optional[str] = None
    headless: Optional[bool] = False
