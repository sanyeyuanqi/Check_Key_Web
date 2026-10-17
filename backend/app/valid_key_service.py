import math
from datetime import datetime
from ipaddress import IPv6Address, ip_address
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Credential, CredentialStatus, ValidKeyRecord
from .platform_catalog import scoped_provider


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


async def list_valid_key_records(
    session: AsyncSession,
    *,
    provider: str | None = None,
    storage_type: str | None = None,
    query: str | None = None,
    ip: str | None = None,
    page: int = 1,
) -> dict[str, Any]:
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
