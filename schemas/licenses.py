from typing import Optional
from pydantic import BaseModel

class AdminGenerateLicensePayload(BaseModel):
    customer_name: str
    customer_email: Optional[str] = ""
    expiry_days: Optional[int] = 30
    expires_at: Optional[str] = None
    custom_key: Optional[str] = None
    plan_tier: Optional[str] = "pro"
    credits_amount: Optional[int] = 0

class AdminExtendLicensePayload(BaseModel):
    additional_days: int = 30

class ActivateLicensePayload(BaseModel):
    license_key: str

class QuickRenewPayload(BaseModel):
    email: str
    password: str
    license_key: str
