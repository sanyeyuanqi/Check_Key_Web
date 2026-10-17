from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Credential
from .platform_catalog import scoped_provider
from .query_cache import CacheResult, cached_query
from .status_service import list_credential_statuses


async def list_credential_statistics(
    session: AsyncSession, *, provider: str | None = None, storage_type: str | None = None,
    created_from: datetime | None = None, created_before: datetime | None = None,
    refresh: bool = False,
) -> CacheResult:
    provider = scoped_provider(provider, storage_type)
    date_options = {
        key: value for key, value in {"created_from": created_from, "created_before": created_before}.items()
        if value is not None
    }

    async def load():
        filters = [Credential.status != "pending"]
        if provider:
            filters.append(Credential.provider == provider)
        if storage_type:
            filters.append(Credential.storage_type == storage_type)
        if created_from is not None:
            filters.append(Credential.created_at >= created_from)
        if created_before is not None:
            filters.append(Credential.created_at < created_before)
        rows = (await session.execute(
            select(Credential.result_bucket, func.count(Credential.id)).where(*filters).group_by(Credential.result_bucket)
        )).all()
        counts = {bucket: int(count) for bucket, count in rows}
        return {
            "summary": {
                "total": sum(counts.values()),
                "valid": counts.get("valid", 0),
                "recovery": counts.get("usage_limit_reached", 0),
                "insufficient": counts.get("insufficient_credit", 0),
                "invalid": counts.get("invalid", 0),
            },
            "statuses": await list_credential_statuses(
                session, provider=provider, storage_type=storage_type,
                created_from=created_from, created_before=created_before,
            ),
        }

    return await cached_query("credential-statistics", {
        "provider": provider or None, "type": storage_type or None,
        **{key: value.astimezone(timezone.utc).isoformat() for key, value in date_options.items()},
    }, load, refresh=refresh)
