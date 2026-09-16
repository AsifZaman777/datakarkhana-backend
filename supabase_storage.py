"""
Data Karkhana — Supabase Storage Client
Manages cloud storage of datasets (.xlsx, .csv) in Supabase Storage Buckets.
"""
import os
import re
from typing import Optional, Tuple, Union
import requests

from config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_STORAGE_BUCKET, DATABASE_URL

_bucket_verified = False


def get_effective_supabase_url() -> str:
    """Returns SUPABASE_URL or extracts it from Supabase DATABASE_URL if available."""
    if SUPABASE_URL:
        return SUPABASE_URL.rstrip("/")
    
    # Try deducing from DATABASE_URL
    # e.g., postgresql://postgres.abcdef:pass@aws-0-ap-south-1.pooler.supabase.com:6543/postgres
    # or db.abcdefg.supabase.co
    if DATABASE_URL:
        m = re.search(r"db\.([a-zA-Z0-9_-]+)\.supabase\.co", DATABASE_URL)
        if m:
            return f"https://{m.group(1)}.supabase.co"
        m2 = re.search(r"postgres(?:ql)?://postgres(?:\.([a-zA-Z0-9_-]+))?:", DATABASE_URL)
        if m2 and m2.group(1):
            return f"https://{m2.group(1)}.supabase.co"
            
    return ""


def is_supabase_storage_configured() -> bool:
    """
    Returns True only if both the Supabase project URL and service role key are available.
    """
    url = get_effective_supabase_url()
    key = SUPABASE_SERVICE_ROLE_KEY
    return bool(url and key and len(key.strip()) > 10)


def get_headers(extra_headers: Optional[dict] = None) -> dict:
    key = SUPABASE_SERVICE_ROLE_KEY
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }
    if extra_headers:
        headers.update(extra_headers)
    return headers


def get_bucket_name() -> str:
    return SUPABASE_STORAGE_BUCKET or "datasets"


def ensure_bucket_exists(bucket_name: Optional[str] = None) -> bool:
    """
    Ensures that the target bucket exists in Supabase Storage.
    Creates it if it does not already exist.
    """
    global _bucket_verified
    if _bucket_verified:
        return True

    if not is_supabase_storage_configured():
        return False

    b_name = bucket_name or get_bucket_name()
    base_url = get_effective_supabase_url()
    endpoint = f"{base_url}/storage/v1/bucket/{b_name}"

    try:
        # Check if bucket exists
        res = requests.get(endpoint, headers=get_headers(), timeout=6)
        if res.status_code == 200:
            _bucket_verified = True
            return True
        elif res.status_code == 404:
            # Create the bucket (private by default to protect datasets)
            create_endpoint = f"{base_url}/storage/v1/bucket"
            create_res = requests.post(
                create_endpoint,
                headers=get_headers({"Content-Type": "application/json"}),
                json={"id": b_name, "name": b_name, "public": False},
                timeout=8
            )
            if create_res.status_code in (200, 201):
                _bucket_verified = True
                return True
            else:
                print(f"[SUPABASE STORAGE] Bucket creation returned: {create_res.status_code} {create_res.text}")
                return False
    except Exception as e:
        print(f"[SUPABASE STORAGE NOTICE] Error verifying bucket '{b_name}': {e}")
        return False

    return False


def normalize_storage_path(path: str) -> str:
    """
    Normalizes a path like 'supabase://datasets/synced/1/test.xlsx' -> 'synced/1/test.xlsx'
    """
    if not path:
        return ""
    p = path.strip()
    if p.startswith("supabase://"):
        p = p.replace("supabase://", "")
        # Remove bucket name prefix if present
        b_name = get_bucket_name()
        if p.startswith(f"{b_name}/"):
            p = p[len(b_name) + 1:]
    return p.lstrip("/")


