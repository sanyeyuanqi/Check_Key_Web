import hashlib
import re
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, Response
from redis.asyncio import Redis
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import AuthLoginAttempt, AuthSession, AuthUser
from .security import (
    generated_password,
    generated_username,
    hash_password,
    hash_token,
    random_session_token,
    verify_password,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def user_json(user: AuthUser) -> dict[str, object]:
    return {"id": user.id, "username": user.username, "role": user.role}


async def ensure_bootstrap_admin(session: AsyncSession) -> None:
    count = await session.scalar(select(func.count()).select_from(AuthUser))
    if count or not settings.bootstrap_admin_password:
        return
    username = settings.bootstrap_admin_username.strip()
    session.add(AuthUser(
        username=username,
        username_normalized=username.lower(),
        password_hash=hash_password(settings.bootstrap_admin_password),
        role="admin",
        active=True,
    ))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()


async def _rate_count(redis: Redis, key: str, seconds: int) -> int:
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, seconds)
    return int(count)


async def login(
    session: AsyncSession,
    redis: Redis,
    request: Request,
    username_value: str,
    password: str,
) -> tuple[AuthUser, str]:
    username = username_value.strip().lower()
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    ip = forwarded or (request.client.host if request.client else "unknown")
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()
    window = settings.login_lock_minutes * 60
    ip_count, user_count = await _rate_count(redis, f"login:ip:{ip_hash}", window), await _rate_count(
        redis, f"login:user:{username}", window
    )
    if ip_count > 20 or user_count > 10:
        raise HTTPException(status_code=429, detail="登录过于频繁，请 15 分钟后再试")

    user = await session.scalar(select(AuthUser).where(AuthUser.username_normalized == username))
    now = utcnow()
    locked = bool(user and user.locked_until and user.locked_until > now)
    valid = bool(user and user.active and not locked and verify_password(user.password_hash, password))
    session.add(AuthLoginAttempt(
        username_normalized=username,
        ip_hash=ip_hash,
        succeeded=valid,
    ))
    if locked:
        await session.commit()
        raise HTTPException(status_code=429, detail="账号已临时锁定，请稍后再试")
    if not valid or not user:
        if user:
            next_count = user.failed_login_count + 1
            if next_count >= settings.max_login_failures:
                user.failed_login_count = 0
                user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
            else:
                user.failed_login_count = next_count
        await session.commit()
        raise HTTPException(status_code=401, detail="账号或密码错误")

    token = random_session_token()
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    session.add(AuthSession(
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=now + timedelta(seconds=settings.session_lifetime_seconds),
    ))
    await session.execute(delete(AuthSession).where(
        (AuthSession.expires_at < now) | AuthSession.revoked_at.is_not(None)
    ))
    await session.commit()
    return user, token


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_lifetime_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")


async def current_user(session: AsyncSession, token: str | None) -> AuthUser | None:
    if not token or not token.startswith("kcs_") or len(token) > 200:
        return None
    return await session.scalar(
        select(AuthUser)
        .join(AuthSession, AuthSession.user_id == AuthUser.id)
        .where(
            AuthSession.token_hash == hash_token(token),
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > utcnow(),
            AuthUser.active.is_(True),
        )
    )


async def revoke_session(session: AsyncSession, token: str | None) -> None:
    if token:
        await session.execute(update(AuthSession).where(
            AuthSession.token_hash == hash_token(token), AuthSession.revoked_at.is_(None)
        ).values(revoked_at=utcnow()))
        await session.commit()


async def create_user(session: AsyncSession, admin: AuthUser, requested_username: str, role: str) -> dict[str, object]:
    username = requested_username.strip() or generated_username()
    if not re.fullmatch(r"[a-zA-Z0-9._-]{3,32}", username):
        raise HTTPException(status_code=400, detail="账号须为 3–32 位字母、数字、点、横线或下划线")
    password = generated_password()
    user = AuthUser(
        username=username,
        username_normalized=username.lower(),
        password_hash=hash_password(password),
        role="admin" if role == "admin" else "user",
        active=True,
        created_by=admin.id,
    )
    session.add(user)
    try:
        await session.commit()
        await session.refresh(user)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail="该账号已存在") from exc
    return {"id": user.id, "username": user.username, "password": password, "role": user.role}


async def managed_user(session: AsyncSession, user_id: int) -> AuthUser:
    user = await session.get(AuthUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="账号不存在")
    return user


async def reset_password(session: AsyncSession, admin: AuthUser, user_id: int) -> dict[str, object]:
    user = await managed_user(session, user_id)
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="当前登录账号不能在此重置密码")
    password = generated_password()
    user.password_hash = hash_password(password)
    user.failed_login_count = 0
    user.locked_until = None
    await session.execute(update(AuthSession).where(
        AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)
    ).values(revoked_at=utcnow()))
    await session.commit()
    return {"id": user.id, "username": user.username, "password": password, "role": user.role}


async def set_user_active(session: AsyncSession, user_id: int, active: bool) -> dict[str, object]:
    user = await managed_user(session, user_id)
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="管理员账号不能被禁用")
    user.active = active
    user.failed_login_count = 0
    user.locked_until = None
    if not active:
        await session.execute(update(AuthSession).where(
            AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)
        ).values(revoked_at=utcnow()))
    await session.commit()
    return {"id": user.id, "active": active}


async def list_users(session: AsyncSession) -> list[dict[str, object]]:
    users = (await session.scalars(select(AuthUser).order_by(AuthUser.created_at.desc()).limit(200))).all()
    return [{
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "active": user.active,
        "createdAt": user.created_at.isoformat(),
        "lastLoginAt": user.last_login_at.isoformat() if user.last_login_at else None,
        "lockedUntil": user.locked_until.isoformat() if user.locked_until else None,
    } for user in users]
