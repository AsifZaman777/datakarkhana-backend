import os
import hashlib
import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Request
from urllib.parse import urlparse

from config import FRONTEND_URL, FRONTEND_LOCAL_URL, FRONTEND_RENDER_URL, FRONTEND_MODE

SECRET_KEY = os.getenv("JWT_SECRET", "marketingostad_super_secret_cyber_key_999")
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return salt.hex() + ":" + key.hex()


def verify_password(password: str, hashed: str) -> bool:
    try:
        salt_hex, key_hex = hashed.split(":")
        salt = bytes.fromhex(salt_hex)
        key = bytes.fromhex(key_hex)
        new_key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return new_key == key
    except Exception:
        return False


def create_jwt_token(user_id: int, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=7)
    payload = {
        "exp": expire,
        "sub": str(user_id),
        "role": role
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_jwt_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return {
            "user_id": int(payload["sub"]),
            "role": payload["role"]
        }
    except Exception:
        return None


def resolve_frontend_base_url(request: Optional[Request] = None) -> str:
    """Dynamically determine the frontend URL based on FRONTEND_MODE flag ('local' or 'render') or request origin"""
    mode = (os.getenv("FRONTEND_MODE") or FRONTEND_MODE or "local").strip().lower()

    # 1. If FRONTEND_MODE flag is set to render
    if mode in ("render", "production", "prod"):
        if FRONTEND_RENDER_URL:
            return FRONTEND_RENDER_URL.rstrip("/")

    # 2. If FRONTEND_MODE flag is set to local
    if mode in ("local", "dev", "development"):
        if FRONTEND_LOCAL_URL:
            return FRONTEND_LOCAL_URL.rstrip("/")

    # 3. Direct detection from the active client's request (e.g. Origin or Referer header)
    if request:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            try:
                parsed = urlparse(origin)
                if parsed.scheme in ("http", "https") and parsed.netloc:
                    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
            except Exception:
                pass

    # 4. Check if running on cloud Render
    if os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL"):
        if FRONTEND_RENDER_URL:
            return FRONTEND_RENDER_URL.rstrip("/")

    # 5. Explicit FRONTEND_URL or local fallback
    if FRONTEND_URL:
        return FRONTEND_URL.rstrip("/")
    if FRONTEND_LOCAL_URL:
        return FRONTEND_LOCAL_URL.rstrip("/")

    return "http://localhost:3000"
