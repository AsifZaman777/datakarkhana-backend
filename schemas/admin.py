from typing import Optional
from pydantic import BaseModel

class CreditRequest(BaseModel):
    user_id: int
    amount: int

class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    full_name: Optional[str] = None

class AllowSyncRequest(BaseModel):
    allow_sync: int

class UploadLimitRequest(BaseModel):
    max_sync_files: int

class BanRequest(BaseModel):
    is_banned: int
    warning_message: Optional[str] = ""
    ban_ip: Optional[bool] = False
    ip_address: Optional[str] = ""

class AdminWarningRequest(BaseModel):
    warning_message: str
