import base64
import hashlib
import json
import secrets
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .config import settings

password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def _fernet() -> Fernet:
    if not settings.fernet_key:
        raise RuntimeError("FERNET_KEY 未配置")
    return Fernet(settings.fernet_key.encode("ascii"))


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(f"{settings.session_secret}\0{token}".encode()).hexdigest()


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("密钥数据无法解密") from exc


def decrypt_secret_batch(values: list[str]) -> list[str]:
    cipher = _fernet()
    try:
        return [cipher.decrypt(value.encode("ascii")).decode("utf-8") for value in values]
    except InvalidToken as exc:
        raise ValueError("密钥数据无法解密") from exc


def encrypt_job(payload: dict[str, Any]) -> str:
    packed = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(packed).decode("ascii")


def decrypt_job(value: str | bytes) -> dict[str, Any]:
    raw = value.encode("ascii") if isinstance(value, str) else value
    return json.loads(_fernet().decrypt(raw).decode("utf-8"))


def random_session_token() -> str:
    return f"kcs_{secrets.token_urlsafe(32)}"


def generated_password(length: int = 22) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generated_username() -> str:
    return f"user_{secrets.token_urlsafe(6).replace('-', '').replace('_', '')[:7].lower()}"


def stable_node_id(value: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()[:18]).decode().rstrip("=")
