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

class TierPermissionItem(BaseModel):
    tier_id: str
    tier_name: str
    allow_sync: bool = True
    max_sync_files: int = 5
    allow_dataset_download: bool = True
    allow_daraz_download: bool = True
    can_use_scraper: bool = True
    can_use_marketing: bool = True

class SaveTierPermissionsRequest(BaseModel):
    tiers: list[TierPermissionItem]
    apply_to_existing_users: Optional[bool] = False

class UserPermissionOverrideRequest(BaseModel):
    allow_sync: Optional[int] = None
    max_sync_files: Optional[int] = None
    allow_download: Optional[int] = None
    plan_tier: Optional[str] = None
