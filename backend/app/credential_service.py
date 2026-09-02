import math
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Credential, ValidationBatch, ValidationBatchItem
from .schemas import ValidationResultInput
from .security import decrypt_secret, encrypt_secret, fingerprint

VALID_STORAGE_TYPES = {
    "anthropic_official",
    "aws_bedrock_api",
    "aws_access",
    "openai_official",
    "openrouter",
    "azure_gpt",
    "azure_claude",
    "google_ai_studio_gemini",
    "google_vertex_gemini",
    "google_vertex_claude",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def result_bucket(status: str) -> str:
    if status in {"valid", "usage_limit_reached", "insufficient_credit"}:
        return status
    return "invalid"


def infer_storage_type(provider: str, kind: str, model: str | None = None) -> str:
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
) -> int:
    batch = await session.scalar(
        select(ValidationBatch).where(ValidationBatch.id == batch_id).with_for_update()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="检测批次不存在")

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
            "provider": item.credential.provider,
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
        credential_statement = insert(Credential).values(**credential_values)
        credential_statement = credential_statement.on_conflict_do_update(
            constraint="uq_credentials_type_fingerprint",
            set_={key: value for key, value in credential_values.items() if key != "fingerprint"},
        ).returning(Credential.id)
        credential_id = int((await session.execute(credential_statement)).scalar_one())

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
) -> dict[str, Any]:
    page = max(1, page)
    filters = []
    if provider:
        filters.append(Credential.provider == provider)
    if storage_type:
        filters.append(Credential.storage_type == storage_type)

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
    if status:
        row_filters.append(Credential.result_bucket == status)
    if query:
        escaped_prefix = query.strip()[:32].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
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
            "masked": row.masked,
            "status": row.status,
            "createdAt": row.created_at.isoformat(),
        } for row in rows],
        "summary": summary,
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
    row = await session.scalar(select(Credential).where(
        Credential.id == credential_id,
        Credential.storage_type == storage_type,
        Credential.result_bucket == status,
    ))
    if not row:
        raise HTTPException(status_code=404, detail="密钥不存在")
    return decrypt_secret(row.encrypted_secret)


async def export_credentials(
    session: AsyncSession,
    *,
    status: str,
    provider: str | None,
    storage_type: str | None,
) -> list[str]:
    filters = []
    if status != "all":
        filters.append(Credential.result_bucket == status)
    if provider:
        filters.append(Credential.provider == provider)
    if storage_type:
        filters.append(Credential.storage_type == storage_type)
    rows = (await session.scalars(
        select(Credential).where(*filters).order_by(Credential.created_at.desc(), Credential.id.desc())
    )).all()
    return [decrypt_secret(row.encrypted_secret) for row in rows]
