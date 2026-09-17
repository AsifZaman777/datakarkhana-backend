from typing import Optional, List, Dict, Any
from pydantic import BaseModel

class SavePackagesPayload(BaseModel):
    packages: List[Dict[str, Any]]
    custom_package: Optional[Dict[str, Any]] = None

class PaymentRequestPayload(BaseModel):
    package_name: str
    credits_requested: int
    amount_bdt: float
    payment_method: str = "bkash"
    user_name: Optional[str] = None
    bkash_number: str
    transaction_id: str

class ApprovePaymentPayload(BaseModel):
    expiry_days: Optional[int] = 30
    expires_at: Optional[str] = None
    custom_key: Optional[str] = None
