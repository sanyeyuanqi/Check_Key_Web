from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
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
    start_batch,
)
from .database import AuthUser, SessionFactory, get_session
from .queue import enqueue_results, is_burst, redis_client
from .schemas import (
    BatchCompleteInput,
    BatchStartInput,
    LoginInput,
    ManagedUserAction,
    ManagedUserInput,
)
from .security import stable_node_id


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with SessionFactory() as session:
        await ensure_bootstrap_admin(session)
    yield


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "X-Node-Id"],
)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)


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


async def require_user(request: Request, session: SessionDep) -> AuthUser:
    user = await current_user(session, request.cookies.get(settings.session_cookie_name))
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


async def require_admin(user: Annotated[AuthUser, Depends(require_user)]) -> AuthUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
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


@app.get("/api/auth/me")
async def auth_me(user: Annotated[AuthUser, Depends(require_user)]) -> dict[str, object]:
    return {"user": user_json(user)}


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
    _: Annotated[AuthUser, Depends(require_admin)],
) -> dict[str, object]:
    return {"users": await list_users(session)}


@app.post("/api/admin/users")
async def admin_users_create(
    payload: ManagedUserInput,
    request: Request,
    session: SessionDep,
    admin: Annotated[AuthUser, Depends(require_admin)],
) -> dict[str, object]:
    ensure_trusted_origin(request)
    return {"account": await create_user(session, admin, payload.username, payload.role)}


@app.patch("/api/admin/users")
async def admin_users_update(
    payload: ManagedUserAction,
    request: Request,
    session: SessionDep,
    admin: Annotated[AuthUser, Depends(require_admin)],
) -> dict[str, object]:
    ensure_trusted_origin(request)
    if payload.action == "reset_password":
        return {"account": await reset_password(session, admin, payload.id)}
    if payload.active is None:
        raise HTTPException(status_code=400, detail="账号状态无效")
    return {"account": await set_user_active(session, payload.id, payload.active)}


@app.get("/api/keys")
async def keys_list(
    session: SessionDep,
    _: Annotated[AuthUser, Depends(require_user)],
    status: str | None = None,
    provider: str | None = None,
    storage_type: Annotated[str | None, Query(alias="type")] = None,
    q: str | None = None,
    page: int = 1,
) -> dict[str, object]:
    return await list_credentials(
        session,
        status=status,
        provider=provider,
        storage_type=storage_type,
        query=q,
        page=page,
    )


@app.get("/api/keys/{credential_id}/reveal")
async def key_reveal(
    credential_id: int,
    session: SessionDep,
    _: Annotated[AuthUser, Depends(require_user)],
    storage_type: Annotated[str, Query(alias="type")],
    status: str,
) -> dict[str, str]:
    return {"secret": await reveal_credential(session, credential_id, storage_type, status)}


@app.get("/api/export")
async def keys_export(
    session: SessionDep,
    _: Annotated[AuthUser, Depends(require_user)],
    status: str,
    provider: str | None = None,
    storage_type: Annotated[str | None, Query(alias="type")] = None,
) -> PlainTextResponse:
    if status not in {"all", "valid", "usage_limit_reached", "insufficient_credit", "invalid"}:
        raise HTTPException(status_code=400, detail="不支持的导出状态")
    values = await export_credentials(
        session,
        status=status,
        provider=provider,
        storage_type=storage_type,
    )
    filenames = {
        "all": "all-keys.txt",
        "valid": "all-valid-keys.txt",
        "usage_limit_reached": "all-usage-limit-reached-keys.txt",
        "insufficient_credit": "all-insufficient-credit-keys.txt",
        "invalid": "all-invalid-keys.txt",
    }
    return PlainTextResponse(
        "\n".join(values),
        headers={
            "Content-Disposition": f'attachment; filename="{filenames[status]}"',
            "Cache-Control": "no-store",
        },
    )


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
    if await is_burst(redis, node_id):
        job_id = await enqueue_results(redis, batch_id, [item.model_dump() for item in payload.results])
        return JSONResponse(
            {"ok": True, "accepted": len(payload.results), "queued": True, "job_id": job_id},
            status_code=202,
        )
    accepted = await process_batch_results(session, batch_id, payload.results)
    return JSONResponse({"ok": True, "accepted": accepted, "queued": False})
