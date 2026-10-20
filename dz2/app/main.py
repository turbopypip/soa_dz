import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .errors import ApiError
from .security import authenticate, bootstrap_admin
from .validation import SPEC, validate_body
from .generated.routes import router
from .generated.models import ApiError as ErrorResponse

logger = logging.getLogger("marketplace.requests")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(handler)
logger.propagate = False


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(bootstrap_admin)
    yield


app = FastAPI(title="Marketplace", lifespan=lifespan)
app.include_router(router)
app.openapi = lambda: SPEC


def error_response(status, code, message, details=None):
    body = ErrorResponse(error_code=code, message=message, details=details)
    return JSONResponse(
        status_code=status, content=body.model_dump(mode="json", exclude_none=True)
    )


@app.exception_handler(ApiError)
async def business_error(request, exc):
    return error_response(exc.status, exc.code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return error_response(
        400,
        "VALIDATION_ERROR",
        "Invalid request",
        {
            "violations": [
                {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                for e in exc.errors()
            ]
        },
    )


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return error_response(exc.status_code, "VALIDATION_ERROR", str(exc.detail))


def redact(value):
    if isinstance(value, dict):
        return {
            k: (
                "***"
                if any(
                    s in k.lower()
                    for s in ("password", "token", "secret", "authorization")
                )
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


@app.middleware("http")
async def request_context(request: Request, call_next):
    start = time.perf_counter()
    timestamp = datetime.now(timezone.utc).isoformat()
    request_id = str(uuid4())
    request.state.user = None
    raw = await request.body()
    body_log = None
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        try:
            body_log = redact(json.loads(raw)) if raw else None
        except (ValueError, UnicodeDecodeError):
            body_log = "<invalid JSON omitted>"
    try:
        if not (
            request.url.path.startswith("/auth/")
            or request.url.path
            in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")
        ):
            request.state.user = await run_in_threadpool(authenticate, request)
        validate_body(request.url.path, request.method, raw)
        response = await call_next(request)
    except ApiError as exc:
        response = error_response(exc.status, exc.code, exc.message, exc.details)
    except Exception:
        # Do not expose exception text (which can contain SQL parameters or secrets).
        response = error_response(500, "INTERNAL_ERROR", "Internal server error")
    response.headers["X-Request-Id"] = request_id
    event = {
        "request_id": request_id,
        "method": request.method,
        "endpoint": request.url.path,
        "status_code": response.status_code,
        "duration_ms": round((time.perf_counter() - start) * 1000, 3),
        "user_id": str(request.state.user["id"]) if request.state.user else None,
        "timestamp": timestamp,
    }
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        event["request_body"] = body_log
    logger.info(json.dumps(event, ensure_ascii=False))
    return response
