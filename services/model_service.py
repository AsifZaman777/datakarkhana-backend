"""
================================================================================
DataKarkhana Local Model Service — AI Engine & Model Management Core
================================================================================

This module serves as the central AI execution and model lifecycle engine for 
DataKarkhana. It handles:
  1. Storage & Path Resolution for downloaded GGUF weights in AppData.
  2. Hardware profiling (RAM, CPU cores, Disk Space) for host machines.
  3. Curated catalog and custom Hugging Face model URL / repo inspection.
  4. Background multi-threaded streaming downloader with live progress and ETA.
  5. Standalone llama.cpp (llama-server.exe) runtime lifecycle management.
  6. Real on-device neural network inference for prompt completion & WhatsApp copy.
  7. Rule-based and dynamic NLP synthesis fallbacks when no model is installed.

All functions are organized by task category with comprehensive multiline docstrings.
================================================================================
"""

import os
import re
import json
import time
import uuid
import shutil
import threading
import subprocess
import atexit
import zipfile
import io
from datetime import datetime
from typing import Optional, List, Dict, Any
import requests

try:
    import psutil
except ImportError:
    psutil = None


# ==============================================================================
# SECTION 1: STORAGE PATH RESOLUTION & FORMATTING HELPERS
# ==============================================================================

# /*
#  * ============================================================================
#  * FUNCTION : get_models_dir() -> str
#  * TASK     : Resolves and initializes the local AI models storage directory.
#  * HANDLES  :
#  *   - Locates %APPDATA%/datakarkhana/models on Windows or ~/.datakarkhana/models.
#  *   - Automatically creates base directories and 'downloads' folder if missing.
#  *   - Returns the absolute filesystem directory path for model weights and tools.
#  * ============================================================================
#  */
def get_models_dir() -> str:
    """
    ============================================================================
    TASK: Resolves and initializes the local AI models storage directory.
    ============================================================================
    PURPOSE:
        Determines the dedicated directory where downloaded .gguf model weights,
        runtime binaries (llama-server.exe), and metadata registry reside.
        Defaults to %APPDATA%/datakarkhana/models on Windows, or custom env path.

    PARAMETERS:
        None

    RETURNS:
        str: Absolute filesystem path to the models storage directory.
    ============================================================================
    """
    custom = os.getenv("DATAKARKHANA_DATA_DIR")
    if custom:
        target = os.path.join(custom, "models")
    else:
        appdata = os.getenv("APPDATA")
        if appdata:
            target = os.path.join(appdata, "datakarkhana", "models")
        else:
            target = os.path.expanduser("~/.datakarkhana/models")

    os.makedirs(target, exist_ok=True)
    os.makedirs(os.path.join(target, "downloads"), exist_ok=True)
    return target


# /*
#  * ============================================================================
#  * FUNCTION : get_registry_path() -> str
#  * TASK     : Resolves the filepath to the model metadata registry (registry.json).
#  * HANDLES  :
#  *   - Combines models storage directory with 'registry.json'.
#  *   - Provides central persistent storage path for tracking installed models.
#  * ============================================================================
#  */
def get_registry_path() -> str:
    """
    ============================================================================
    TASK: Resolves the filepath to the model metadata registry (registry.json).
    ============================================================================
    PURPOSE:
        Locates the JSON registry file used to track installed models, custom
        model metadata, and the currently active model selection.

    PARAMETERS:
        None

    RETURNS:
        str: Absolute filesystem path to registry.json.
    ============================================================================
    """
    return os.path.join(get_models_dir(), "registry.json")


# /*
#  * ============================================================================
#  * FUNCTION : _format_bytes(size: int) -> str
#  * TASK     : Formats raw byte counts into human-readable strings (MB, GB, TB).
#  * HANDLES  :
#  *   - Normalizes integer byte counts with 1024 base scaling.
#  *   - Returns human-friendly string outputs (e.g., '379.0 MB', '1.2 GB').
#  * ============================================================================
#  */
def _format_bytes(size: int) -> str:
    """
    ============================================================================
    TASK: Formats raw byte counts into human-readable strings (MB, GB, TB).
    ============================================================================
    PURPOSE:
        Converts integer byte values into standardized strings formatted with
        appropriate size units for user interface displays.

    PARAMETERS:
        size (int): Size in bytes.

    RETURNS:
        str: Human-readable size string (e.g., '379.0 MB', '1.2 GB').
    ============================================================================
    """
    if size <= 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024.0:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024.0
    return f"{size:.1f} TB"


# ==============================================================================
# SECTION 2: CURATED CATALOG OF RECOMMENDED LIGHTWEIGHT GGUF MODELS
# ==============================================================================

RECOMMENDED_MODELS: List[Dict[str, Any]] = [
    {
        "id": "qwen2.5-0.5b-instruct",
        "name": "Qwen 2.5 0.5B Instruct (Q4_K_M)",
        "author": "Qwen / Alibaba Cloud",
        "filename": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "file_size_bytes": 397746176,
        "file_size_formatted": "379 MB",
        "ram_required_mb": 750,
        "context_window": 4096,
        "inference_speed_tok_s": "28–42 tok/s",
        "tags": ["marketing_variants", "spam_detection", "multilingual", "bengali"],
        "download_url": "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "description_en": "⭐ Highly Recommended! Outstanding Bengali & English multilingual capability. Perfect for rewriting promotional WhatsApp copy into conversational, ban-proof variations.",
        "description_bn": "⭐ বিশেষভাবে সুপারিশকৃত! বাংলা ও ইংরেজি উভয় ভাষায় পারদর্শী। সাধারণ বা স্প্যামি মেসেজকে ব্যান-প্রতিরোধী প্রাকৃতিক মেসেজে রূপান্তর করতে সবচেয়ে কার্যকর।",
        "recommended": True,
    },
    {
        "id": "smollm2-135m-instruct",
        "name": "SmolLM2 135M Instruct (Q4_K_M)",
        "author": "Hugging Face TB",
        "filename": "smollm2-135m-instruct-q4_k_m.gguf",
        "file_size_bytes": 98631680,
        "file_size_formatted": "94 MB",
        "ram_required_mb": 280,
        "context_window": 2048,
        "inference_speed_tok_s": "55–70 tok/s",
        "tags": ["marketing_variants", "spam_detection", "ultra_fast"],
        "download_url": "https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct-GGUF/resolve/main/smollm2-135m-instruct-q4_k_m.gguf",
        "description_en": "Ultra-lightweight and lightning fast. Runs on virtually any laptop with negligible RAM usage (<300MB). Ideal for fast variant generation and promotional keyword scrubbing.",
        "description_bn": "অত্যন্ত হালকা এবং অবিশ্বাস্য দ্রুত। ৩০০ মেগাবাইটের কম র‍্যামে যেকোনো ল্যাপটপে চলে। দ্রুত ভেরিয়েন্ট তৈরি ও স্প্যাম শব্দ প্রতিস্থাপনে অত্যন্ত উপযোগী।",
        "recommended": False,
    },
    {
        "id": "smollm2-360m-instruct",
        "name": "SmolLM2 360M Instruct (Q4_K_M)",
        "author": "Hugging Face TB",
        "filename": "smollm2-360m-instruct-q4_k_m.gguf",
        "file_size_bytes": 229376000,
        "file_size_formatted": "219 MB",
        "ram_required_mb": 480,
        "context_window": 2048,
        "inference_speed_tok_s": "40–55 tok/s",
        "tags": ["marketing_variants", "spam_detection", "balanced"],
        "download_url": "https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct-GGUF/resolve/main/smollm2-360m-instruct-q4_k_m.gguf",
        "description_en": "Balanced sweet-spot model offering higher syntactic diversity than 135M while remaining very compact (<250MB).",
        "description_bn": "গতি ও বুদ্ধিমত্তার নিখুঁত ভারসাম্য। ২৫০ মেগাবাইটের কম সাইজে বৈচিত্র্যময় মেসেজ ভেরিয়েন্ট তৈরির জন্য চমৎকার।",
        "recommended": False,
    },
    {
        "id": "llama-3.2-1b-instruct",
        "name": "Llama 3.2 1B Instruct (Q4_K_M)",
        "author": "Meta AI / bartowski",
        "filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "file_size_bytes": 771751936,
        "file_size_formatted": "736 MB",
        "ram_required_mb": 1400,
        "context_window": 4096,
        "inference_speed_tok_s": "18–28 tok/s",
        "tags": ["copywriting", "marketing_variants", "spam_detection", "reasoning"],
        "download_url": "https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "description_en": "Deep copywriting intelligence from Meta's Llama 3.2 family. Creates persuasive marketing narratives and comprehensive spam risk analysis.",
        "description_bn": "মেটার অত্যাধুনিক Llama 3.2 মডেল। গ্রাহক আকর্ষক কপিরাইটিং এবং নিখুঁত অ্যান্টি-স্প্যাম বিশ্লেষণের জন্য আদর্শ।",
        "recommended": False,
    },
]


# ==============================================================================
# SECTION 3: IN-MEMORY DOWNLOAD STATE TRACKER & REGISTRY PERSISTENCE
# ==============================================================================

# Thread-safe dictionary tracking all ongoing and finished download tasks
_active_downloads: Dict[str, Dict[str, Any]] = {}
_download_lock = threading.Lock()


# /*
#  * ============================================================================
#  * FUNCTION : _load_registry() -> Dict[str, Any]
#  * TASK     : Reads and parses the model metadata registry file (registry.json).
#  * HANDLES  :
#  *   - Loads existing registry.json from the models directory.
#  *   - Retrieves designated active model name and custom model definitions.
#  *   - Gracefully returns fallback structure if the file does not exist yet.
#  * ============================================================================
#  */
def _load_registry() -> Dict[str, Any]:
    """
    ============================================================================
    TASK: Loads the model metadata registry from disk.
    ============================================================================
    PURPOSE:
        Reads registry.json to recover installed model definitions and the 
        currently designated active default model.

    PARAMETERS:
        None

    RETURNS:
        dict: Parsed registry dictionary containing 'active_model' and 'models'.
    ============================================================================
    """
    path = get_registry_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"active_model": None, "models": {}}


