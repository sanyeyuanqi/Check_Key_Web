import math
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Credential, ValidationBatch, ValidationBatchItem
from .platform_catalog import STORAGE_TYPE_PROVIDERS, scoped_provider
from .schemas import BatchRevealKeyInput, ValidationResultInput
from .security import decrypt_secret, encrypt_secret, fingerprint
from .status_service import ensure_credential_statuses, list_credential_statuses
from .valid_key_service import remember_valid_key

VALID_STORAGE_TYPES = set(STORAGE_TYPE_PROVIDERS)


def utcnow() -> datetime:
    return datetime.now(UTC)


def result_bucket(status: str) -> str:
    if status in {"valid", "usage_limit_reached", "insufficient_credit"}:
        return status
    return "invalid"


def infer_storage_type(provider: str, kind: str, model: str | None = None) -> str:
    if provider == "OpenCode" or kind == "opencode":
        return "opencode_zen"
    if provider == "Cloudflare" or kind == "cloudflare":
        return "cloudflare_workers_ai"
    if kind == "anthropic_oauth":
        return "anthropic_enterprise_subscription"
    if provider == "Anthropic" or kind == "anthropic":
        return "anthropic_official"
    if kind == "bedrock_api_key":
        return "aws_bedrock_api"
    if kind == "aws_access_key":
        return "aws_access"
    if provider == "OpenAI" or kind == "openai":
        return "openai_official"
    if provider == "OpenRouter" or kind == "openrouter":
        return "openrouter"
    if provider == "TokenRouter" or kind == "tokenrouter":
        return "tokenrouter"
    if provider == "Vercel" or kind == "vercel":
        return "vercel"
    if provider == "Azure" or kind == "azure":
        return "azure_claude" if (model or "").lower().startswith("claude-") else "azure_gpt"
    if kind == "google_ai_studio":
        return "google_ai_studio_gemini"
    if kind.startswith("google_vertex_"):
        return "google_vertex_claude" if (model or "").lower().startswith("claude-") else "google_vertex_gemini"
    raise HTTPException(status_code=400, detail="无法识别密钥类型")


async def start_batch(session: AsyncSession, node_id: str, provider_label: str, total: int) -> int:
    batch = ValidationBatch(node_id=node_id, provider_label=provider_label.strip() or "Unknown", total=total)
    session.add(batch)
    await session.commit()
    await session.refresh(batch)
    return batch.id


async def process_batch_results(
    session: AsyncSession,
    batch_id: int,
    results: list[ValidationResultInput],
    *,
    source_ip: str | None = None,
) -> int:
    batch = await session.scalar(
        select(ValidationBatch).where(ValidationBatch.id == batch_id).with_for_update()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="检测批次不存在")

    await ensure_credential_statuses(session, (item.status for item in results))
    now = utcnow()
    for item in results:
        secret = item.credential.secret
        requested_type = item.credential.requested_storage_type
        storage_type = requested_type if requested_type in VALID_STORAGE_TYPES else infer_storage_type(
            item.credential.provider,
            item.credential.kind,
            item.model,
        )
        bucket = result_bucket(item.status)
        credential_values = {
            "fingerprint": fingerprint(secret),
            "storage_type": storage_type,
            "provider": STORAGE_TYPE_PROVIDERS[storage_type],
            "kind": item.credential.kind,
            "encrypted_secret": encrypt_secret(secret),
            "key_prefix": secret[:32],
            "masked": item.credential.masked or f"{secret[:8]}…{secret[-4:]}",
            "category": item.credential.category or "未分类",
            "status": item.status,
            "result_bucket": bucket,
            "usable": item.usable,
            "model": item.model,
            "http_status": item.http_status,
            "attempts": item.attempts,
            "duration_ms": item.duration_ms,
            "region": item.region,
            "message": item.message,
            "reset_at": item.reset_at,
            "source": "desktop",
            "created_at": now,
            "updated_at": now,
        }
        if source_ip is not None:
            credential_values["source_ip"] = source_ip
        credential_statement = insert(Credential).values(**credential_values)
        credential_statement = credential_statement.on_conflict_do_update(
            constraint="uq_credentials_type_fingerprint",
            set_={key: value for key, value in credential_values.items() if key != "fingerprint"},
        ).returning(Credential.id)
        credential_id = int((await session.execute(credential_statement)).scalar_one())

        if item.status == "valid":
            await remember_valid_key(session, credential_id, observed_at=now)

        item_statement = insert(ValidationBatchItem).values(
            batch_id=batch.id,
            credential_id=credential_id,
            item_index=item.index,
            status=item.status,
            result_bucket=bucket,
        ).on_conflict_do_update(
            constraint="uq_batch_item_index",
            set_={
                "credential_id": credential_id,
                "status": item.status,
                "result_bucket": bucket,
            },
        )
        await session.execute(item_statement)

    counts = (await session.execute(
        select(
            func.count(ValidationBatchItem.id),
            func.sum(case((ValidationBatchItem.result_bucket == "valid", 1), else_=0)),
            func.sum(case((ValidationBatchItem.result_bucket == "usage_limit_reached", 1), else_=0)),
            func.sum(case((ValidationBatchItem.result_bucket == "insufficient_credit", 1), else_=0)),
            func.sum(case((ValidationBatchItem.result_bucket == "invalid", 1), else_=0)),
        ).where(ValidationBatchItem.batch_id == batch.id)
    )).one()
    batch.received_count = int(counts[0] or 0)
    batch.valid_count = int(counts[1] or 0)
    batch.recovery_count = int(counts[2] or 0)
    batch.insufficient_count = int(counts[3] or 0)
    batch.invalid_count = int(counts[4] or 0)
    batch.completed_at = now if batch.received_count >= batch.total else None
    await session.commit()
    return len(results)


