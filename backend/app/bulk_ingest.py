"""Transactional, replay-safe bulk ingestion of encrypted queue results."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import BigInteger, and_, case, cast, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .credential_service import VALID_STORAGE_TYPES, infer_storage_type, result_bucket
from .database import Credential, CredentialRegistry, ValidationBatch, ValidationBatchItem, ValidKeyRecord
from .platform_catalog import STORAGE_TYPE_PROVIDERS
from .query_cache import invalidate_query_cache
from .schemas import ValidationResultInput
from .security import encrypt_secret, fingerprint
from .status_service import ensure_credential_statuses


_STREAM_ID = re.compile(r"([0-9]{1,20})-([0-9]{1,20})\Z")
_MAX_STREAM_NUMBER = 2**64 - 1
_SQL_CHUNK_SIZE = 1000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class QueuedResultJob:
    message_id: str
    batch_id: int
    results: list[ValidationResultInput] = field(repr=False)
    source_ip: str | None
    observed_at: datetime


@dataclass(frozen=True)
class _Observation:
    item: ValidationResultInput = field(repr=False)
    storage_type: str
    provider: str
    fingerprint: str
    event_key: str
    observed_at: datetime
    batch_id: int
    source_ip: str | None

    @property
    def identity(self) -> tuple[str, str]:
        return self.storage_type, self.fingerprint


def _stream_parts(message_id: str) -> tuple[int, int]:
    matched = _STREAM_ID.fullmatch(message_id) if isinstance(message_id, str) else None
    if matched is None:
        raise ValueError("Invalid queue message ID")
    parts = int(matched[1]), int(matched[2])
    if any(value > _MAX_STREAM_NUMBER for value in parts):
        raise ValueError("Invalid queue message ID")
    return parts


def queue_event_key(message_id: str) -> str:
    milliseconds, sequence = _stream_parts(message_id)
    return f"{milliseconds:020d}-{sequence:020d}"


def queue_observed_at(message_id: str) -> datetime:
    milliseconds, _ = _stream_parts(message_id)
    try:
        return _EPOCH + timedelta(milliseconds=milliseconds)
    except OverflowError:
        raise ValueError("Queue message time is outside the supported range") from None


def _chunks(rows: list[Any]):
    for start in range(0, len(rows), _SQL_CHUNK_SIZE):
        yield rows[start:start + _SQL_CHUNK_SIZE]


def _observations(jobs: list[QueuedResultJob]) -> list[_Observation]:
    observations = []
    for job in jobs:
        if type(job.batch_id) is not int or job.batch_id < 1:
            raise ValueError("Invalid queue batch ID")
        if not isinstance(job.observed_at, datetime) or job.observed_at.tzinfo is None or job.observed_at.utcoffset() is None:
            raise ValueError("Queue observation time must include a timezone")
        event_key = queue_event_key(job.message_id)
        # Stream IDs supply the authoritative event clock, including reclaims.
        observed_at = queue_observed_at(job.message_id)
        for item in job.results:
            requested_type = item.credential.requested_storage_type
            storage_type = requested_type if requested_type in VALID_STORAGE_TYPES else infer_storage_type(
                item.credential.provider, item.credential.kind, item.model,
            )
            observations.append(_Observation(
                item=item, storage_type=storage_type, provider=STORAGE_TYPE_PROVIDERS[storage_type],
                fingerprint=fingerprint(item.credential.secret), event_key=event_key,
                observed_at=observed_at, batch_id=job.batch_id, source_ip=job.source_ip,
            ))
    return observations


def _latest(observations: list[_Observation], identity) -> dict[Any, _Observation]:
    latest = {}
    for observation in observations:
        key = identity(observation)
        previous = latest.get(key)
        if previous is None or observation.event_key >= previous.event_key:
            latest[key] = observation
    return latest


def _credential_rows(observations: list[_Observation]) -> tuple[list[dict[str, Any]], dict[tuple[str, str], _Observation]]:
    latest = _latest(observations, lambda observation: observation.identity)
    latest_ips = _latest([row for row in observations if row.source_ip is not None], lambda observation: observation.identity)
    rows = []
    for identity, observation in sorted(latest.items(), key=lambda pair: (pair[1].provider, *pair[0])):
        item = observation.item
        secret = item.credential.secret
        known_ip = latest_ips.get(identity)
        rows.append({
            "fingerprint": observation.fingerprint,
            "storage_type": observation.storage_type,
            "provider": observation.provider,
            "kind": item.credential.kind,
            "encrypted_secret": encrypt_secret(secret),
            "key_prefix": secret[:32],
            "masked": item.credential.masked or f"{secret[:8]}…{secret[-4:]}",
            "category": item.credential.category or "未分类",
            "status": item.status,
            "result_bucket": result_bucket(item.status),
            "usable": item.usable,
            "model": item.model,
            "http_status": item.http_status,
            "attempts": item.attempts,
            "duration_ms": item.duration_ms,
            "region": item.region,
            "message": item.message,
            "reset_at": item.reset_at,
            "source": "desktop",
            "source_ip": known_ip.source_ip if known_ip is not None else None,
            "created_at": observation.observed_at,
            "updated_at": observation.observed_at,
            "queue_event_id": observation.event_key,
        })
    return rows, latest_ips


def _credential_upsert(rows: list[dict[str, Any]], latest_ips: dict[tuple[str, str], _Observation]):
    statement = insert(Credential).values(rows)
    excluded = statement.excluded
    source_ip = func.coalesce(excluded.source_ip, Credential.source_ip)
    preserve_ip = []
    for row in rows:
        known_ip = latest_ips.get((row["storage_type"], row["fingerprint"]))
        if known_ip is not None and known_ip.event_key != row["queue_event_id"]:
            # A group may include an older known IP and a newer legacy job
            # without one. Keep the known IP unless the DB already has a more
            # recent address; an intervening observation must not be undone.
            preserve_ip.append((and_(
                excluded.storage_type == row["storage_type"],
                excluded.fingerprint == row["fingerprint"],
                Credential.source_ip.is_not(None),
                or_(
                    Credential.queue_event_id > known_ip.event_key,
                    Credential.updated_at > known_ip.observed_at,
                ),
            ), Credential.source_ip))
    if preserve_ip:
        source_ip = case(*preserve_ip, else_=source_ip)
    changes = {
        name: getattr(excluded, name)
        for name in rows[0] if name not in {"fingerprint", "storage_type", "provider", "source_ip"}
    }
    changes["source_ip"] = source_ip
    return statement.on_conflict_do_update(
        constraint="uq_credentials_type_fingerprint",
        set_=changes,
        where=and_(
            or_(Credential.queue_event_id.is_(None), Credential.queue_event_id < excluded.queue_event_id),
            Credential.updated_at <= excluded.updated_at,
        ),
    )


async def _registry_ids(session: AsyncSession, identities: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    identifiers = {}
    for chunk in _chunks(sorted(identities)):
        result = await session.execute(select(
            CredentialRegistry.storage_type, CredentialRegistry.fingerprint, CredentialRegistry.id,
        ).where(tuple_(CredentialRegistry.storage_type, CredentialRegistry.fingerprint).in_(chunk)))
        identifiers.update({(storage_type, key_fingerprint): identifier for storage_type, key_fingerprint, identifier in result.all()})
    if len(identifiers) != len(identities):
        raise RuntimeError("Queued credential registry is incomplete")
    return identifiers


async def _remember_valid(session: AsyncSession, observations: list[_Observation], identifiers: dict[tuple[str, str], int]) -> None:
    valid = {}
    for observation in observations:
        if observation.item.status != "valid":
            continue
        identifier = identifiers[observation.identity]
        previous = valid.get(identifier)
        if previous is None:
            valid[identifier] = observation.observed_at, observation.observed_at
        else:
            valid[identifier] = min(previous[0], observation.observed_at), max(previous[1], observation.observed_at)
    for chunk in _chunks([
        {"credential_id": identifier, "first_valid_at": first, "last_valid_at": last}
        for identifier, (first, last) in sorted(valid.items())
    ]):
        statement = insert(ValidKeyRecord).values(chunk)
        await session.execute(statement.on_conflict_do_update(
            index_elements=[ValidKeyRecord.credential_id],
            set_={
                "first_valid_at": func.least(ValidKeyRecord.first_valid_at, statement.excluded.first_valid_at),
                "last_valid_at": func.greatest(ValidKeyRecord.last_valid_at, statement.excluded.last_valid_at),
            },
        ))


async def _write_batch_items(session: AsyncSession, observations: list[_Observation], identifiers: dict[tuple[str, str], int]) -> None:
    latest = _latest(observations, lambda observation: (observation.batch_id, observation.item.index))
    for chunk in _chunks([
        {
            "batch_id": batch_id, "credential_id": identifiers[observation.identity], "item_index": item_index,
            "status": observation.item.status, "result_bucket": result_bucket(observation.item.status),
            "queue_event_id": observation.event_key,
        }
        for (batch_id, item_index), observation in sorted(latest.items())
    ]):
        statement = insert(ValidationBatchItem).values(chunk)
        await session.execute(statement.on_conflict_do_update(
            constraint="uq_batch_item_index",
            set_={name: getattr(statement.excluded, name) for name in ("credential_id", "status", "result_bucket", "queue_event_id")},
            where=or_(ValidationBatchItem.queue_event_id.is_(None), ValidationBatchItem.queue_event_id < statement.excluded.queue_event_id),
        ))


async def _update_batch_summaries(session: AsyncSession, batch_ids: list[int]) -> None:
    counts = select(
        ValidationBatchItem.batch_id,
        func.count(ValidationBatchItem.id).label("received_count"),
        *[
            func.sum(case((ValidationBatchItem.result_bucket == bucket, 1), else_=0)).label(name)
            for bucket, name in (
                ("valid", "valid_count"), ("usage_limit_reached", "recovery_count"),
                ("insufficient_credit", "insufficient_count"), ("invalid", "invalid_count"),
            )
        ],
        func.max(ValidationBatchItem.queue_event_id).label("latest_event"),
    ).where(ValidationBatchItem.batch_id.in_(batch_ids)).group_by(ValidationBatchItem.batch_id).subquery()
    latest_observed = func.to_timestamp(cast(func.split_part(counts.c.latest_event, "-", 1), BigInteger) / 1000.0)
    await session.execute(update(ValidationBatch).where(ValidationBatch.id == counts.c.batch_id).values(
        **{name: getattr(counts.c, name) for name in (
            "received_count", "valid_count", "recovery_count", "insufficient_count", "invalid_count",
        )},
        completed_at=case((
            counts.c.received_count >= ValidationBatch.total,
            func.coalesce(ValidationBatch.completed_at, latest_observed, ValidationBatch.started_at),
        ), else_=None),
    ).execution_options(synchronize_session=False))


async def process_queued_results(session: AsyncSession, jobs: list[QueuedResultJob]) -> int:
    """Consume all supplied records atomically; returned count includes replays."""
    if not jobs:
        return 0
    try:
        observations = _observations(jobs)
        batch_ids = sorted({job.batch_id for job in jobs})
        locked = (await session.scalars(select(ValidationBatch.id).where(
            ValidationBatch.id.in_(batch_ids),
        ).order_by(ValidationBatch.id).with_for_update())).all()
        if set(locked) != set(batch_ids):
            raise HTTPException(status_code=404, detail="检测批次不存在")

        if observations:
            for statuses in _chunks(sorted({row.item.status for row in observations})):
                await ensure_credential_statuses(session, statuses)
            rows, latest_ips = _credential_rows(observations)
            for chunk in _chunks(rows):
                await session.execute(_credential_upsert(chunk, latest_ips))
            identifiers = await _registry_ids(session, [(row["storage_type"], row["fingerprint"]) for row in rows])
            await _remember_valid(session, observations, identifiers)
            await _write_batch_items(session, observations, identifiers)
            await _update_batch_summaries(session, batch_ids)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    await invalidate_query_cache("credentials")
    return len(observations)
