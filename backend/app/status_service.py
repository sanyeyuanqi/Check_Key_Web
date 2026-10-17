from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Credential, CredentialStatus
from .platform_catalog import scoped_provider
from .status_catalog import initial_status_rows


async def ensure_credential_statuses(session: AsyncSession, values: Iterable[str]) -> None:
    catalog = {row["value"]: row for row in initial_status_rows()}
    rows = [
        catalog.get(value, {"value": value, "name": value, "tone": "neutral", "sort_order": 1000})
        for value in sorted(set(values))
    ]
    if rows:
        await session.execute(insert(CredentialStatus).values(rows).on_conflict_do_nothing(index_elements=["value"]))


async def list_credential_statuses(
    session: AsyncSession, *, provider: str | None = None, storage_type: str | None = None,
    created_from: datetime | None = None, created_before: datetime | None = None,
) -> list[dict[str, Any]]:
    filters = [Credential.status == CredentialStatus.value]
    provider = scoped_provider(provider, storage_type)
    if provider:
        filters.append(Credential.provider == provider)
    if storage_type:
        filters.append(Credential.storage_type == storage_type)
    if created_from is not None:
        filters.append(Credential.created_at >= created_from)
    if created_before is not None:
        filters.append(Credential.created_at < created_before)
    rows = (await session.execute(
        select(CredentialStatus.value, CredentialStatus.name, CredentialStatus.tone,
               CredentialStatus.sort_order, func.count(Credential.id))
        .outerjoin(Credential, and_(*filters))
        .where(CredentialStatus.value != "pending")
        .group_by(CredentialStatus.value, CredentialStatus.name, CredentialStatus.tone, CredentialStatus.sort_order)
        .order_by(CredentialStatus.sort_order, CredentialStatus.value)
    )).all()
    return [
        {"value": value, "name": name, "tone": tone, "sortOrder": order, "count": int(count)}
        for value, name, tone, order, count in rows
    ]


async def normalize_exact_export_status(session: AsyncSession, status: str) -> str:
    if status == "all":
        return status
    selected = set(status.split(","))
    if "" in selected:
        raise ValueError("请选择有效的导出状态")
    available = (await session.scalars(
        select(CredentialStatus.value)
        .where(CredentialStatus.value != "pending")
        .order_by(CredentialStatus.sort_order, CredentialStatus.value)
    )).all()
    if not selected.issubset(available):
        raise ValueError("不支持的导出状态")
    return ",".join(value for value in available if value in selected)
