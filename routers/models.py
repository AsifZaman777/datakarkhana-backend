"""
FastAPI Router for Local AI Models
Provides endpoints for:
- Curated model recommendations and hardware compatibility stats
- Hugging Face model URL / repo inspection
- Background chunked streaming download with live ETA & progress polling
- Installed models management (activation, deletion, disk usage)
- Plug-and-play AI generation for marketing variants and spam auditing
"""
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, status

from schemas.models import (
    ModelSpec,
    SystemInfoResponse,
    InspectHuggingFaceRequest,
    InspectHuggingFaceResponse,
    DownloadModelRequest,
    DownloadProgressResponse,
    InstalledModelInfo,
    SetActiveModelRequest,
    GenerateVariantsRequest,
    GenerateVariantsResponse,
    AuditSpamRequest,
    AuditSpamResponse,
    UniversalGenerateRequest,
    UniversalGenerateResponse,
)
import services.model_service as model_svc

router = APIRouter(prefix="/api/models", tags=["Local AI Models"])


# ── System Info & Recommendations ─────────────────────────

@router.get("/system-info", response_model=SystemInfoResponse)
def get_system_info():
    """Returns host RAM, CPU cores, free disk space in AppData, and installed models count."""
    return model_svc.get_system_hardware_info()


@router.get("/recommended", response_model=List[ModelSpec])
def get_recommended_models():
    """Returns curated lightweight GGUF models with RAM, disk, and speed metrics."""
    return model_svc.get_recommended_catalog()


# ── Installed Models Management ───────────────────────────

@router.get("/installed", response_model=List[InstalledModelInfo])
def get_installed_models():
    """Returns all .gguf models currently stored in the AppData models directory."""
    return model_svc.get_installed_models()


@router.post("/set-active")
def set_active_model(payload: SetActiveModelRequest):
    """Sets a specific installed model as the default active model for inference."""
    success = model_svc.set_active_model(payload.filename)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model '{payload.filename}' is not installed.",
        )
    return {"status": "ok", "active_model": payload.filename}


@router.delete("/{filename}")
def delete_model(filename: str):
    """Deletes an installed model file from disk to free up storage."""
    success = model_svc.delete_model(filename)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Could not delete model '{filename}'. File may not exist.",
        )
    return {"status": "ok", "deleted": filename}


# ── Hugging Face Link Inspector ───────────────────────────

@router.post("/inspect", response_model=InspectHuggingFaceResponse)
def inspect_huggingface_url(payload: InspectHuggingFaceRequest):
    """Verifies a Hugging Face model link or repo ID and retrieves size/quantization metadata."""
    res = model_svc.inspect_huggingface_url(payload.url_or_repo)
    if not res.get("valid"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=res.get("error", "Invalid Hugging Face model URL or repository."),
        )
    return res


# ── Download Manager ──────────────────────────────────────

@router.post("/download", response_model=DownloadProgressResponse)
def download_model(payload: DownloadModelRequest):
    """Initiates an asynchronous chunked download from Hugging Face or recommended catalog."""
    try:
        res = model_svc.start_model_download(
            model_id=payload.model_id,
            custom_url=payload.custom_url,
            custom_name=payload.custom_name,
            filename=payload.filename,
        )
        return res
    except ValueError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))
    except Exception as err:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Download failed: {err}")


@router.get("/download/progress/{task_id}", response_model=DownloadProgressResponse)
def get_download_progress(task_id: str):
    """Polls real-time download telemetry (percent, speed in MB/s, ETA, bytes)."""
    progress = model_svc.get_download_progress(task_id)
    if not progress:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Download task '{task_id}' not found.",
        )
    return progress


@router.post("/download/cancel/{task_id}")
def cancel_download(task_id: str):
    """Cancels an ongoing model download and removes incomplete partial files."""
    success = model_svc.cancel_download(task_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Download task '{task_id}' not found or already finished.",
        )
    return {"status": "cancelled", "task_id": task_id}


# ── AI Generation (Variants & Spam Audit) ─────────────────

@router.post("/generate-variants", response_model=GenerateVariantsResponse)
def generate_variants(payload: GenerateVariantsRequest):
    """Generates 3-4 diverse variations of a WhatsApp message to bypass hash bans."""
    if not payload.base_template.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="base_template cannot be empty")

    res = model_svc.generate_marketing_variants(
        base_template=payload.base_template,
        count=payload.count or 3,
        language=payload.language or "bn",
        model_filename=payload.model_filename,
    )
    return res


@router.post("/audit-spam", response_model=AuditSpamResponse)
def audit_spam(payload: AuditSpamRequest):
    """Runs deep AI spam inspection on message text and returns conversational rewrites."""
    if not payload.text.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="text cannot be empty")

    res = model_svc.audit_message_spam(
        text=payload.text,
        language=payload.language or "bn",
        model_filename=payload.model_filename,
    )
    return res


@router.post("/generate", response_model=UniversalGenerateResponse)
def generate_text(payload: UniversalGenerateRequest):
    """Universal prompt completion for plug-and-play local AI across the application."""
    if not payload.prompt.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="prompt cannot be empty")

    res = model_svc.universal_generate(
        prompt=payload.prompt,
        model_filename=payload.model_filename,
        max_tokens=payload.max_tokens or 512,
        temperature=payload.temperature or 0.7,
    )
    return res