async def list_credentials(
    session: AsyncSession,
    *,
    status: str | None,
    provider: str | None,
    storage_type: str | None,
    query: str | None,
    page: int,
    status_mode: str = "bucket",
    created_from: datetime | None = None,
    created_before: datetime | None = None,
) -> dict[str, Any]:
    page = max(1, page)
    prefix = (query or "").strip()[:32]
    if prefix:
        status = "all"
        created_from = created_before = None
    filters = [Credential.status != "pending"]
    provider = scoped_provider(provider, storage_type)
    if provider:
        filters.append(Credential.provider == provider)
    if storage_type:
        filters.append(Credential.storage_type == storage_type)
    if created_from is not None:
        filters.append(Credential.created_at >= created_from)
    if created_before is not None:
        filters.append(Credential.created_at < created_before)

    summary_rows = (await session.execute(
        select(Credential.result_bucket, func.count(Credential.id)).where(*filters).group_by(Credential.result_bucket)
    )).all()
    counts = {bucket: int(count) for bucket, count in summary_rows}
    summary = {
        "total": sum(counts.values()),
        "valid": counts.get("valid", 0),
        "recovery": counts.get("usage_limit_reached", 0),
        "insufficient": counts.get("insufficient_credit", 0),
        "invalid": counts.get("invalid", 0),
    }

    row_filters = list(filters)
    if status and status != "all":
        status_column = Credential.status if status_mode == "exact" else Credential.result_bucket
        row_filters.append(status_column == status)
    if prefix:
        escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        row_filters.append(Credential.key_prefix.like(f"{escaped_prefix}%", escape="\\"))
    total = int(await session.scalar(select(func.count(Credential.id)).where(*row_filters)) or 0)
    page_size = 100
    total_pages = max(1, math.ceil(total / page_size))
    page = min(page, total_pages)
    rows = (await session.scalars(
        select(Credential)
        .where(*row_filters)
        .order_by(Credential.created_at.desc(), Credential.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()
    return {
        "keys": [{
            "id": row.id,
            "keyPrefix": row.key_prefix,
            "provider": row.provider,
            "storageType": row.storage_type,
            "masked": row.masked,
            "status": row.status,
            "createdAt": row.created_at.isoformat(),
            "updatedAt": row.updated_at.isoformat(),
            "ipAddress": row.source_ip,
        } for row in rows],
        "summary": summary,
        "statuses": await list_credential_statuses(
            session, provider=provider, storage_type=storage_type,
            created_from=created_from, created_before=created_before,
        ),
        "total": total,
        "page": page,
        "totalPages": total_pages,
    }


async def reveal_credential(
    session: AsyncSession,
    credential_id: int,
    storage_type: str,
    status: str,
) -> str:
    filters = [
        Credential.id == credential_id,
        Credential.storage_type == storage_type,
        Credential.result_bucket == status,
    ]
    provider = scoped_provider(None, storage_type)
    if provider:
        filters.append(Credential.provider == provider)
    row = await session.scalar(select(Credential).where(*filters))
    if not row:
        raise HTTPException(status_code=404, detail="密钥不存在")
    return decrypt_secret(row.encrypted_secret)


async def reveal_credentials(
    session: AsyncSession,
    keys: list[BatchRevealKeyInput],
) -> list[str]:
    groups: dict[tuple[str, str], list[int]] = {}
    for item in keys:
        if item.type not in VALID_STORAGE_TYPES:
            raise HTTPException(status_code=422, detail="密钥类型无效")
        groups.setdefault((item.type, item.status), []).append(item.id)

    rows = (await session.scalars(select(Credential).where(or_(*[
        and_(
            Credential.provider == STORAGE_TYPE_PROVIDERS[storage_type],
            Credential.storage_type == storage_type,
            Credential.result_bucket == bucket,
            Credential.id.in_(identifiers),
        )
        for (storage_type, bucket), identifiers in groups.items()
    ])))).all()
    matched = {(row.storage_type, row.result_bucket, row.id): row for row in rows}
    identities = [(item.type, item.status, item.id) for item in keys]
    if any(identity not in matched for identity in identities):
        raise HTTPException(status_code=404, detail="所选密钥已变更或不存在，请刷新列表后重试")
    return [decrypt_secret(matched[identity].encrypted_secret) for identity in identities]


async def export_credentials(
    session: AsyncSession,
    *,
    status: str,
    provider: str | None,
    storage_type: str | None,
    status_mode: str = "bucket",
    created_from: datetime | None = None,
    created_before: datetime | None = None,
) -> list[str]:
    filters = []
    provider = scoped_provider(provider, storage_type)
    if status != "all":
        selected_statuses = status.split(",")
        status_column = Credential.status if status_mode == "exact" else Credential.result_bucket
        filters.append(
            status_column.in_(selected_statuses)
            if len(selected_statuses) > 1 else status_column == status
        )
    if provider:
        filters.append(Credential.provider == provider)
    if storage_type:
        filters.append(Credential.storage_type == storage_type)
    if created_from is not None:
        filters.append(Credential.created_at >= created_from)
    if created_before is not None:
        filters.append(Credential.created_at < created_before)
    rows = (await session.scalars(
        select(Credential).where(*filters).order_by(Credential.created_at.desc(), Credential.id.desc())
    )).all()
    return [decrypt_secret(row.encrypted_secret) for row in rows]
