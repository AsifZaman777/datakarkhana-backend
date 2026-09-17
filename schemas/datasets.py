from typing import Optional
from pydantic import BaseModel

class DatasetRequestCreate(BaseModel):
    name: Optional[str] = ""
    category_query: Optional[str] = None
    category: Optional[str] = "Scraped Leads"
    division: Optional[str] = None
    district: Optional[str] = None
    area: Optional[str] = None
    business_name: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    additional_notes: Optional[str] = None
    estimated_rows: Optional[int] = 0

class DatasetRequestStatusUpdate(BaseModel):
    status: str
    admin_notes: Optional[str] = None
