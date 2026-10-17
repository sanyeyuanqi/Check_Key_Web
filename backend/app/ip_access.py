from datetime import UTC, datetime
from ipaddress import IPv6Address, ip_address
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .database import AuthIPBlock, AuthUser, get_session
from .request_context import client_ip


class IPAccessDenied(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=403, detail="此 IP 地址已被禁止访问")


def normalize_block_ip(value: str) -> str:
    value = value.strip()
    if "%" in value:
        raise HTTPException(status_code=422, detail="请输入有效的 IPv4 或 IPv6 地址")
    try:
        address = ip_address(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="请输入有效的 IPv4 或 IPv6 地址") from exc
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def require_ip_manager(admin: AuthUser) -> None:
    if admin.role != "super_admin":
        raise HTTPException(status_code=403, detail="需要超级管理员权限")


def prevent_self_block(value: str, request_ip: str | None) -> None:
    if request_ip and value == normalize_block_ip(request_ip):
        raise HTTPException(status_code=400, detail="不能封禁当前访问 IP，以免无法管理系统")


def block_json(block: AuthIPBlock, creator: str | None = None) -> dict[str, object]:
    return {
        "ipAddress": block.ip_address,
        "active": block.active,
        "reason": block.reason,
        "createdAt": block.created_at.isoformat(),
        "updatedAt": block.updated_at.isoformat(),
        "createdBy": creator,
    }


async def list_ip_blocks(session: AsyncSession) -> list[dict[str, object]]:
    rows = (await session.execute(
        select(AuthIPBlock, AuthUser.username)
        .outerjoin(AuthUser, AuthUser.id == AuthIPBlock.created_by)
        .order_by(AuthIPBlock.active.desc(), AuthIPBlock.updated_at.desc(), AuthIPBlock.ip_address)
    )).all()
    return [block_json(block, creator) for block, creator in rows]


async def block_ip(
    session: AsyncSession, admin: AuthUser, value: str, reason: str, request_ip: str | None,
) -> dict[str, object]:
    require_ip_manager(admin)
    value = normalize_block_ip(value)
    prevent_self_block(value, request_ip)
    reason = reason.strip()
    if len(reason) > 200:
        raise HTTPException(status_code=422, detail="备注不能超过 200 字")
    statement = insert(AuthIPBlock).values(
        ip_address=value, active=True, reason=reason, created_by=admin.id,
    ).on_conflict_do_update(
        index_elements=[AuthIPBlock.ip_address],
        set_={"active": True, "reason": reason, "updated_at": datetime.now(UTC)},
    ).returning(AuthIPBlock)
    block = (await session.scalars(statement, execution_options={"populate_existing": True})).one()
    creator = await session.scalar(select(AuthUser.username).where(AuthUser.id == block.created_by))
    await session.commit()
    return block_json(block, creator)


async def set_ip_block_active(
    session: AsyncSession, admin: AuthUser, value: str, active: bool, request_ip: str | None,
) -> dict[str, object]:
    require_ip_manager(admin)
    value = normalize_block_ip(value)
    if active:
        prevent_self_block(value, request_ip)
    block = await session.get(AuthIPBlock, value)
    if block is None:
        raise HTTPException(status_code=404, detail="IP 封禁记录不存在")
    block.active = active
    block.updated_at = datetime.now(UTC)
    creator = await session.scalar(select(AuthUser.username).where(AuthUser.id == block.created_by))
    await session.commit()
    return block_json(block, creator)


async def require_allowed_ip(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    if request.method == "OPTIONS" or not request.url.path.startswith("/api/"):
        return
    address = client_ip(request)
    if address:
        block = await session.get(AuthIPBlock, address)
        if block is not None and block.active:
            raise IPAccessDenied()
