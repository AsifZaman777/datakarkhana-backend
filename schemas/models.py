"""
Pydantic schemas for Local AI Model Management, Hugging Face downloads,
system resource specs, and local inference for marketing variants and spam auditing.
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class ModelSpec(BaseModel):
    """Specification of a recommended or custom local AI model"""
    id: str
    name: str
    author: str
    filename: str
    file_size_bytes: int
    file_size_formatted: str
    ram_required_mb: int
    context_window: int
    inference_speed_tok_s: str
    tags: List[str]
    download_url: str
    description_en: str
    description_bn: str
    recommended: bool = False
    is_installed: bool = False
    is_active: bool = False


class SystemInfoResponse(BaseModel):
    """Host machine hardware stats for checking model compatibility"""
    total_ram_gb: float
    available_ram_gb: float
    cpu_cores: int
    models_dir: str
    disk_free_gb: float
    installed_models_count: int


class InspectHuggingFaceRequest(BaseModel):
    """Request to inspect a Hugging Face link or repo before downloading"""
    url_or_repo: str


class InspectHuggingFaceResponse(BaseModel):
    """Result of inspecting a Hugging Face link/repo"""
    valid: bool
    filename: str
    download_url: str
    file_size_bytes: int
    file_size_formatted: str
    estimated_ram_mb: int
    model_name: str
    author: str
    error: Optional[str] = None


class DownloadModelRequest(BaseModel):
    """Request to initiate background download of a local model"""
    model_id: Optional[str] = None
    custom_url: Optional[str] = None
    custom_name: Optional[str] = None
    filename: Optional[str] = None


class DownloadProgressResponse(BaseModel):
    """Real-time progress for an ongoing or completed model download"""
    task_id: str
    model_id: str
    filename: str
    name: str = ""
    total_bytes: int = 0
    downloaded_bytes: int = 0
    progress_percent: float = 0.0
    speed_mbps: float = 0.0
    eta_seconds: int = 0
    status: str = "pending"  # "pending", "downloading", "completed", "failed", "cancelled"
    error: Optional[str] = None
    message: Optional[str] = None


class InstalledModelInfo(BaseModel):
    """Metadata for a model currently saved on disk in AppData"""
    id: str
    name: str
    filename: str
    file_size_bytes: int
    file_size_formatted: str
    ram_required_mb: int
    path: str
    installed_at: str
    is_active: bool = False
    tags: List[str] = []


class SetActiveModelRequest(BaseModel):
    """Request to activate a specific installed model as default"""
    filename: str
    task: Optional[str] = "general"


class GenerateVariantsRequest(BaseModel):
    """Request to generate marketing message variants using local AI"""
    base_template: str
    count: Optional[int] = 3
    language: Optional[str] = "bn"
    model_filename: Optional[str] = None


class GenerateVariantsResponse(BaseModel):
    """Generated message variants ready for Selenium random distribution"""
    variants: List[str]
    model_used: str
    latency_ms: int
    tokens_generated: Optional[int] = 0


class AuditSpamRequest(BaseModel):
    """Request to run deep AI spam inspection on a text template"""
    text: str
    language: Optional[str] = "bn"
    model_filename: Optional[str] = None


class AuditSpamResponse(BaseModel):
    """Detailed AI spam audit findings with suggested natural alternatives"""
    risk_level: str  # "low", "medium", "high"
    spam_score: int  # 0 to 100
    flagged_reasons: List[str]
    suggested_rewrite: str
    model_used: str
    latency_ms: int


class ChatMessage(BaseModel):
    """Single turn in a conversational exchange"""
    role: str  # "system", "user", "assistant"
    content: str


class UniversalGenerateRequest(BaseModel):
    """Universal prompt completion and multi-turn chat for plug-and-play local AI tasks"""
    prompt: Optional[str] = None
    messages: Optional[List[ChatMessage]] = None
    system_prompt: Optional[str] = None
    model_filename: Optional[str] = None
    max_tokens: Optional[int] = 512
    temperature: Optional[float] = 0.7


class UniversalGenerateResponse(BaseModel):
    """Universal text generation result"""
    text: str
    model_used: str
    latency_ms: int
