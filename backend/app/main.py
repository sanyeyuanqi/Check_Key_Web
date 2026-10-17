from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import AwareDatetime
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .auth_service import (
    clear_session_cookie,
    create_user,
    current_user,
    ensure_bootstrap_admin,
    list_users,
    login,
    reset_password,
    revoke_session,
    set_session_cookie,
    set_user_active,
    user_json,
)
from .config import settings
from .credential_service import (
    export_credentials,
    list_credentials,
    process_batch_results,
    reveal_credential,
    reveal_credentials,
    start_batch,
)
from .database import AuthUser, SessionFactory, get_session
from .export_service import export_filename, list_export_records, normalize_export_status, record_export
from .ip_access import IPAccessDenied, block_ip, list_ip_blocks, require_allowed_ip, set_ip_block_active
from .queue import enqueue_results, is_burst, redis_client
from .request_context import client_ip
from .schemas import (
    BatchCompleteInput,
    BatchRevealInput,
    BatchStartInput,
    LoginInput,
    IPBlockAction,
    IPBlockInput,
    ManagedUserAction,
    ManagedUserInput,
)
from .security import stable_node_id
from .status_service import list_credential_statuses, normalize_exact_export_status
from .valid_key_service import list_valid_key_records


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with SessionFactory() as session:
        await ensure_bootstrap_admin(session)
    yield


app = FastAPI(
    title=settings.app_name, version="1.0.0", lifespan=lifespan,
    dependencies=[Depends(require_allowed_ip)],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "X-Node-Id"],
    expose_headers=["Content-Disposition"],
)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    body = {"error": str(exc.detail)}
    if isinstance(exc, IPAccessDenied):
        body["code"] = "IP_BLOCKED"
    headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
    return JSONResponse(body, status_code=exc.status_code, headers=headers)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    message = exc.errors()[0].get("msg", "请求格式无效") if exc.errors() else "请求格式无效"
    return JSONResponse({"error": str(message)}, status_code=422)


def ensure_trusted_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in settings.allowed_origins:
        raise HTTPException(status_code=403, detail="请求来源无效")


async def redis_dependency() -> Redis:
    client = redis_client()
    try:
        yield client
    finally:
        await client.aclose()


SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(redis_dependency)]


def created_range_options(
    created_from: datetime | None, created_before: datetime | None,
) -> dict[str, datetime]:
    if created_from is not None and created_before is not None and created_from >= created_before:
        raise HTTPException(status_code=422, detail="开始日期必须早于结束日期")
    return {
        key: value for key, value in {"created_from": created_from, "created_before": created_before}.items()
        if value is not None
    }


async def require_user(request: Request, session: SessionDep) -> AuthUser:
    user = await current_user(session, request.cookies.get(settings.session_cookie_name))
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


