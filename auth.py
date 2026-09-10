import os
import hashlib
import jwt
from datetime import datetime, timedelta

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
    expire = datetime.utcnow() + timedelta(days=7)
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
