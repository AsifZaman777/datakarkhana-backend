from typing import Optional, List, Dict, Any
from pydantic import BaseModel

class WhatsAppCampaignRequest(BaseModel):
    recipient_group: str
    message_template: str
    resume: Optional[bool] = False
    start_row: Optional[int] = None
    selected_contacts: Optional[List[Dict[str, Any]]] = None

class EmailCampaignRequest(BaseModel):
    recipient_group: str
    subject: str
    html_code: str
    selected_contacts: Optional[List[Dict[str, Any]]] = None

class BrevoApplyRequest(BaseModel):
    business_name: str
    domain_name: str
    location: str
    business_phone: str
    social_media_website: str

class BrevoApproveRequest(BaseModel):
    api_key: Optional[str] = ""
    daily_limit: Optional[int] = 300
    account_status: Optional[str] = "approved"

class BrevoRejectRequest(BaseModel):
    reason: str

class UserBrevoConfigRequest(BaseModel):
    api_key: str
    daily_limit: Optional[int] = 300
    account_status: Optional[str] = "approved"

class BrevoActivateLinkRequest(BaseModel):
    activation_url: str