async def require_admin(user: Annotated[AuthUser, Depends(require_user)]) -> AuthUser:
    if user.role not in {"admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


async def require_super_admin(user: Annotated[AuthUser, Depends(require_user)]) -> AuthUser:
    if user.role != "super_admin":
        raise HTTPException(status_code=403, detail="需要超级管理员权限")
    return user


def request_node_id(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    raw = request.headers.get("x-node-id") or forwarded or (request.client.host if request.client else "unknown")
    return stable_node_id(raw[:256])


@app.get("/health")
async def health(session: SessionDep, redis: RedisDep) -> dict[str, object]:
    await session.execute(text("SELECT 1"))
    await redis.ping()
    return {"ok": True, "database": "postgresql", "queue": "redis"}


@app.post("/api/auth/login")
async def auth_login(payload: LoginInput, request: Request, session: SessionDep, redis: RedisDep) -> JSONResponse:
    ensure_trusted_origin(request)
    user, token = await login(session, redis, request, payload.username, payload.password)
    response = JSONResponse({"user": user_json(user)})
    set_session_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/access")
async def access_check() -> JSONResponse:
    return JSONResponse({"allowed": True}, headers={"Cache-Control": "no-store"})


@app.get("/api/auth/me")
async def auth_me(user: Annotated[AuthUser, Depends(require_user)]) -> JSONResponse:
    return JSONResponse({"user": user_json(user)}, headers={"Cache-Control": "no-store"})


@app.post("/api/auth/logout")
async def auth_logout(request: Request, session: SessionDep) -> JSONResponse:
    ensure_trusted_origin(request)
    await revoke_session(session, request.cookies.get(settings.session_cookie_name))
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/admin/users")
async def admin_users_list(
    session: SessionDep,
    admin: Annotated[AuthUser, Depends(require_admin)],
) -> JSONResponse:
    return JSONResponse({"users": await list_users(session, admin)}, headers={"Cache-Control": "no-store"})


@app.post("/api/admin/users")
async def admin_users_create(
    payload: ManagedUserInput,
    request: Request,
    session: SessionDep,
    admin: Annotated[AuthUser, Depends(require_admin)],
) -> JSONResponse:
    ensure_trusted_origin(request)
    account = await create_user(session, admin, payload.username, payload.role)
    return JSONResponse({"account": account}, headers={"Cache-Control": "no-store"})


@app.patch("/api/admin/users")
async def admin_users_update(
    payload: ManagedUserAction,
    request: Request,
    session: SessionDep,
    admin: Annotated[AuthUser, Depends(require_admin)],
) -> JSONResponse:
    ensure_trusted_origin(request)
    if payload.action == "reset_password":
        account = await reset_password(session, admin, payload.id)
        return JSONResponse({"account": account}, headers={"Cache-Control": "no-store"})
    if payload.active is None:
        raise HTTPException(status_code=400, detail="账号状态无效")
    account = await set_user_active(session, admin, payload.id, payload.active)
    return JSONResponse({"account": account}, headers={"Cache-Control": "no-store"})


@app.get("/api/admin/ip-blocks")
async def admin_ip_blocks_list(
    request: Request,
    session: SessionDep,
    _: Annotated[AuthUser, Depends(require_super_admin)],
) -> JSONResponse:
    return JSONResponse(
        {"blocks": await list_ip_blocks(session), "clientIp": client_ip(request)},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/admin/ip-blocks")
async def admin_ip_blocks_create(
    payload: IPBlockInput,
    request: Request,
    session: SessionDep,
    admin: Annotated[AuthUser, Depends(require_super_admin)],
) -> JSONResponse:
    ensure_trusted_origin(request)
    block = await block_ip(session, admin, payload.ip, payload.reason, client_ip(request))
    return JSONResponse({"block": block}, headers={"Cache-Control": "no-store"})


@app.patch("/api/admin/ip-blocks")
async def admin_ip_blocks_update(
    payload: IPBlockAction,
    request: Request,
    session: SessionDep,
    admin: Annotated[AuthUser, Depends(require_super_admin)],
) -> JSONResponse:
    ensure_trusted_origin(request)
    block = await set_ip_block_active(session, admin, payload.ip, payload.active, client_ip(request))
    return JSONResponse({"block": block}, headers={"Cache-Control": "no-store"})


@app.get("/api/keys")
async def keys_list(
    session: SessionDep,
    _: Annotated[AuthUser, Depends(require_user)],
    status: str | None = None,
    provider: str | None = None,
    storage_type: Annotated[str | None, Query(alias="type")] = None,
    q: str | None = None,
    page: int = 1,
    status_mode: Literal["bucket", "exact"] = "bucket",
    created_from: AwareDatetime | None = None,
    created_before: AwareDatetime | None = None,
) -> dict[str, object]:
    prefix_search = bool(q and q.strip())
    date_options = {} if prefix_search else created_range_options(created_from, created_before)
    return await list_credentials(
        session,
        status="all" if prefix_search else status,
        provider=provider,
        storage_type=storage_type,
        query=q,
        page=page,
        status_mode=status_mode,
        **date_options,
    )


@app.get("/api/credential-statuses")
async def credential_statuses_list(
    session: SessionDep,
    _: Annotated[AuthUser, Depends(require_user)],
    provider: str | None = None,
    storage_type: Annotated[str | None, Query(alias="type")] = None,
    created_from: AwareDatetime | None = None,
    created_before: AwareDatetime | None = None,
) -> JSONResponse:
    statuses = await list_credential_statuses(
        session, provider=provider, storage_type=storage_type,
        **created_range_options(created_from, created_before),
    )
    return JSONResponse({"statuses": statuses}, headers={"Cache-Control": "no-store"})


@app.post("/api/keys/batch-reveal")
async def keys_batch_reveal(
    payload: BatchRevealInput,
    request: Request,
    session: SessionDep,
    _: Annotated[AuthUser, Depends(require_user)],
) -> JSONResponse:
    ensure_trusted_origin(request)
    secrets = await reveal_credentials(session, payload.keys)
    return JSONResponse({"secrets": secrets}, headers={"Cache-Control": "no-store"})


@app.get("/api/keys/{credential_id}/reveal")
async def key_reveal(
    credential_id: int,
    session: SessionDep,
    _: Annotated[AuthUser, Depends(require_user)],
    storage_type: Annotated[str, Query(alias="type")],
    status: str,
) -> dict[str, str]:
    return {"secret": await reveal_credential(session, credential_id, storage_type, status)}


@app.get("/api/valid-key-records")
async def valid_key_records_list(
    session: SessionDep,
    _: Annotated[AuthUser, Depends(require_user)],
    provider: Annotated[str | None, Query(max_length=80)] = None,
    storage_type: Annotated[str | None, Query(alias="type", max_length=64)] = None,
    q: str | None = None,
    ip: Annotated[str | None, Query(max_length=80)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> JSONResponse:
    ip_options = {"ip": ip} if ip is not None else {}
    body = await list_valid_key_records(session, provider=provider, storage_type=storage_type, query=q, page=page, **ip_options)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@app.get("/api/export")
async def keys_export(
    request: Request,
    session: SessionDep,
    user: Annotated[AuthUser, Depends(require_user)],
    status: Annotated[str, Query(max_length=8192)],
    provider: Annotated[str | None, Query(max_length=80)] = None,
    storage_type: Annotated[str | None, Query(alias="type", max_length=64)] = None,
    status_mode: Literal["bucket", "exact"] = "bucket",
    created_from: AwareDatetime | None = None,
    created_before: AwareDatetime | None = None,
) -> PlainTextResponse:
    date_options = created_range_options(created_from, created_before)
    try:
        status = await normalize_exact_export_status(session, status) if status_mode == "exact" else normalize_export_status(status)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    values = await export_credentials(
        session,
        status=status,
        provider=provider,
        storage_type=storage_type,
        **({"status_mode": "exact"} if status_mode == "exact" else {}),
        **date_options,
    )
    file_name = export_filename(provider, status, **date_options)
    content = "\n".join(values)
    await record_export(
        session, user=user, provider=provider, storage_type=storage_type,
        status=status, count=len(values), file_name=file_name,
        ip_address=client_ip(request),
        **({"status_mode": "exact"} if status_mode == "exact" else {}),
    )
    return PlainTextResponse(
        content,
        headers={
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/export-records")
async def export_records_list(
    session: SessionDep,
    user: Annotated[AuthUser, Depends(require_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    username: Annotated[str | None, Query(max_length=32)] = None,
    ip: Annotated[str | None, Query(max_length=80)] = None,
) -> JSONResponse:
    filter_options = {}
    if username is not None:
        filter_options["username"] = username
    if ip is not None:
        filter_options["ip"] = ip
    body = await list_export_records(session, user=user, page=page, **filter_options)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@app.get("/api/device/ping")
async def device_ping(session: SessionDep, redis: RedisDep) -> dict[str, object]:
    await session.execute(text("SELECT 1"))
    await redis.ping()
    return {"ok": True, "authorization": "not_required", "queue": "redis"}


@app.post("/api/device/batches/start")
async def device_batch_start(payload: BatchStartInput, request: Request, session: SessionDep) -> dict[str, object]:
    batch_id = await start_batch(session, request_node_id(request), payload.provider_label, payload.total)
    return {"batch_id": str(batch_id), "accepted": 0}


@app.post("/api/device/batches/{batch_id}/complete")
async def device_batch_complete(
    batch_id: int,
    payload: BatchCompleteInput,
    request: Request,
    session: SessionDep,
    redis: RedisDep,
) -> JSONResponse:
    node_id = request_node_id(request)
    source_ip = client_ip(request)
    if await is_burst(redis, node_id):
        job_id = await enqueue_results(redis, batch_id, [item.model_dump() for item in payload.results], source_ip=source_ip)
        return JSONResponse(
            {"ok": True, "accepted": len(payload.results), "queued": True, "job_id": job_id},
            status_code=202,
        )
    accepted = await process_batch_results(session, batch_id, payload.results, source_ip=source_ip)
    return JSONResponse({"ok": True, "accepted": accepted, "queued": False})