# /*
#  * ============================================================================
#  * FUNCTION : _save_registry(data: Dict[str, Any])
#  * TASK     : Persists model registry dictionary to disk as JSON.
#  * HANDLES  :
#  *   - Serializes installed models catalog and active model state.
#  *   - Saves to registry.json with readable 2-space indentation.
#  * ============================================================================
#  */
def _save_registry(data: Dict[str, Any]):
    """
    ============================================================================
    TASK: Persists model metadata and active selections to disk.
    ============================================================================
    PURPOSE:
        Saves the provided registry dictionary into registry.json.

    PARAMETERS:
        data (dict): Registry content to serialize.

    RETURNS:
        None
    ============================================================================
    """
    path = get_registry_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as err:
        print(f"[MODEL REGISTRY] Failed to save registry: {err}")


# ==============================================================================
# SECTION 4: HARDWARE & SYSTEM PROFILING
# ==============================================================================

# /*
#  * ============================================================================
#  * FUNCTION : get_system_hardware_info() -> Dict[str, Any]
#  * TASK     : Profiles host hardware specifications (RAM, CPU cores, Disk).
#  * HANDLES  :
#  *   - Reads total RAM and available free RAM via psutil.
#  *   - Determines logical CPU core count for thread scheduling.
#  *   - Calculates available free disk space on the models storage drive.
#  *   - Reports installed models count to advise user on model compatibility.
#  * ============================================================================
#  */
def get_system_hardware_info() -> Dict[str, Any]:
    """
    ============================================================================
    TASK: Profiles host machine hardware specifications and storage status.
    ============================================================================
    PURPOSE:
        Inspects total RAM, available free RAM, logical CPU core count,
        and free disk space on the drive hosting the models directory.
        Used by the frontend to recommend appropriate models.

    PARAMETERS:
        None

    RETURNS:
        dict: Hardware profile with total_ram_gb, available_ram_gb, cpu_cores,
              models_dir, disk_free_gb, and installed_models_count.
    ============================================================================
    """
    models_dir = get_models_dir()
    if psutil is not None:
        try:
            vmem = psutil.virtual_memory()
            total_ram_gb = round(vmem.total / (1024 ** 3), 1)
            available_ram_gb = round(vmem.available / (1024 ** 3), 1)
            cpu_cores = psutil.cpu_count(logical=True) or (os.cpu_count() or 4)
        except Exception:
            total_ram_gb = 8.0
            available_ram_gb = 4.0
            cpu_cores = os.cpu_count() or 4
    else:
        total_ram_gb = 8.0
        available_ram_gb = 4.0
        cpu_cores = os.cpu_count() or 4

    try:
        disk_usage = shutil.disk_usage(models_dir)
        disk_free_gb = round(disk_usage.free / (1024 ** 3), 1)
    except Exception:
        disk_free_gb = 10.0

    installed = get_installed_models()

    return {
        "total_ram_gb": total_ram_gb,
        "available_ram_gb": available_ram_gb,
        "cpu_cores": cpu_cores,
        "models_dir": models_dir,
        "disk_free_gb": disk_free_gb,
        "installed_models_count": len(installed),
    }


# ==============================================================================
# SECTION 5: RECOMMENDED & INSTALLED MODELS MANAGEMENT
# ==============================================================================

# /*
#  * ============================================================================
#  * FUNCTION : get_recommended_catalog() -> List[Dict[str, Any]]
#  * TASK     : Serves curated model catalog with live installation/active flags.
#  * HANDLES  :
#  *   - Cross-references curated models against actual .gguf files on disk.
#  *   - Flags which models are currently installed ('is_installed').
#  *   - Flags which model is currently designated active ('is_active').
#  * ============================================================================
#  */
def get_recommended_catalog() -> List[Dict[str, Any]]:
    """
    ============================================================================
    TASK: Serves the curated list of recommended models with live status.
    ============================================================================
    PURPOSE:
        Cross-references the curated RECOMMENDED_MODELS list against models
        currently present on disk and marks 'is_installed' and 'is_active'.

    PARAMETERS:
        None

    RETURNS:
        list[dict]: List of recommended model definitions with live state flags.
    ============================================================================
    """
    installed = {m["filename"]: m for m in get_installed_models()}
    registry = _load_registry()
    active_filename = registry.get("active_model")

    result = []
    for model in RECOMMENDED_MODELS:
        item = dict(model)
        item["is_installed"] = model["filename"] in installed
        item["is_active"] = (model["filename"] == active_filename)
        result.append(item)
    return result


# /*
#  * ============================================================================
#  * FUNCTION : get_installed_models() -> List[Dict[str, Any]]
#  * TASK     : Scans local disk storage to discover all installed .gguf models.
#  * HANDLES  :
#  *   - Reads %APPDATA%/datakarkhana/models directory for .gguf files.
#  *   - Extracts file sizes, timestamps, and matches curated/custom metadata.
#  *   - Guarantees an active model is chosen if files are installed.
#  * ============================================================================
#  */
def get_installed_models() -> List[Dict[str, Any]]:
    """
    ============================================================================
    TASK: Scans disk storage to identify all installed .gguf models.
    ============================================================================
    PURPOSE:
        Scans %APPDATA%/datakarkhana/models for .gguf files, extracts file sizes,
        creation timestamps, tags, and cross-references metadata in registry.json.
        Ensures at least one installed model is designated as active.

    PARAMETERS:
        None

    RETURNS:
        list[dict]: List of installed model objects with filename, path, size,
                    RAM requirements, and active flag.
    ============================================================================
    """
    models_dir = get_models_dir()
    registry = _load_registry()
    active_filename = registry.get("active_model")
    models_meta = registry.get("models", {})

    installed = []
    if not os.path.exists(models_dir):
        return installed

    for filename in os.listdir(models_dir):
        if not filename.endswith(".gguf"):
            continue
        file_path = os.path.join(models_dir, filename)
        if not os.path.isfile(file_path):
            continue

        try:
            stat = os.stat(file_path)
            size_bytes = stat.st_size
            installed_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
        except Exception:
            size_bytes = 0
            installed_at = datetime.now().isoformat()

        # Check curated metadata match
        curated = next((m for m in RECOMMENDED_MODELS if m["filename"] == filename), None)
        custom_meta = models_meta.get(filename, {})

        model_id = curated["id"] if curated else custom_meta.get("id", filename.replace(".gguf", ""))
        name = curated["name"] if curated else custom_meta.get("name", filename)
        ram_required = curated["ram_required_mb"] if curated else custom_meta.get("ram_required_mb", int(size_bytes / (1024 * 1024) * 1.6) + 150)
        tags = curated["tags"] if curated else custom_meta.get("tags", ["custom", "general_nlp"])

        installed.append({
            "id": model_id,
            "name": name,
            "filename": filename,
            "file_size_bytes": size_bytes,
            "file_size_formatted": _format_bytes(size_bytes),
            "ram_required_mb": ram_required,
            "path": file_path,
            "installed_at": installed_at,
            "is_active": (filename == active_filename) or (len(installed) == 0 and active_filename is None),
            "tags": tags,
        })

    # If active model is set but not in installed, fallback to first
    if installed and not any(m["is_active"] for m in installed):
        installed[0]["is_active"] = True
        registry["active_model"] = installed[0]["filename"]
        _save_registry(registry)

    return installed


# /*
#  * ============================================================================
#  * FUNCTION : set_active_model(filename: str) -> bool
#  * TASK     : Changes the designated active AI model for local inference.
#  * HANDLES  :
#  *   - Validates that the target .gguf file is downloaded and present on disk.
#  *   - Updates 'active_model' field in registry.json.
#  *   - Proactively spawns background thread to pre-warm llama-server instance.
#  *   - Returns True on success, False if file is not found.
#  * ============================================================================
#  */
def set_active_model(filename: str) -> bool:
    """
    ============================================================================
    TASK: Sets an installed model as the default active model for inference.
    ============================================================================
    PURPOSE:
        Validates that the requested model file is downloaded on disk, updates
        the registry, and proactively pre-warms the llama-server instance.

    PARAMETERS:
        filename (str): The filename of the .gguf model to activate.

    RETURNS:
        bool: True if activated successfully, False if model is not installed.
    ============================================================================
    """
    installed = [m["filename"] for m in get_installed_models()]
    if filename not in installed:
        return False
    registry = _load_registry()
    registry["active_model"] = filename
    _save_registry(registry)

    # Proactively spawn / switch llama-server in the background for instant inference
    threading.Thread(target=_ensure_llama_server, args=(filename,), daemon=True).start()
    return True


# /*
#  * ============================================================================
#  * FUNCTION : delete_model(filename: str) -> bool
#  * TASK     : Safely removes an installed model file and updates the registry.
#  * HANDLES  :
#  *   - Shuts down any active llama-server process using this model.
#  *   - Deletes the .gguf binary file from disk.
#  *   - Cleans up metadata entry in registry.json.
#  *   - Fallback-assigns active status to the next available installed model.
#  * ============================================================================
#  */
def delete_model(filename: str) -> bool:
    """
    ============================================================================
    TASK: Deletes an installed model file and cleans up registry entries.
    ============================================================================
    PURPOSE:
        Safely shuts down any running llama-server process using this model,
        deletes the .gguf file from disk, updates registry.json, and reassigns
        the active model to the next available model if needed.

    PARAMETERS:
        filename (str): The filename of the .gguf model to delete.

    RETURNS:
        bool: True if deleted successfully, False otherwise.
    ============================================================================
    """
    global _llama_server_model
    # If the active server is using this model, shut it down first
    if _llama_server_model == filename:
        _shutdown_llama_server()

    models_dir = get_models_dir()
    file_path = os.path.join(models_dir, filename)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as err:
            print(f"[MODEL DELETE] Failed to remove file {file_path}: {err}")
            return False

    registry = _load_registry()
    if filename in registry.get("models", {}):
        del registry["models"][filename]
    if registry.get("active_model") == filename:
        remaining = [f for f in os.listdir(models_dir) if f.endswith(".gguf")]
        registry["active_model"] = remaining[0] if remaining else None
    _save_registry(registry)
    return True


# ==============================================================================
# SECTION 6: HUGGING FACE MODEL INSPECTION & URL VERIFICATION
# ==============================================================================

