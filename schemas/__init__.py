from .auth import (
    RegisterRequest,
    LoginRequest,
    VerifyOtpRequest,
    ResendVerificationRequest,
    DeductCreditRequest,
    ViolationRequest,
)
from .datasets import (
    DatasetRequestCreate,
    DatasetRequestStatusUpdate,
)
from .scraper import (
    ScrapeRequest,
)
from .payments import (
    SavePackagesPayload,
    PaymentRequestPayload,
    ApprovePaymentPayload,
)
from .licenses import (
    AdminGenerateLicensePayload,
    AdminExtendLicensePayload,
    ActivateLicensePayload,
    QuickRenewPayload,
)
from .marketing import (
    WhatsAppCampaignRequest,
    EmailCampaignRequest,
    BrevoApplyRequest,
    BrevoApproveRequest,
    BrevoRejectRequest,
    UserBrevoConfigRequest,
    BrevoActivateLinkRequest,
)
from .admin import (
    CreditRequest,
    UpdateUserRequest,
    AllowSyncRequest,
    UploadLimitRequest,
    BanRequest,
    AdminWarningRequest,
)