def upload_dataset_file(
    file_bytes: bytes,
    relative_path: str,
    content_type: Optional[str] = None,
    bucket_name: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Uploads a dataset file to the Supabase Storage Bucket.
    Returns: (success: bool, stored_path_or_error: str)
    """
    if not is_supabase_storage_configured():
        return False, "Cloud dataset storage is currently not configured on this server."

    b_name = bucket_name or get_bucket_name()
    ensure_bucket_exists(b_name)

    clean_path = normalize_storage_path(relative_path)
    base_url = get_effective_supabase_url()
    upload_url = f"{base_url}/storage/v1/object/{b_name}/{clean_path}"

    if not content_type:
        if clean_path.endswith(".csv"):
            content_type = "text/csv"
        elif clean_path.endswith(".xlsx"):
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            content_type = "application/octet-stream"

    headers = get_headers({
        "Content-Type": content_type,
        "x-upsert": "true",
    })

    try:
        resp = requests.post(upload_url, data=file_bytes, headers=headers, timeout=30)
        if resp.status_code in (200, 201):
            stored_uri = f"supabase://{b_name}/{clean_path}"
            return True, stored_uri
        else:
            error_msg = f"Supabase Storage Upload Error ({resp.status_code}): {resp.text}"
            print(f"[SUPABASE STORAGE] {error_msg}")
            return False, error_msg
    except Exception as e:
        error_msg = f"Failed to upload to Supabase Storage: {str(e)}"
        print(f"[SUPABASE STORAGE] {error_msg}")
        return False, error_msg


def download_dataset_file(file_path_or_uri: str, bucket_name: Optional[str] = None) -> Tuple[bool, Optional[bytes], str]:
    """
    Downloads dataset file bytes from Supabase Storage.
    Returns: (success: bool, file_bytes: Optional[bytes], error_message: str)
    """
    if not is_supabase_storage_configured():
        return False, None, "Cloud dataset storage is currently not configured on this server."

    b_name = bucket_name or get_bucket_name()
    clean_path = normalize_storage_path(file_path_or_uri)
    base_url = get_effective_supabase_url()

    # Authenticated object retrieval
    download_url = f"{base_url}/storage/v1/object/authenticated/{b_name}/{clean_path}"

    try:
        resp = requests.get(download_url, headers=get_headers(), timeout=30)
        if resp.status_code == 200:
            return True, resp.content, ""
        elif resp.status_code == 404:
            # Try unauthenticated object path in case the bucket is marked public
            pub_url = f"{base_url}/storage/v1/object/{b_name}/{clean_path}"
            resp_pub = requests.get(pub_url, headers=get_headers(), timeout=15)
            if resp_pub.status_code == 200:
                return True, resp_pub.content, ""
            return False, None, f"File not found in Supabase Storage bucket '{b_name}' at path '{clean_path}'."
        else:
            return False, None, f"Failed to download from Supabase Storage ({resp.status_code}): {resp.text}"
    except Exception as e:
        return False, None, f"Network error reading from Supabase Storage: {str(e)}"


def get_signed_url(file_path_or_uri: str, expires_in: int = 3600, bucket_name: Optional[str] = None) -> Optional[str]:
    """
    Generates a temporary signed download URL valid for `expires_in` seconds.
    """
    if not is_supabase_storage_configured():
        return None

    b_name = bucket_name or get_bucket_name()
    clean_path = normalize_storage_path(file_path_or_uri)
    base_url = get_effective_supabase_url()
    sign_url = f"{base_url}/storage/v1/object/sign/{b_name}/{clean_path}"

    try:
        resp = requests.post(
            sign_url,
            headers=get_headers({"Content-Type": "application/json"}),
            json={"expiresIn": expires_in},
            timeout=8
        )
        if resp.status_code == 200:
            data = resp.json()
            signed_path = data.get("signedURL") or data.get("signedUrl")
            if signed_path:
                if signed_path.startswith("http"):
                    return signed_path
                return f"{base_url}/storage/v1{signed_path}"
    except Exception as e:
        print(f"[SUPABASE STORAGE SIGNED URL ERROR] {e}")

    return None


def delete_dataset_file(file_path_or_uri: str, bucket_name: Optional[str] = None) -> bool:
    """
    Deletes a file from Supabase Storage.
    """
    if not is_supabase_storage_configured():
        return False

    b_name = bucket_name or get_bucket_name()
    clean_path = normalize_storage_path(file_path_or_uri)
    base_url = get_effective_supabase_url()
    delete_url = f"{base_url}/storage/v1/object/{b_name}/{clean_path}"

    try:
        resp = requests.delete(delete_url, headers=get_headers(), timeout=10)
        return resp.status_code in (200, 204)
    except Exception:
        return False