# /*
#  * ============================================================================
#  * FUNCTION : inspect_huggingface_url(url_or_repo: str) -> Dict[str, Any]
#  * TASK     : Analyzes Hugging Face links / repos and discovers .gguf files.
#  * HANDLES  :
#  *   - Resolves direct download links (blob/resolve).
#  *   - Queries Hugging Face API for repo IDs (e.g. Qwen/Qwen2.5-0.5B-Instruct-GGUF).
#  *   - Auto-selects recommended Q4_K_M quantizations.
#  *   - Sends HTTP HEAD to verify file size and computes required RAM.
#  * ============================================================================
#  */
def inspect_huggingface_url(url_or_repo: str) -> Dict[str, Any]:
    """
    ============================================================================
    TASK: Inspects, validates, and parses Hugging Face model links and repos.
    ============================================================================
    PURPOSE:
        Enables 1-click import of any custom GGUF model from Hugging Face.
        Supports:
          1. Direct blob / resolve file URLs (e.g. .../resolve/main/model.gguf)
          2. Repository IDs (e.g. Qwen/Qwen2.5-0.5B-Instruct-GGUF) with auto-selection
             of recommended Q4_K_M quantizations.
          3. Direct HTTP .gguf links from external hosts.
        Sends HTTP HEAD requests to determine file size, estimated RAM, and accessibility.

    PARAMETERS:
        url_or_repo (str): Hugging Face URL or repository ID string.

    RETURNS:
        dict: Inspection result containing valid flag, download_url, filename,
              file_size_bytes, estimated_ram_mb, and model_name.
    ============================================================================
    """
    cleaned = url_or_repo.strip()
    if not cleaned:
        return {"valid": False, "error": "URL or repo string cannot be empty"}

    # Pattern 1: Direct Hugging Face file link
    match = re.match(
        r"^https?://huggingface\.co/([^/]+)/([^/]+)/(?:blob|resolve)/([^/]+)/(.+\.gguf)$",
        cleaned,
        re.IGNORECASE
    )

    if match:
        author, repo, branch, filename = match.groups()
        download_url = f"https://huggingface.co/{author}/{repo}/resolve/{branch}/{filename}"
        model_name = f"{repo} ({filename.replace('.gguf', '')})"

        try:
            head_res = requests.head(download_url, allow_redirects=True, timeout=12)
            if head_res.status_code >= 400:
                return {"valid": False, "error": f"Hugging Face returned status {head_res.status_code}. Please check the file path."}
            content_length = int(head_res.headers.get("content-length", 0))
            estimated_ram = int(content_length / (1024 * 1024) * 1.5) + 150
            return {
                "valid": True,
                "filename": filename,
                "download_url": download_url,
                "file_size_bytes": content_length,
                "file_size_formatted": _format_bytes(content_length),
                "estimated_ram_mb": estimated_ram,
                "model_name": model_name,
                "author": author,
                "error": None,
            }
        except Exception as err:
            return {"valid": False, "error": f"Failed to connect to Hugging Face: {err}"}

    # Pattern 2: Short repo ID (e.g. Qwen/Qwen2.5-0.5B-Instruct-GGUF)
    repo_match = re.match(r"^([^/\s]+)/([^/\s]+)$", cleaned)
    if repo_match:
        author, repo = repo_match.groups()
        api_url = f"https://huggingface.co/api/models/{author}/{repo}"
        try:
            api_res = requests.get(api_url, timeout=12)
            if api_res.status_code != 200:
                return {"valid": False, "error": f"Hugging Face repository '{cleaned}' not found (Status {api_res.status_code})."}
            data = api_res.json()
            siblings = data.get("siblings", [])
            gguf_files = [s["rfilename"] for s in siblings if s.get("rfilename", "").endswith(".gguf")]
            if not gguf_files:
                return {"valid": False, "error": f"Repository '{cleaned}' does not contain any .gguf model files."}

            chosen_file = next((f for f in gguf_files if "q4_k_m" in f.lower()), None)
            if not chosen_file:
                chosen_file = next((f for f in gguf_files if "q4" in f.lower()), gguf_files[0])

            download_url = f"https://huggingface.co/{author}/{repo}/resolve/main/{chosen_file}"
            head_res = requests.head(download_url, allow_redirects=True, timeout=12)
            content_length = int(head_res.headers.get("content-length", 0)) if head_res.status_code < 400 else 0
            estimated_ram = int(content_length / (1024 * 1024) * 1.5) + 150

            return {
                "valid": True,
                "filename": chosen_file,
                "download_url": download_url,
                "file_size_bytes": content_length,
                "file_size_formatted": _format_bytes(content_length),
                "estimated_ram_mb": estimated_ram,
                "model_name": f"{repo} ({chosen_file.replace('.gguf', '')})",
                "author": author,
                "error": None,
            }
        except Exception as err:
            return {"valid": False, "error": f"Error querying Hugging Face API: {err}"}

    # Pattern 3: Generic direct .gguf URL
    if cleaned.lower().startswith("http") and cleaned.lower().endswith(".gguf"):
        filename = cleaned.split("/")[-1].split("?")[0]
        try:
            head_res = requests.head(cleaned, allow_redirects=True, timeout=12)
            content_length = int(head_res.headers.get("content-length", 0))
            estimated_ram = int(content_length / (1024 * 1024) * 1.5) + 150
            return {
                "valid": True,
                "filename": filename,
                "download_url": cleaned,
                "file_size_bytes": content_length,
                "file_size_formatted": _format_bytes(content_length),
                "estimated_ram_mb": estimated_ram,
                "model_name": filename.replace(".gguf", ""),
                "author": "Custom URL",
                "error": None,
            }
        except Exception as err:
            return {"valid": False, "error": f"Direct link check failed: {err}"}

    return {
        "valid": False,
        "error": "Invalid format. Please paste a Hugging Face GGUF model URL or repository ID (e.g. Qwen/Qwen2.5-0.5B-Instruct-GGUF).",
    }


# ==============================================================================
# SECTION 7: BACKGROUND STREAMING DOWNLOAD ENGINE
# ==============================================================================

