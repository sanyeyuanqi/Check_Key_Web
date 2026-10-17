import math
from datetime import datetime
from ipaddress import IPv6Address, ip_address
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Credential, CredentialStatus, ValidKeyRecord
from .platform_catalog import STORAGE_TYPE_PROVIDERS, scoped_provider
from .schemas import ValidKeyExportKeyInput
from .security import decrypt_secret


async def remember_valid_key(session: AsyncSession, credential_id: int, *, observed_at: datetime) -> None:
    statement = insert(ValidKeyRecord).values(
        credential_id=credential_id, first_valid_at=observed_at, last_valid_at=observed_at,
    )
    await session.execute(statement.on_conflict_do_update(
        index_elements=[ValidKeyRecord.credential_id],
        set_={
            "first_valid_at": func.least(ValidKeyRecord.first_valid_at, statement.excluded.first_valid_at),
            "last_valid_at": func.greatest(ValidKeyRecord.last_valid_at, statement.excluded.last_valid_at),
        },
    ))


def _valid_key_filters(
    *, provider: str | None, storage_type: str | None, query: str | None, ip: str | None,
) -> list[Any]:
    filters = []
    provider = scoped_provider(provider, storage_type)
    if provider:
        filters.append(Credential.provider == provider)
    if storage_type:
        filters.append(Credential.storage_type == storage_type)
    ip_query = (ip or "").strip()
    if ip_query:
        try:
            address = ip_address(ip_query)
            if isinstance(address, IPv6Address):
                address = address.ipv4_mapped or ip_address(str(address).split("%", 1)[0])
        except ValueError:
            prefix = ip_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(Credential.source_ip.ilike(f"{prefix}%", escape="\\"))
        else:
            filters.append(Credential.source_ip == str(address))
    elif query and query.strip():
        prefix = query.strip()[:32].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(Credential.key_prefix.like(f"{prefix}%", escape="\\"))
    return filters


async def list_valid_key_records(
    session: AsyncSession,
    *,
    provider: str | None = None,
    storage_type: str | None = None,
    query: str | None = None,
    ip: str | None = None,
    page: int = 1,
) -> dict[str, Any]:
    filters = _valid_key_filters(provider=provider, storage_type=storage_type, query=query, ip=ip)
    total = int(await session.scalar(
        select(func.count(ValidKeyRecord.credential_id))
        .join(Credential, Credential.id == ValidKeyRecord.credential_id)
        .where(*filters)
    ) or 0)
    page_size = 100
    total_pages = max(1, math.ceil(total / page_size))
    page = min(max(1, page), total_pages)
    rows = (await session.execute(
        select(ValidKeyRecord, Credential, CredentialStatus)
        .join(Credential, Credential.id == ValidKeyRecord.credential_id)
        .join(CredentialStatus, CredentialStatus.value == Credential.status)
        .where(*filters)
        .order_by(ValidKeyRecord.last_valid_at.desc(), ValidKeyRecord.credential_id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()
    return {
        "records": [{
            "credentialId": credential.id,
            "keyPrefix": credential.key_prefix,
            "masked": credential.masked,
            "provider": credential.provider,
            "storageType": credential.storage_type,
            "status": credential.status,
            "statusName": status.name,
            "statusTone": status.tone,
            "firstValidAt": record.first_valid_at.isoformat(),
            "lastValidAt": record.last_valid_at.isoformat(),
            "updatedAt": credential.updated_at.isoformat(),
            "ipAddress": credential.source_ip,
        } for record, credential, status in rows],
        "total": total,
        "page": page,
        "totalPages": total_pages,
        "pageSize": page_size,
    }


def _validate_export_scope(provider: str, storage_type: str | None = None) -> None:
    if provider not in set(STORAGE_TYPE_PROVIDERS.values()):
        raise HTTPException(status_code=422, detail="服务平台无效")
    if storage_type is not None and STORAGE_TYPE_PROVIDERS.get(storage_type) != provider:
        raise HTTPException(status_code=422, detail="密钥类型与服务平台不匹配")


async def export_selected_valid_keys(
    session: AsyncSession, *, provider: str, keys: list[ValidKeyExportKeyInput],
) -> list[str]:
    """Reveal only the complete, platform-scoped archive selection in input order."""
    _validate_export_scope(provider)
    identities = [(item.id, item.type) for item in keys]
    if not 1 <= len(keys) <= 100 or len(set(identities)) != len(identities):
        raise HTTPException(status_code=422, detail="所选密钥参数无效")
    if any(STORAGE_TYPE_PROVIDERS.get(item.type) != provider for item in keys):
        raise HTTPException(status_code=404, detail="所选有效密钥记录已变更或不存在，请刷新列表后重试")
    rows = (await session.scalars(select(Credential)
        .join(ValidKeyRecord, ValidKeyRecord.credential_id == Credential.id)
        .where(Credential.provider == provider, tuple_(Credential.id, Credential.storage_type).in_(identities))
    )).all()
    matched = {(row.provider, row.id, row.storage_type): row for row in rows}
    requested = [(provider, identifier, storage_type) for identifier, storage_type in identities]
    if any(identity not in matched for identity in requested):
        raise HTTPException(status_code=404, detail="所选有效密钥记录已变更或不存在，请刷新列表后重试")
    return [decrypt_secret(matched[identity].encrypted_secret) for identity in requested]


def valid_key_export_statement(
    *, provider: str, storage_type: str | None = None,
    query: str | None = None, ip: str | None = None,
) -> Select[tuple[str]]:
    """Select archive ciphertext without loading ORM objects or limiting pages."""
    _validate_export_scope(provider, storage_type)
    filters = _valid_key_filters(provider=provider, storage_type=storage_type, query=query, ip=ip)
    return (select(Credential.encrypted_secret)
        .join(ValidKeyRecord, ValidKeyRecord.credential_id == Credential.id)
        .where(*filters)
        .order_by(ValidKeyRecord.last_valid_at.desc(), ValidKeyRecord.credential_id.desc())
    )


async def export_all_valid_keys(
    session: AsyncSession, *, provider: str, storage_type: str | None = None,
    query: str | None = None, ip: str | None = None,
) -> list[str]:
    """Export every archive match across pages, regardless of its current status."""
    statement = valid_key_export_statement(
        provider=provider, storage_type=storage_type, query=query, ip=ip,
    ).with_only_columns(Credential)
    rows = (await session.scalars(statement)).all()
    return [decrypt_secret(row.encrypted_secret) for row in rows]
