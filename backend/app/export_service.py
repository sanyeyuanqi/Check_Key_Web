import asyncio
import math
import re
from datetime import UTC, datetime
from ipaddress import IPv6Address, ip_address
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import AuthUser, CredentialStatus, ExportRecord
from .ip_geolocation import resolve_ip_region


EXPORT_STATUSES = ("valid", "usage_limit_reached", "insufficient_credit", "invalid")


def normalize_export_status(status: str) -> str:
    if status == "all":
        return status
    selected = set(status.split(","))
    if not selected or not selected.issubset(EXPORT_STATUSES):
        raise ValueError("不支持的导出状态")
    return ",".join(item for item in EXPORT_STATUSES if item in selected)


def export_filename(
    provider: str | None, status: str, *,
    created_from: datetime | None = None, created_before: datetime | None = None,
) -> str:
    slugs = {
        "OpenAI": "openai", "Anthropic": "anthropic", "Google": "google",
        "Azure": "azure", "AWS Bedrock": "aws", "OpenRouter": "openrouter",
        "TokenRouter": "tokenrouter",
        "Vercel": "vercel",
        "OpenCode": "opencode",
        "Cloudflare": "cloudflare",
    }
    filename_status = status.replace(",", "-")
    if len(filename_status) > 100 or not re.fullmatch(r"[a-zA-Z0-9_-]+", filename_status):
        filename_status = "selected"
    if provider in slugs:
        base_name = f"{slugs[provider]}-{filename_status}.txt"
    elif "," in status or filename_status == "selected":
        base_name = f"all-{filename_status}-keys.txt"
    else:
        filenames = {
            "all": "all-keys.txt",
            "valid": "all-valid-keys.txt",
            "usage_limit_reached": "all-usage-limit-reached-keys.txt",
            "insufficient_credit": "all-insufficient-credit-keys.txt",
            "invalid": "all-invalid-keys.txt",
        }
        base_name = filenames.get(status, f"all-{filename_status}-keys.txt")
    boundaries = []
    if created_from is not None:
        boundaries.append("from-" + created_from.astimezone(UTC).strftime("%Y-%m-%dT%H%M%SZ"))
    if created_before is not None:
        boundaries.append("before-" + created_before.astimezone(UTC).strftime("%Y-%m-%dT%H%M%SZ"))
    if boundaries:
        suffix = "-" + "-".join(boundaries) + ".txt"
        return base_name.removesuffix(".txt")[:160 - len(suffix)].rstrip("-") + suffix
    return base_name


async def record_export(
    session: AsyncSession,
    *,
    user: AuthUser,
    provider: str | None,
    storage_type: str | None,
    status: str,
    count: int,
    file_name: str,
    ip_address: str | None = None,
    status_mode: str = "bucket",
) -> None:
    session.add(ExportRecord(
        user_id=user.id,
        username=user.username,
        provider=provider or None,
        storage_type=storage_type or None,
        status=status,
        status_mode=status_mode,
        credential_count=count,
        file_name=file_name,
        ip_address=ip_address,
    ))
    await session.commit()


def _escape_filter_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_export_records(
    session: AsyncSession, *, user: AuthUser, page: int,
    username: str | None = None, ip: str | None = None,
) -> dict[str, Any]:
    filters = [] if user.role in {"super_admin", "admin"} else [ExportRecord.user_id == user.id]
    account_query = (username or "").strip()
    if account_query:
        filters.append(ExportRecord.username.ilike(f"%{_escape_filter_literal(account_query)}%", escape="\\"))
    ip_query = (ip or "").strip()
    if ip_query:
        try:
            address = ip_address(ip_query)
            if isinstance(address, IPv6Address):
                address = address.ipv4_mapped or ip_address(str(address).split("%", 1)[0])
        except ValueError:
            filters.append(ExportRecord.ip_address.ilike(f"{_escape_filter_literal(ip_query)}%", escape="\\"))
        else:
            filters.append(ExportRecord.ip_address == str(address))
    total = int(await session.scalar(select(func.count(ExportRecord.id)).where(*filters)) or 0)
    page_size = 50
    total_pages = max(1, math.ceil(total / page_size))
    page = min(max(1, page), total_pages)
    records = (await session.scalars(
        select(ExportRecord)
        .where(*filters)
        .order_by(ExportRecord.created_at.desc(), ExportRecord.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()
    status_names = dict((await session.execute(select(CredentialStatus.value, CredentialStatus.name))).all()) if any(
        row.status_mode == "exact" for row in records
    ) else {}
    legacy_names = {
        "all": "所有密钥", "valid": "可用密钥", "usage_limit_reached": "额度待恢复",
        "insufficient_credit": "余额不足", "invalid": "不可用密钥",
    }
    addresses = {row.ip_address for row in records}
    ip_regions = await asyncio.to_thread(
        lambda: {address: resolve_ip_region(address) for address in addresses}
    ) if addresses else {}
    return {
        "records": [{
            "id": row.id,
            "createdAt": row.created_at.isoformat(),
            "username": row.username,
            "provider": row.provider,
            "storageType": row.storage_type,
            "status": row.status,
            "statusNames": [
                (status_names if row.status_mode == "exact" else legacy_names).get(value, "所有密钥" if value == "all" else value)
                for value in row.status.split(",")
            ],
            "count": row.credential_count,
            "fileName": row.file_name,
            "ipAddress": row.ip_address,
            "ipRegion": ip_regions[row.ip_address],
        } for row in records],
        "total": total,
        "page": page,
        "totalPages": total_pages,
        "pageSize": page_size,
    }