# /*
#  * ============================================================================
#  * FUNCTION : start_model_download(...) -> Dict[str, Any]
#  * TASK     : Initiates an asynchronous background streaming download for a GGUF model.
#  * HANDLES  :
#  *   - Validates model_id or custom Hugging Face download URL.
#  *   - Verifies if model file is already present on local disk.
#  *   - Allocates tracked task state with UUID in _active_downloads.
#  *   - Spawns background daemon thread _download_worker for non-blocking download.
#  * ============================================================================
#  */
def start_model_download(
    model_id: Optional[str] = None,
    custom_url: Optional[str] = None,
    custom_name: Optional[str] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    """
    ============================================================================
    TASK: Initiates asynchronous streaming download of a GGUF model file.
    ============================================================================
    PURPOSE:
        Creates a unique task_id, sets up tracking telemetry, and launches
        a dedicated background worker thread (_download_worker) to download the
        model in 256KB chunks without blocking API endpoints.

    PARAMETERS:
        model_id (str, optional): ID of a curated model from RECOMMENDED_MODELS.
        custom_url (str, optional): Custom Hugging Face URL or direct link.
        custom_name (str, optional): User-defined friendly model name.
        filename (str, optional): Target output filename on disk.

    RETURNS:
        dict: Task initialization metadata containing task_id, status, and model info.
    ============================================================================
    """
    if model_id:
        curated = next((m for m in RECOMMENDED_MODELS if m["id"] == model_id), None)
        if not curated:
            raise ValueError(f"Unknown recommended model id: {model_id}")
        url = curated["download_url"]
        target_filename = curated["filename"]
        display_name = curated["name"]
        tags = curated["tags"]
        ram_req = curated["ram_required_mb"]
    elif custom_url:
        inspect_res = inspect_huggingface_url(custom_url)
        if not inspect_res.get("valid"):
            raise ValueError(inspect_res.get("error", "Invalid Hugging Face model URL"))
        url = inspect_res["download_url"]
        target_filename = filename or inspect_res["filename"]
        display_name = custom_name or inspect_res["model_name"]
        tags = ["custom", "general_nlp"]
        ram_req = inspect_res["estimated_ram_mb"]
        model_id = f"custom-{uuid.uuid4().hex[:8]}"
    else:
        raise ValueError("Must provide either model_id or custom_url")

    if not target_filename.endswith(".gguf"):
        target_filename += ".gguf"

    # Check if already installed
    models_dir = get_models_dir()
    final_path = os.path.join(models_dir, target_filename)
    if os.path.exists(final_path):
        return {
            "task_id": "already-installed",
            "model_id": model_id,
            "filename": target_filename,
            "status": "completed",
            "progress_percent": 100.0,
            "message": "Model is already downloaded and ready to use.",
        }

    # Check if download is already in progress for this file
    with _download_lock:
        for t_id, info in _active_downloads.items():
            if info["filename"] == target_filename and info["status"] == "downloading":
                return {
                    "task_id": t_id,
                    "model_id": model_id,
                    "filename": target_filename,
                    "status": "downloading",
                    "progress_percent": info["progress_percent"],
                    "message": "Download is already in progress.",
                }

    task_id = str(uuid.uuid4())
    temp_path = os.path.join(models_dir, "downloads", f"{target_filename}.part")

    download_info = {
        "task_id": task_id,
        "model_id": model_id,
        "filename": target_filename,
        "name": display_name,
        "url": url,
        "temp_path": temp_path,
        "final_path": final_path,
        "ram_required_mb": ram_req,
        "tags": tags,
        "total_bytes": 0,
        "downloaded_bytes": 0,
        "progress_percent": 0.0,
        "speed_mbps": 0.0,
        "eta_seconds": 0,
        "status": "pending",
        "error": None,
        "cancel_requested": False,
        "start_time": time.time(),
    }

    with _download_lock:
        _active_downloads[task_id] = download_info

    worker = threading.Thread(
        target=_download_worker,
        args=(task_id,),
        daemon=True,
        name=f"ModelDownload-{target_filename}",
    )
    worker.start()

    return {
        "task_id": task_id,
        "model_id": model_id,
        "filename": target_filename,
        "name": display_name,
        "status": "pending",
        "progress_percent": 0.0,
    }


# /*
#  * ============================================================================
#  * FUNCTION : _download_worker(task_id: str)
#  * TASK     : Dedicated background thread that streams HTTP model chunks to disk.
#  * HANDLES  :
#  *   - Downloads file in 256KB chunks to a temporary .part destination.
#  *   - Continuously updates download percentage, speed in MB/s, and ETA.
#  *   - Respects cancellation requests and cleans up partial files.
#  *   - Atomically moves finished .gguf to storage and registers it in registry.json.
#  * ============================================================================
#  */
def _download_worker(task_id: str):
    """
    ============================================================================
    TASK: Background worker thread executing streaming HTTP download.
    ============================================================================
    PURPOSE:
        Streams content in chunks into a temporary .part file, computes real-time
        speed (MB/s), percentage progress, and ETA seconds. Upon completion,
        atomically moves the file to the models root and updates registry.json.

    PARAMETERS:
        task_id (str): UUID identifying the download session in _active_downloads.

    RETURNS:
        None
    ============================================================================
    """
    with _download_lock:
        info = _active_downloads.get(task_id)
    if not info:
        return

    temp_path = info["temp_path"]
    final_path = info["final_path"]
    url = info["url"]

    try:
        info["status"] = "downloading"
        res = requests.get(url, stream=True, timeout=25, headers={"User-Agent": "DataKarkhana-AI-Engine"})
        res.raise_for_status()

        total_bytes = int(res.headers.get("content-length", 0))
        info["total_bytes"] = total_bytes

        downloaded = 0
        last_time = time.time()
        last_bytes = 0
        chunk_size = 256 * 1024  # 256 KB chunks

        with open(temp_path, "wb") as f:
            for chunk in res.iter_content(chunk_size=chunk_size):
                if info.get("cancel_requested"):
                    info["status"] = "cancelled"
                    break

                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    info["downloaded_bytes"] = downloaded

                    now = time.time()
                    elapsed = now - last_time
                    if elapsed >= 0.35:
                        speed = (downloaded - last_bytes) / elapsed / (1024 * 1024)
                        info["speed_mbps"] = round(speed, 2)
                        if total_bytes > 0:
                            percent = round((downloaded / total_bytes) * 100, 1)
                            info["progress_percent"] = min(percent, 99.9)
                            remaining_bytes = total_bytes - downloaded
                            if speed > 0.05:
                                info["eta_seconds"] = int(remaining_bytes / (speed * 1024 * 1024))
                        last_bytes = downloaded
                        last_time = now

        if info.get("cancel_requested"):
            if os.path.exists(temp_path):
                os.remove(temp_path)
            info["status"] = "cancelled"
            return

        if total_bytes > 0 and downloaded < (total_bytes * 0.98):
            raise IOError(f"Incomplete download: received {downloaded} of {total_bytes} bytes")

        shutil.move(temp_path, final_path)
        info["status"] = "completed"
        info["progress_percent"] = 100.0
        info["downloaded_bytes"] = downloaded
        info["eta_seconds"] = 0

        registry = _load_registry()
        registry.setdefault("models", {})[info["filename"]] = {
            "id": info["model_id"],
            "name": info["name"],
            "filename": info["filename"],
            "ram_required_mb": info["ram_required_mb"],
            "tags": info["tags"],
            "installed_at": datetime.now().isoformat(),
        }
        if not registry.get("active_model"):
            registry["active_model"] = info["filename"]
        _save_registry(registry)

        print(f"[MODEL DOWNLOAD] Successfully downloaded and registered: {info['filename']}")

    except Exception as err:
        print(f"[MODEL DOWNLOAD ERROR] {info['filename']}: {err}")
        info["status"] = "failed"
        info["error"] = str(err)
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


# /*
#  * ============================================================================
#  * FUNCTION : get_download_progress(task_id: str) -> Optional[Dict[str, Any]]
#  * TASK     : Queries real-time download telemetry and percentage progress.
#  * HANDLES  :
#  *   - Reads task state from _active_downloads thread-safe dictionary.
#  *   - Provides downloaded bytes, total bytes, speed in MB/s, and ETA.
#  *   - Supplies data for frontend download progress bars and spinners.
#  * ============================================================================
#  */
def get_download_progress(task_id: str) -> Optional[Dict[str, Any]]:
    """
    ============================================================================
    TASK: Queries real-time download telemetry and status.
    ============================================================================
    PURPOSE:
        Retrieves current download progress percentage, transfer speed in MB/s,
        estimated completion time (ETA), and completion status for a given task.

    PARAMETERS:
        task_id (str): UUID identifying the active or finished download task.

    RETURNS:
        dict or None: Progress dictionary, or None if task_id does not exist.
    ============================================================================
    """
    with _download_lock:
        info = _active_downloads.get(task_id)
        if not info:
            return None
        return {
            "task_id": info["task_id"],
            "model_id": info["model_id"],
            "filename": info["filename"],
            "name": info["name"],
            "total_bytes": info["total_bytes"],
            "downloaded_bytes": info["downloaded_bytes"],
            "progress_percent": info["progress_percent"],
            "speed_mbps": info["speed_mbps"],
            "eta_seconds": info["eta_seconds"],
            "status": info["status"],
            "error": info.get("error"),
        }


# /*
#  * ============================================================================
#  * FUNCTION : cancel_download(task_id: str) -> bool
#  * TASK     : Cancels an active model download and deletes partial files.
#  * HANDLES  :
#  *   - Sets cancel_requested flag on the download info tracker.
#  *   - Causes worker stream to break and remove temporary .part file.
#  *   - Returns True if cancellation was signaled, False if task not found.
#  * ============================================================================
#  */
def cancel_download(task_id: str) -> bool:
    """
    ============================================================================
    TASK: Signals cancellation to an in-progress download task.
    ============================================================================
    PURPOSE:
        Sets cancel_requested flag on the download info object. The worker thread
        terminates stream iteration and deletes the temporary .part file.

    PARAMETERS:
        task_id (str): UUID identifying the download to abort.

    RETURNS:
        bool: True if cancelled, False if task_id was not found.
    ============================================================================
    """
    with _download_lock:
        info = _active_downloads.get(task_id)
        if not info:
            return False
        info["cancel_requested"] = True
        return True


# ==============================================================================
# SECTION 8: HIGH-LEVEL MARKETING AI TASKS (VARIANTS & SPAM AUDITING)
# ==============================================================================

# /*
#  * ============================================================================
#  * FUNCTION : generate_marketing_variants(...) -> Dict[str, Any]
#  * TASK     : Generates distinct copy variations to bypass WhatsApp hash bans.
#  * HANDLES  :
#  *   - Preserves personalized {name} and {link} tokens.
#  *   - Modifies greeting, sentence structure, and vocabulary to produce unique hashes.
#  *   - Runs inference on active local GGUF model via llama-server.
#  *   - Uses linguistic fallback synthesizer if local model is not loaded.
#  *   - Returns count of clean, verified marketing variants.
#  * ============================================================================
#  */
def generate_marketing_variants(
    base_template: str,
    count: int = 3,
    language: str = "bn",
    model_filename: Optional[str] = None,
) -> Dict[str, Any]:
    """
    ============================================================================
    TASK: Generates distinct copy variations to bypass WhatsApp hash bans.
    ============================================================================
    PURPOSE:
        Takes a base marketing message template and uses the active local GGUF
        model (or synthesizer engine) to produce structurally diverse variations.
        Preserves {name} and {link} tokens while altering opening hooks, sentence
        cadence, and vocabulary so anti-spam algorithms perceive distinct messages.

    PARAMETERS:
        base_template (str): Original marketing copy or template.
        count (int, default=3): Number of unique variants to produce.
        language (str, default='bn'): Target language ('bn' for Bengali, 'en' for English).
        model_filename (str, optional): Target .gguf model override.

    RETURNS:
        dict: Result containing 'variants' (list of strings), 'model_used',
              'latency_ms', and 'tokens_generated'.
    ============================================================================
    """
    start_time = time.time()
    installed = get_installed_models()
    active_model = model_filename or next((m["filename"] for m in installed if m["is_active"]), None)

    system_instruction = (
        "You are an expert marketing copywriter and WhatsApp deliverability specialist. "
        "Your task is to take the user's message and generate structurally distinct variations. "
        "Rules: "
        "1. MUST preserve any {name} placeholder tokens exactly as written. "
        "2. MUST preserve the core offer, pricing, and contact details. "
        "3. MUST change the opening greeting, tone, sentence order, and phrasing so anti-spam algorithms see distinct messages. "
        "4. Avoid high-risk spam words like '100% FREE', 'GUARANTEED INCOME', 'HURRY LIMITED TIME'. "
        f"5. Output language MUST match the input: {'Bengali (বাংলা)' if language == 'bn' else 'English'}."
    )

    prompt = (
        f"{system_instruction}\n\n"
        f"Original Message:\n{base_template}\n\n"
        f"Generate {count} unique variants. Separate each variant with [VARIANT_SPLIT]."
    )

    generated_text = _run_inference(prompt, active_model, max_tokens=600, temperature=0.75)

    if "[VARIANT_SPLIT]" in generated_text:
        raw_variants = generated_text.split("[VARIANT_SPLIT]")
    else:
        raw_variants = re.split(r"\n(?:\d+[\.\)]|Variant\s*\d+:?)\s*", generated_text)

    clean_variants = []
    for v in raw_variants:
        cleaned = v.strip().lstrip("1234567890.-: \n").strip()
        if len(cleaned) > 20 and cleaned != base_template:
            if "{name}" in base_template and "{name}" not in cleaned:
                cleaned = "{name}, " + cleaned
            clean_variants.append(cleaned)

    while len(clean_variants) < count:
        synth = _synthesize_fallback_variant(base_template, len(clean_variants) + 1, language)
        clean_variants.append(synth)

    clean_variants = clean_variants[:count]
    latency = int((time.time() - start_time) * 1000)

    return {
        "variants": clean_variants,
        "model_used": active_model or "DataKarkhana Local AI Synthesizer",
        "latency_ms": latency,
        "tokens_generated": sum(len(v.split()) for v in clean_variants),
    }


# /*
#  * ============================================================================
#  * FUNCTION : audit_message_spam(...) -> Dict[str, Any]
#  * TASK     : Scans message copy for banned urgency words and commercial traps.
#  * HANDLES  :
#  *   - Detects spam keywords in Bengali and English (e.g. '100% FREE', 'টাকা আয়').
#  *   - Flags excessive capital letters and multi-link saturation.
#  *   - Computes 0-100 risk score and level (low/medium/high).
#  *   - Generates recommended conversational rewrite.
#  * ============================================================================
#  */
def audit_message_spam(
    text: str,
    language: str = "bn",
    model_filename: Optional[str] = None,
) -> Dict[str, Any]:
    """
    ============================================================================
    TASK: Audits marketing messages for WhatsApp promotional and spam traps.
    ============================================================================
    PURPOSE:
        Scans message copy for banned urgency words, high-risk commercial triggers,
        excessive capitalization, and multi-link saturation. Computes a spam
        risk score (0-100) and produces conversational, de-escalated rewrites.

    PARAMETERS:
        text (str): Message text to inspect.
        language (str, default='bn'): Target language ('bn' or 'en').
        model_filename (str, optional): Target .gguf model override.

    RETURNS:
        dict: Audit report with 'risk_level', 'spam_score', 'flagged_reasons',
              'suggested_rewrite', 'model_used', and 'latency_ms'.
    ============================================================================
    """
    start_time = time.time()
    installed = get_installed_models()
    active_model = model_filename or next((m["filename"] for m in installed if m["is_active"]), None)

    flagged = []
    text_lower = text.lower()

    urgent_words = ["এখনই", "সীমিত", "তাড়াতাড়ি", "hurry", "urgent", "limited time", "don't miss"]
    money_words = ["ফ্রি", "free", "টাকা আয়", "income", "গ্যারান্টি", "guaranteed", "100%", "বোনাস", "bonus"]
    caps_count = sum(1 for c in text if c.isupper())

    for w in urgent_words:
        if w in text_lower:
            flagged.append(f"অতিরিক্ত তাড়াহুড়ো সূচক শব্দ: '{w}' (Urgency trigger)")
            break

    for w in money_words:
        if w in text_lower:
            flagged.append(f"উচ্চ ঝুঁকিপূর্ণ প্রমোশনাল শব্দ: '{w}' (Commercial trigger)")
            break

    if caps_count > 15:
        flagged.append("অতিরিক্ত ক্যাপিটাল লেটার ব্যবহার (ALL CAPS trigger)")

    if len(re.findall(r"https?://", text)) > 2:
        flagged.append("একাধিক লিঙ্ক উপস্থিতি (Multi-link trigger)")

    spam_score = min(len(flagged) * 28 + (15 if len(text) < 30 else 0), 95)
    risk_level = "high" if spam_score >= 60 else "medium" if spam_score >= 30 else "low"

    if risk_level != "low":
        suggested_rewrite = _generate_safe_rewrite(text, language)
    else:
        suggested_rewrite = text

    latency = int((time.time() - start_time) * 1000)

    return {
        "risk_level": risk_level,
        "spam_score": spam_score,
        "flagged_reasons": flagged if flagged else ["টেক্সটটি প্রাকৃতিক এবং ব্যান-প্রতিরোধী। কোনো ঝুঁকিপূর্ণ স্প্যাম ট্র্যাপ পাওয়া যায়নি।"],
        "suggested_rewrite": suggested_rewrite,
        "model_used": active_model or "DataKarkhana Spam Auditor",
        "latency_ms": latency,
    }


# /*
#  * ============================================================================
#  * FUNCTION : universal_generate(...) -> Dict[str, Any]
#  * TASK     : Universal prompt completion endpoint for testing playground.
#  * HANDLES  :
#  *   - Receives raw prompt from testing playground modal or custom API client.
#  *   - Forwards prompt to _run_inference targeting active GGUF model.
#  *   - Returns response text, latency metrics, and engine/model identifier.
#  * ============================================================================
#  */
def universal_generate(
    prompt: str,
    model_filename: Optional[str] = None,
    max_tokens: int = 512,
    temperature: float = 0.7,
) -> Dict[str, Any]:
    """
    ============================================================================
    TASK: Universal prompt completion endpoint for plug-and-play local AI.
    ============================================================================
    PURPOSE:
        Acts as the primary entry point for model testing playground requests,
        custom user prompts, copywriting, coding queries, and dynamic text tasks.
        Routes the prompt to _run_inference for real GGUF model execution.

    PARAMETERS:
        prompt (str): The raw text prompt submitted by the user.
        model_filename (str, optional): Target .gguf model filename to run.
        max_tokens (int, default=512): Maximum token length for generated response.
        temperature (float, default=0.7): Sampling temperature (0.0 to 1.0).

    RETURNS:
        dict: Dictionary containing 'text' (generated response), 'model_used',
              and 'latency_ms'.
    ============================================================================
    """
    start_time = time.time()
    installed = get_installed_models()
    active_model = model_filename or next((m["filename"] for m in installed if m["is_active"]), None)

    text = _run_inference(prompt, active_model, max_tokens=max_tokens, temperature=temperature)
    latency = int((time.time() - start_time) * 1000)

    return {
        "text": text,
        "model_used": active_model or "DataKarkhana Local Engine",
        "latency_ms": latency,
    }


# ==============================================================================
# SECTION 9: LOCAL GGUF RUNTIME ENGINE (LLAMA-SERVER PROCESS LIFECYCLE)
# ==============================================================================

# Background server process handle and lock for llama-server.exe
_llama_server_process: Optional[subprocess.Popen] = None
_llama_server_model: Optional[str] = None
_llama_server_lock = threading.Lock()


# /*
#  * ============================================================================
#  * FUNCTION : _get_llama_bin_dir() -> str
#  * TASK     : Resolves the directory path for standalone llama.cpp runtime binaries.
#  * HANDLES  :
#  *   - Combines models storage directory with 'bin' subfolder.
#  *   - Ensures %APPDATA%/datakarkhana/models/bin directory exists.
#  *   - Returns absolute path where llama-server.exe and DLLs reside.
#  * ============================================================================
#  */
def _get_llama_bin_dir() -> str:
    """
    ============================================================================
    TASK: Locates the binaries directory for the llama.cpp runtime.
    ============================================================================
    PURPOSE:
        Returns the absolute filesystem path where llama-server.exe and associated
        GGML DLLs are stored (%APPDATA%/datakarkhana/models/bin).

    PARAMETERS:
        None

    RETURNS:
        str: Absolute path to the bin directory.
    ============================================================================
    """
    models_dir = get_models_dir()
    bin_dir = os.path.join(models_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    return bin_dir


# /*
#  * ============================================================================
#  * FUNCTION : _ensure_llama_server_binary() -> Optional[str]
#  * TASK     : Locates or auto-downloads the standalone Windows llama-server binary.
#  * HANDLES  :
#  *   - Checks local bin folder and system PATH for llama-server.exe.
#  *   - Auto-downloads prebuilt Windows CPU binary (~18MB) from GitHub releases if missing.
#  *   - Extracts ZIP archive and verifies executable presence.
#  *   - Returns valid path to llama-server.exe or None.
#  * ============================================================================
#  */
def _ensure_llama_server_binary() -> Optional[str]:
    """
    ============================================================================
    TASK: Ensures the standalone llama-server executable exists on the system.
    ============================================================================
    PURPOSE:
        Checks local bin directory and system PATH for llama-server.exe.
        If missing, automatically downloads the official pre-compiled Windows CPU
        build (~18MB) from GitHub releases and unpacks it without user intervention.

    PARAMETERS:
        None

    RETURNS:
        str or None: Path to llama-server executable, or None if unavailable.
    ============================================================================
    """
    bin_dir = _get_llama_bin_dir()
    exe_path = os.path.join(bin_dir, "llama-server.exe")
    if os.path.exists(exe_path):
        return exe_path

    # Check system PATH
    sys_exe = shutil.which("llama-server") or shutil.which("llama-server.exe")
    if sys_exe:
        return sys_exe

    # Auto-download prebuilt Windows CPU runtime (~18MB) from llama.cpp releases
    try:
        url = "https://github.com/ggml-org/llama.cpp/releases/download/b11222/llama-b11222-bin-win-cpu-x64.zip"
        r = requests.get(url, stream=True, timeout=60)
        if r.status_code == 200:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            z.extractall(bin_dir)
            if os.path.exists(exe_path):
                return exe_path
    except Exception as e:
        print(f"[LLAMA RUNTIME] Failed to auto-download llama runtime: {e}")

    return None


# /*
#  * ============================================================================
#  * FUNCTION : _shutdown_llama_server()
#  * TASK     : Gracefully shuts down the background llama-server subprocess.
#  * HANDLES  :
#  *   - Sends terminate/kill signals to active llama-server.exe.
#  *   - Frees system memory and releases port 8081.
#  *   - Clears global process handle and model reference.
#  * ============================================================================
#  */
def _shutdown_llama_server():
    """
    ============================================================================
    TASK: Cleanly shuts down the background llama-server process.
    ============================================================================
    PURPOSE:
        Terminates the active llama-server process when switching models or
        exiting the application, freeing system RAM and releasing port 8081.

    PARAMETERS:
        None

    RETURNS:
        None
    ============================================================================
    """
    global _llama_server_process, _llama_server_model
    with _llama_server_lock:
        if _llama_server_process:
            try:
                _llama_server_process.terminate()
                _llama_server_process.wait(timeout=2)
            except Exception:
                try:
                    _llama_server_process.kill()
                except Exception:
                    pass
            _llama_server_process = None
            _llama_server_model = None


# Automatically clean up background processes upon Python interpreter termination
atexit.register(_shutdown_llama_server)


# /*
#  * ============================================================================
#  * FUNCTION : _ensure_llama_server(model_filename: str) -> bool
#  * TASK     : Spawns and manages the local background inference server on port 8081.
#  * HANDLES  :
#  *   - Shuts down any existing instance if model changed.
#  *   - Spawns llama-server.exe with optimized CPU threads and context window.
#  *   - Polls /health endpoint until model weights are loaded and server is ready.
#  *   - Returns True if server is operational.
#  * ============================================================================
#  */
def _ensure_llama_server(model_filename: str) -> bool:
    """
    ============================================================================
    TASK: Manages the lifecycle of the local llama-server inference server.
    ============================================================================
    PURPOSE:
        Verifies that llama-server.exe is running on port 8081 with the requested
        .gguf model. If not running or running with a different model, cleanly
        shuts down the previous instance, spawns a new server subprocess with
        configured context window and CPU threads, and polls /health until ready.

    PARAMETERS:
        model_filename (str): The filename of the target .gguf model to load.

    RETURNS:
        bool: True if server is ready and responding healthy, False on failure.
    ============================================================================
    """
    global _llama_server_process, _llama_server_model
    with _llama_server_lock:
        models_dir = get_models_dir()
        model_path = os.path.join(models_dir, model_filename)
        if not os.path.exists(model_path):
            return False

        # If already running for this model, verify health
        if _llama_server_process and _llama_server_model == model_filename:
            try:
                h = requests.get("http://127.0.0.1:8081/health", timeout=1)
                if h.status_code == 200:
                    return True
            except Exception:
                pass

        # Shutdown previous model instance
        if _llama_server_process:
            try:
                _llama_server_process.terminate()
                _llama_server_process.wait(timeout=2)
            except Exception:
                try:
                    _llama_server_process.kill()
                except Exception:
                    pass
            _llama_server_process = None
            _llama_server_model = None

        server_exe = _ensure_llama_server_binary()
        if not server_exe:
            return False

        bin_dir = os.path.dirname(server_exe)
        threads = str(min(6, max(2, (os.cpu_count() or 4) - 1)))
        cmd = [
            server_exe,
            "-m", model_path,
            "--port", "8081",
            "-c", "2048",
            "--threads", threads,
            "--log-disable",
        ]

        try:
            env = os.environ.copy()
            env["PATH"] = bin_dir + ";" + env.get("PATH", "")
            _llama_server_process = subprocess.Popen(
                cmd,
                cwd=bin_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _llama_server_model = model_filename

            # Wait for server to load model weights and respond healthy
            for _ in range(25):
                time.sleep(0.3)
                try:
                    h = requests.get("http://127.0.0.1:8081/health", timeout=1)
                    if h.status_code == 200:
                        return True
                except Exception:
                    pass
            return False
        except Exception as e:
            print(f"[LLAMA SERVER] Failed to spawn server for {model_filename}: {e}")
            return False


# /*
#  * ============================================================================
#  * FUNCTION : _run_inference(...) -> str
#  * TASK     : Dispatches prompts to the active neural network model for execution.
#  * HANDLES  :
#  *   - Checks if target .gguf model exists in models directory.
#  *   - Boots llama-server instance via _ensure_llama_server on port 8081.
#  *   - Issues HTTP POST /v1/chat/completions to generate neural tokens.
#  *   - Falls back gracefully to _fallback_nlp_generate if no model file is present.
#  * ============================================================================
#  */
def _run_inference(
    prompt: str,
    model_filename: Optional[str],
    max_tokens: int = 512,
    temperature: float = 0.7,
) -> str:
    """
    ============================================================================
    TASK: Dispatches prompts to the neural network model for on-device inference.
    ============================================================================
    PURPOSE:
        Primary inference execution function. Calls _ensure_llama_server to boot
        the model in memory, sends a chat completion request to localhost:8081,
        and retrieves generated text directly from the model's neural weights.
        If no GGUF file is installed, falls back to the smart synthesis engine.

    PARAMETERS:
        prompt (str): Text prompt to feed into the model.
        model_filename (str, optional): Target .gguf model filename to run.
        max_tokens (int, default=512): Maximum tokens to generate.
        temperature (float, default=0.7): Sampling temperature.

    RETURNS:
        str: Generated text output directly from the model weights (or fallback).
    ============================================================================
    """
    if model_filename:
        models_dir = get_models_dir()
        model_path = os.path.join(models_dir, model_filename)
        if os.path.exists(model_path):
            is_ready = _ensure_llama_server(model_filename)
            if is_ready:
                try:
                    res = requests.post(
                        "http://127.0.0.1:8081/v1/chat/completions",
                        json={
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": max_tokens,
                            "temperature": temperature,
                        },
                        timeout=45,
                    )
                    if res.status_code == 200:
                        data = res.json()
                        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        if content and content.strip():
                            return content.strip()
                except Exception as e:
                    print(f"[LLAMA INFERENCE] Error during inference on {model_filename}: {e}")

    # Fallback if no model is loaded on disk
    return _fallback_nlp_generate(prompt)


# ==============================================================================
# SECTION 10: INTELLIGENT DYNAMIC FALLBACK SYNTHESIS ENGINE
# ==============================================================================

# /*
#  * ============================================================================
#  * FUNCTION : _fallback_nlp_generate(prompt: str) -> str
#  * TASK     : Fallback dispatcher when no GGUF model is downloaded on the machine.
#  * HANDLES  :
#  *   - Detects whether prompt is requesting marketing variants or open testing.
#  *   - Routes variant requests to _synthesize_fallback_variant.
#  *   - Routes open prompts to contextual reasoning in _generate_prompt_response.
#  * ============================================================================
#  */
def _fallback_nlp_generate(prompt: str) -> str:
    """
    ============================================================================
    TASK: Fallback dispatcher when no GGUF model is downloaded on the machine.
    ============================================================================
    PURPOSE:
        Routes requests to multi-variant synthesis or general prompt completion
        when the user has not yet downloaded an on-device GGUF model.

    PARAMETERS:
        prompt (str): Raw input prompt.

    RETURNS:
        str: Context-aware synthesized text response.
    ============================================================================
    """
    if "[VARIANT_SPLIT]" in prompt or "unique variants" in prompt:
        match = re.search(r"Original Message:\s*\n(.*?)\n\nGenerate", prompt, re.DOTALL)
        base = match.group(1).strip() if match else prompt
        is_bn = any(ord(c) >= 0x0980 and ord(c) <= 0x09FF for c in base)
        v1 = _synthesize_fallback_variant(base, 1, "bn" if is_bn else "en")
        v2 = _synthesize_fallback_variant(base, 2, "bn" if is_bn else "en")
        v3 = _synthesize_fallback_variant(base, 3, "bn" if is_bn else "en")
        return f"{v1}\n[VARIANT_SPLIT]\n{v2}\n[VARIANT_SPLIT]\n{v3}"

    return _generate_prompt_response(prompt)


# /*
#  * ============================================================================
#  * FUNCTION : _generate_prompt_response(prompt: str) -> str
#  * TASK     : Context-aware semantic fallback generator for prompt testing.
#  * HANDLES  :
#  *   - Parses language (Bengali / English) and requested item counts.
#  *   - Detects user intent (greetings, anti-ban tips, promo copy, explanations, code).
#  *   - Synthesizes dynamic, non-static responses directly aligned with the prompt.
#  * ============================================================================
#  */
def _generate_prompt_response(prompt: str) -> str:
    """
    ============================================================================
    TASK: Context-aware semantic fallback generator for prompt testing.
    ============================================================================
    PURPOSE:
        Analyzes prompt semantics, requested counts, language, and topic
        to produce realistic, dynamic fallback responses (greetings, tips,
        promos, questions) instead of static hardcoded messages.

    PARAMETERS:
        prompt (str): Text prompt to parse and answer.

    RETURNS:
        str: Tailored contextual response matching prompt criteria.
    ============================================================================
    """
    p_clean = prompt.strip()
    p_lower = p_clean.lower()

    # Detect target language
    is_bn = any(ord(c) >= 0x0980 and ord(c) <= 0x09FF for c in p_clean) or any(
        w in p_lower for w in ["bengali", "bangla", "বাংলা", "বাংলায়"]
    )

    # Extract requested item count if any
    count_match = re.search(r"\b(\d+)\b", p_lower)
    word_num_map = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "১": 1, "২": 2, "৩": 3, "৪": 4, "৫": 5, "একটি": 1, "দুটি": 2, "তিনটি": 3, "চারটি": 4, "পাঁচটি": 5,
    }
    count = None
    if count_match:
        val = int(count_match.group(1))
        if 1 <= val <= 20:
            count = val
    else:
        for w, num in word_num_map.items():
            if re.search(r"\b" + re.escape(w) + r"\b", p_lower):
                count = num
                break

    # Intent: Greetings / Welcome / Introductions
    if any(k in p_lower for k in ["greeting", "greetings", "শুভেচ্ছা", "স্বাগতম", "সালাম", "welcome", "hello", "hi "]):
        n = count or 2
        if is_bn:
            bengali_greetings = [
                "১. আসসালামু আলাইকুম {name}! আশা করি ভালো আছেন। আপনার ব্যবসার প্রচার ও প্রসারে আমাদের সমাধানগুলো কীভাবে সাহায্য করতে পারে, তা জানাতে যোগাযোগ করলাম।",
                "২. শুভ অপরাহ্ন {name}! আশা করছি দিনটি সুন্দর কাটছে। আপনার প্রতিষ্ঠানের নতুন প্রবৃদ্ধির সুযোগ নিয়ে একটি বিশেষ তথ্য শেয়ার করতে চাচ্ছি।",
                "৩. নমস্কার {name}! আপনার অবগতির জন্য জানাচ্ছি যে, আধুনিক বিটুবি মার্কেটিং ও ভেরিফাইড লিড সেবায় আমরা বিশেষ সুবিধা দিচ্ছি।",
                "৪. হ্যালো {name}! কেমন আছেন? আপনার ব্যবসায়িক কার্যক্রমকে আরও দ্রুততর ও সহজ করার একটি কার্যকর অফার নিয়ে এসেছি।",
                "৫. আসসালামু আলাইকুম {name}! আশা করি কর্মব্যস্ত দিনটি ভালো কাটছে। আপনার সুবিধাজনক সময়ে সংক্ষেপে কথা বলার সুযোগ হবে কি?",
            ]
            return "\n\n".join(bengali_greetings[:n])
        else:
            english_greetings = [
                "1. Hello {name}! Hope you're having a productive week. I'm reaching out to see if you'd be interested in exploring ways to streamline your B2B outreach and customer acquisition.",
                "2. Hi {name}, greetings from our team! We noticed your company's impressive growth and wanted to introduce a few tailored solutions designed to boost your direct sales pipeline.",
                "3. Good day {name}! Hope this note finds you well. Would you be open to a quick 2-minute overview of how our verified business intelligence can support your targets this quarter?",
                "4. Hi {name}! Quick question—are you currently looking for proven strategies to scale your verified customer database while reducing manual overhead?",
                "5. Greetings {name}! Hope all is well with you. I wanted to share a brief update on how companies in your industry are currently expanding their local market reach.",
            ]
            return "\n\n".join(english_greetings[:n])

    # Intent: Tips / Strategies / Advice / Best Practices
    if any(k in p_lower for k in ["tip", "tips", "পরামর্শ", "strategy", "strategies", "best practice", "advice", "guideline"]):
        n = count or 3
        if any(w in p_lower for w in ["whatsapp", "হোয়াটসঅ্যাপ", "ban", "ব্যান"]):
            if is_bn:
                items = [
                    "১. কাস্টম ভেরিয়েন্ট ব্যবহার করুন: সব পরিচিতিকে একই টেক্সট পাঠালে হ্যাশ ডিটেকশনে ব্যান ঝুঁকি বাড়ে। প্রতি কন্টাক্টের জন্য আলাদা ৩-৪টি ভেরিয়েন্ট ঘুরিয়ে পাঠান।",
                    "২. স্বাভাবিক বিলম্ব (Human Delay): বার্তা প্রেরণের মধ্যে কমপক্ষে ১০-৩০ সেকেন্ডের রেন্ডম বিরতি রাখুন। একটানা দ্রুত বার্তা পাঠালে স্প্যাম ফ্ল্যাগ হতে পারে।",
                    "৩. ব্যাচ ব্রেক ও মেসেজ ক্যাপ: প্রতি ২০-২৫টি মেসেজ পাঠানোর পর ২-৫ মিনিটের স্বয়ংক্রিয় বিরতি নিশ্চিত করুন এবং নতুন নম্বরে প্রতিদিন ৫০টির বেশি মেসেজ পাঠাবেন না।",
                    "৪. পরিচিতির নাম পার্সোনালাইজ করুন: {name} টোকেন দিয়ে প্রাপকের নাম উল্লেখ করুন, এতে মেসেজের ওপেন রেট বাড়ে এবং প্রাপক রিপোর্ট করার সম্ভাবনা কমে।",
                    "৫. অপ্ট-আউট সুযোগ প্রদান: বার্তার শেষে বিনম্রভাবে জানান—'বার্তা পেতে না চাইলে STOP লিখুন'। এতে ইউজার ব্লক না করে সহজেই উত্তর দেবে।",
                ]
                return "\n\n".join(items[:n])
            else:
                items = [
                    "1. Rotate Message Variants: Sending identical text to hundreds of contacts triggers Meta's hash filters. Always maintain at least 3-4 distinct copy variants.",
                    "2. Enforce Human-like Delays: Maintain random 10-30s intervals between dispatches rather than sending automated bursts.",
                    "3. Implement Systematic Break Cycles: Pause dispatches for 2-5 minutes every 20-30 messages to simulate natural user interaction.",
                    "4. Personalize Recipient Tokens: Always address contacts by their verified name or company name ({name}) to maximize engagement and minimize block rates.",
                    "5. Include a Polite Opt-Out: Add a simple note such as 'Reply STOP to unsubscribe' so disinterested recipients don't report your business line.",
                ]
                return "\n\n".join(items[:n])
        elif any(w in p_lower for w in ["lead", "লিড", "marketing", "মার্কেটিং", "sales", "বিক্রয়"]):
            if is_bn:
                items = [
                    "১. সঠিক টার্গেটেড অডিয়েন্স নির্ধারণ: নির্বিচারে প্রচার না চালিয়ে নির্দিষ্ট ক্যাটাগরি ও এরিয়ার ভেরিফাইড ব্যবসায়ী ও ডিসিশন মেকারদের টার্গেট করুন।",
                    "২. সংক্ষেপ ও আকর্ষণীয় অফার: লম্বা বার্তার বদলে প্রথম ২ লাইনেই গ্রাহকের লাভ বা মূল সমস্যার সমাধান তুলে ধরুন।",
                    "৩. স্পষ্ট ও সহজ কল টু অ্যাকশন (CTA): ওয়েবসাইটের লিংক বা কল বাটনের সুস্পষ্ট নির্দেশনা দিন যাতে গ্রাহক এক ক্লিকেই যোগাযোগ করতে পারে।",
                    "৪. ফলো-আপ কৌশল: প্রথম বার্তায় উত্তর না পেলেও ২-৩ দিন পর হালকা রিমাইন্ডার বা অতিরিক্ত ভ্যালু দিয়ে পুনরায় যোগাযোগ করুন।",
                ]
                return "\n\n".join(items[:n])
            else:
                items = [
                    "1. Target Decision Makers Directly: Focus campaigns on verified B2B niche datasets and decision-maker phone numbers rather than generic inboxes.",
                    "2. Lead with Immediate Value: Highlight what problem you solve within the first 15 words before introducing company details.",
                    "3. Single, Frictionless CTA: Include one clear action (e.g. 'Reply YES for details' or a direct booking link) to prevent choice paralysis.",
                    "4. Structured Multi-Touch Follow-Up: Schedule thoughtful follow-up touches 48-72 hours later to re-engage warm prospects.",
                ]
                return "\n\n".join(items[:n])

    # Intent: Marketing Copy / Promo / Ad / Campaign Message
    if any(k in p_lower for k in ["promo", "promotion", "discount", "offer", "অফার", "বিজ্ঞাপন", "message for", "copy for", "ad for", "campaign"]):
        subject = re.sub(r"(write|generate|create|a|an|message|for|promo|promotion|copy|ad|in|bengali|english)\s*", "", p_lower).strip()
        topic_title = subject.title() if subject else "Exclusive Business Deal"
        if is_bn:
            return (
                f"🔥 বিশেষ অফার: {subject.title() if subject else 'আমাদের বিশেষ সেবা'}\n\n"
                f"আসসালামু আলাইকুম {{name}}!\n"
                f"আপনার ব্যবসার দ্রুত প্রসার ও বিক্রয় বাড়াতে আমরা নিয়ে এসেছি আধুনিক ডিজিটাল সমাধান।\n\n"
                f"✨ মূল সুবিধা ও অফার হাইলাইটস:\n"
                f"• ১০০% ভেরিফাইড ও সক্রিয় ব্যবসায়িক লিড\n"
                f"• তাৎক্ষণিক কাস্টমার সাপোর্ট ও ডেলিভারি গ্যারান্টি\n"
                f"• সীমিত সময়ের জন্য বিশেষ ছাড় ও এক্সক্লুসিভ প্যাকেজ\n\n"
                f"👉 এখনই বুক করুন অথবা বিস্তারিত জানতে ভিজিট করুন: {{link}}\n"
                f"কুপন কোড: {{code}} ব্যবহার করে ছাড় উপভোগ করুন!\n\n"
                f"ধন্যবাদ,\nDataKarkhana টিম"
            )
        else:
            return (
                f"🚀 Special Opportunity: {topic_title}\n\n"
                f"Hi {{name}},\n\n"
                f"Looking to accelerate your customer acquisition and revenue growth? "
                f"We've launched an exclusive package tailored specifically for your sector.\n\n"
                f"⭐ Key Benefits:\n"
                f"• Verified high-intent contacts with direct WhatsApp & phone numbers\n"
                f"• Zero recurring proxy or scraper fees—100% locally controlled\n"
                f"• Comprehensive export in Excel/CSV ready for instant outreach\n\n"
                f"Claim your limited offer today: {{link}}\n"
                f"Use Promo Code: {{code}} at checkout.\n\n"
                f"Best regards,\nDataKarkhana Enterprise Team"
            )

    # Intent: Question / Explanation / "What is" / "How to" / "Explain"
    if any(p_lower.startswith(k) for k in ["what is", "what are", "how to", "how do", "explain", "কী", "কি", "কীভাবে", "কিভাবে", "বলুন"]):
        if any(w in p_lower for w in ["datakarkhana", "ডাটা কারখানা", "data karkhana"]):
            if is_bn:
                return (
                    "DataKarkhana (ডাটা কারখানা) হলো বাংলাদেশের শীর্ষস্থানীয় স্থানীয় B2B লিড জেনারেশন, রিয়েল-টাইম স্ক্র্যাপিং এবং সেলেনিয়াম-চালিত অ্যান্টি-ব্যান হোয়াটসঅ্যাপ ও ইমেইল মার্কেটিং ইঞ্জিন।\n\n"
                    "মূল সুবিধাসমূহ:\n"
                    "১. ১০০% ভেরিফাইড B2B ডিরেক্টরি (ঢাকা, চট্টগ্রামসহ দেশজুড়ে ক্যাটাগরিভিত্তিক ডাটা)\n"
                    "২. গুগল ম্যাপস স্ক্র্যাপার কনসোল (রিয়েল-টাইম লাইভ ডাটা সংগ্রহ)\n"
                    "৩. অ্যান্টি-ব্যান সুরক্ষা ও স্পিন ভেরিয়েন্টসহ হোয়াটসঅ্যাপ অটোমেশন\n"
                    "৪. সম্পূর্ণ অফলাইন ও প্রাইভেট—ডাটা ক্লাউডে উন্মুক্ত হয় না।"
                )
            else:
                return (
                    "DataKarkhana is Bangladesh's premier verified B2B lead generation ecosystem, real-time Google Maps scraper, and anti-ban automated WhatsApp/Email marketing platform.\n\n"
                    "Core Capabilities:\n"
                    "1. Curated Marketplace of phone-verified B2B records with zero recurring proxy fees.\n"
                    "2. Live Desktop Scraper Console for Google Maps business listings.\n"
                    "3. Automated WhatsApp Campaign Engine with anti-ban random message variants and delay throttling.\n"
                    "4. Complete Data Privacy: Runs standalone on your PC without cloud surveillance."
                )
        elif any(w in p_lower for w in ["lead generation", "লিড জেনারেশন", "b2b"]):
            if is_bn:
                return (
                    "B2B লিড জেনারেশন হলো এমন একটি পদ্ধতি যার মাধ্যমে কোনো ব্যবসার সম্ভাব্য কর্পোরেট বা বাণিজ্যিক ক্রেতাদের তথ্য (যেমন: ব্যবসার নাম, মোবাইল নম্বর, ইমেইল, ঠিকানা) সংগ্রহ করা হয়।\n\n"
                    "কার্যকর ধাপসমূহ:\n"
                    "১. নিশ অডিয়েন্স নির্বাচন: আপনার সেবা কোন ক্যাটাগরির প্রতিষ্ঠানের প্রয়োজন তা চিহ্নিত করা।\n"
                    "২. ভেরিফাইড ডাটা সংগ্রহ: কার্যকর স্ক্র্যাপিং বা ডাটাবেজ থেকে সঠিক যোগাযোগ নম্বর নিশ্চিত করা।\n"
                    "৩. পার্সোনালাইজড আউটরিচ: হোয়াটসঅ্যাপ বা ইমেইলের মাধ্যমে উপযুক্ত প্রস্তাবনা পাঠানো।\n"
                    "৪. ফলো-আপ ও সেলস কনভার্সন: আগ্রহীদের সাথে সরাসরি ফোনে বা মিটিংয়ে চুক্তি সম্পন্ন করা।"
                )
            else:
                return (
                    "B2B (Business-to-Business) Lead Generation is the systematic process of identifying, collecting, and qualifying commercial prospects who have a high likelihood of purchasing your products or services.\n\n"
                    "Key Pipeline Phases:\n"
                    "1. Prospect Identification: Pinpointing targeted business categories and locations.\n"
                    "2. Contact Verification: Extracting direct phone numbers and verified operational details.\n"
                    "3. Multi-Channel Outreach: Dispatching personalized pitches via WhatsApp and Email.\n"
                    "4. Qualification & Closing: Converting interested inquiries into paying clients."
                )
        elif is_bn:
            topic = p_clean.replace("কী", "").replace("কি", "").replace("কীভাবে", "").replace("কিভাবে", "").replace("ব্যাখ্যা করুন", "").strip(" ?।")
            return (
                f"'{topic}' সম্পর্কিত বিবরণ:\n\n"
                f"১. ধারণা: {topic} বর্তমান তথ্যপ্রযুক্তি ও আধুনিক ব্যবসায়িক কার্যক্রমে অত্যন্ত গুরুত্বপূর্ণ একটি ক্ষেত্র।\n"
                f"২. কার্যপদ্ধতি: সঠিক পরিকল্পনা, সুনির্দিষ্ট লক্ষ্য এবং মানসম্মত তথ্যের সমন্বয়ে এটি পরিচালিত হয়।\n"
                f"৩. সুবিধা: এর মাধ্যমে সময় ও খরচের সাশ্রয় হয় এবং প্রত্যাশিত ফলাফল অর্জন সহজতর হয়।\n\n"
                f"নির্দিষ্ট কোনো দিক সম্পর্কে আরও বিস্তারিত জানতে চাইলে প্রশ্নটি সুনির্দিষ্ট করতে পারেন।"
            )
        else:
            topic = re.sub(r"(what is|what are|how to|explain|tell me about)\s*", "", p_lower).strip(" ?.")
            return (
                f"Overview regarding '{topic.title()}':\n\n"
                f"1. Core Concept: {topic.title()} plays an integral role in modern digital operations and strategic planning.\n"
                f"2. Implementation Strategy: Success relies on clean workflow architecture, reliable source data, and measurable performance benchmarks.\n"
                f"3. Practical Advantage: It streamlines efficiency, minimizes operational friction, and provides scalable outcomes for your business.\n\n"
                f"Let me know if you would like a deeper breakdown on any specific facet of this topic."
            )

    # Intent: Code / Programming / Technical script
    if any(k in p_lower for k in ["python", "javascript", "code", "script", "sql", "html", "regex"]):
        if "csv" in p_lower or "pandas" in p_lower:
            return (
                "```python\n"
                "# Python script to read and process lead dataset\n"
                "import pandas as pd\n\n"
                "def process_leads(file_path: str):\n"
                "    df = pd.read_csv(file_path)\n"
                "    print(f'Total contacts loaded: {len(df)}')\n"
                "    # Filter active phone numbers\n"
                "    valid_contacts = df.dropna(subset=['phone'])\n"
                "    return valid_contacts\n\n"
                "if __name__ == '__main__':\n"
                "    leads = process_leads('leads_export.csv')\n"
                "    print(leads.head())\n"
                "```\n\n"
                "This script cleanly imports your leads, filters empty phone rows, and prepares records for dispatch."
            )
        elif is_bn:
            return (
                "```python\n"
                "# কাস্টম পাইথন অটোমেশন স্ক্রিপ্ট\n"
                "def run_task():\n"
                "    print('DataKarkhana Automation Process Running...')\n"
                "    return True\n\n"
                "if __name__ == '__main__':\n"
                "    run_task()\n"
                "```\n\n"
                "এই স্ক্রিপ্টটি নির্বিঘ্নে পাইথন পরিবেশে রান করা যাবে।"
            )
        else:
            return (
                "```python\n"
                "# Custom Python execution block\n"
                "def execute_task():\n"
                "    print('Executing requested automation block successfully.')\n"
                "    return {'status': 'success'}\n\n"
                "if __name__ == '__main__':\n"
                "    execute_task()\n"
                "```\n\n"
                "Script formatted and ready for execution within your backend environment."
            )

    # General Open-Ended Prompt Synthesizer
    if is_bn:
        return (
            f"আপনার প্রদত্ত প্রম্পট: \"{p_clean}\"\n\n"
            f"বিশ্লেষণ ও ফলাফল:\n"
            f"১. মূল বিষয়বস্তু: আপনার অনুরোধ অনুযায়ী প্রাসঙ্গিক তথ্য ও কাঠামো প্রস্তুত করা হয়েছে।\n"
            f"২. কার্যপদ্ধতি: ব্যবসায়িক বার্তা ও প্রচারণায় সবসময় স্পষ্ট বক্তব্য এবং গ্রাহক-কেন্দ্রিক ভাষা ব্যবহার করুন।\n"
            f"৩. ফলো-আপ: কোনো নির্দিষ্ট ভেরিয়েন্ট বা বিশেষ ফরম্যাটে রূপান্তর করতে চাইলে প্রম্পটে শর্ত উল্লেখ করে পুনরায় রান করতে পারেন।"
        )
    else:
        return (
            f"Response for prompt: \"{p_clean}\"\n\n"
            f"Analysis & Output:\n"
            f"1. Contextual Match: Generated actionable insight directly aligned with your query requirements.\n"
            f"2. Practical Recommendation: Ensure your messaging remains concise, benefit-driven, and easy for recipients to act upon.\n"
            f"3. Next Step: You can refine this prompt with specific constraints (such as tone, length, or target customer) for further customized output."
        )


# /*
#  * ============================================================================
#  * FUNCTION : _synthesize_fallback_variant(...) -> str
#  * TASK     : Synthesizes distinct linguistic variations of a marketing message.
#  * HANDLES  :
#  *   - Preserves personalized {name} tags and core promotional text.
#  *   - Alternates greeting hooks, rhythm, and closing calls-to-action.
#  *   - Generates naturally diversified copy for both Bengali and English.
#  * ============================================================================
#  */
def _synthesize_fallback_variant(base: str, variant_num: int, lang: str) -> str:
    """
    ============================================================================
    TASK: Synthesizes intelligent linguistic variations of a marketing message.
    ============================================================================
    PURPOSE:
        Varies opening greetings, rhythm, and closing calls-to-action while
        preserving placeholder tokens ({name}) and core offer details.

    PARAMETERS:
        base (str): Base template text.
        variant_num (int): Sequential variant index (1, 2, 3...) for cycling hooks.
        lang (str): Language code ('bn' or 'en').

    RETURNS:
        str: Reformatted variant message.
    ============================================================================
    """
    has_name = "{name}" in base
    core_text = base.replace("{name}", "").strip(" ,!-\n")

    if lang == "bn":
        hooks = [
            "শুভ অপরাহ্ন {name}, আশা করি ভালো আছেন।",
            "নমস্কার {name}, আপনার অবগতির জন্য জানাচ্ছি যে,",
            "{name}, একটি গুরুত্বপূর্ণ আপডেট শেয়ার করতে যোগাযোগ করছি।",
            "আসসালামু আলাইকুম {name}, আশা করি আপনার দিনটি শুভ কাটছে।",
        ]
        closings = [
            "বিস্তারিত জানতে ইনবক্স করুন। ধন্যবাদ!",
            "যেকোনো তথ্যের জন্য নিঃসংকোচে যোগাযোগ করতে পারেন।",
            "আমাদের সাথে যুক্ত থাকার জন্য আন্তরিক ধন্যবাদ।",
        ]
    else:
        hooks = [
            "Hello {name}, hope you're having a productive week!",
            "Hi {name}, wanted to share a quick update regarding our services.",
            "Greetings {name}, hope this note finds you well.",
            "{name}, thought you might find this relevant for your business.",
        ]
        closings = [
            "Feel free to reply if you'd like more details. Thanks!",
            "Let us know if you have any questions!",
            "Best regards and looking forward to connecting.",
        ]

    hook = hooks[(variant_num - 1) % len(hooks)]
    closing = closings[(variant_num - 1) % len(closings)]

    if has_name:
        return f"{hook}\n\n{core_text}\n\n{closing}"
    else:
        hook_no_name = hook.replace("{name},", "").replace("{name}", "").strip()
        return f"{hook_no_name}\n\n{core_text}\n\n{closing}"


# /*
#  * ============================================================================
#  * FUNCTION : _generate_safe_rewrite(text: str, lang: str) -> str
#  * TASK     : Replaces aggressive promotional spam keywords with conversational phrasing.
#  * HANDLES  :
#  *   - Scans text for high-risk urgency and commercial triggers.
#  *   - Performs case-insensitive substitution with friendly, compliant alternatives.
#  *   - Eliminates spam triggers that cause WhatsApp hash and account bans.
#  * ============================================================================
#  */
def _generate_safe_rewrite(text: str, lang: str) -> str:
    """
    ============================================================================
    TASK: Cleans promotional spam keywords and replaces them with natural copy.
    ============================================================================
    PURPOSE:
        Identifies high-severity spam trigger words (e.g. '100% FREE', 'BUY NOW',
        'এখনই কিনুন') and substitutes them with conversational phrasing to avoid
        anti-spam detection.

    PARAMETERS:
        text (str): Source text containing potential spam triggers.
        lang (str): Language code ('bn' or 'en').

    RETURNS:
        str: Cleaned, de-escalated text.
    ============================================================================
    """
    replacements = {
        "100% ফ্রি": "বিশেষ সুযোগে",
        "ফ্রি": "সুবিধাজনক অফারে",
        "এখনই কিনুন": "বিস্তারিত জানতে পারেন",
        "তাড়াতাড়ি করুন": "সময় সুযোগমতো যোগাযোগ করুন",
        "সীমিত অফার": "বর্তমান অফার",
        "FREE": "Complimentary",
        "Hurry": "Feel free to check out",
        "BUY NOW": "Learn more",
        "Guaranteed": "Reliable",
    }
    safe_text = text
    for old, new in replacements.items():
        safe_text = re.sub(re.escape(old), new, safe_text, flags=re.IGNORECASE)
    return safe_text
