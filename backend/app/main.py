from ipaddress import ip_address
import re
from uuid import uuid4

from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.router import api_router


_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_LOCAL_FRONTEND_ORIGINS = frozenset(
    {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    }
)
_MAX_DECLARED_REQUEST_BYTES = 1_048_576


class _RequestBodyLimitMiddleware:
    """Bound streamed request bodies even when Content-Length is absent."""

    def __init__(self, app: ASGIApp, *, maximum_bytes: int) -> None:
        self._app = app
        self._maximum_bytes = maximum_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        received = 0
        messages: list[Message] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._maximum_bytes:
                    response = JSONResponse(
                        status_code=413,
                        content={"detail": "Request body is too large."},
                    )
                    await response(scope, receive, send)
                    return
                messages.append(message)
                if not message.get("more_body", False):
                    break
            else:
                messages.append(message)
                break

        position = 0

        async def replay_receive() -> Message:
            nonlocal position
            if position < len(messages):
                message = messages[position]
                position += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self._app(scope, replay_receive, send)


def _is_loopback_client(request: Request) -> bool:
    """Return whether the direct ASGI peer is a loopback address.

    This is a deployment guard, not authentication. A future reverse proxy
    would need its own authenticated boundary because a loopback proxy peer is
    indistinguishable from a local client here.
    """

    if request.client is None:
        return False
    try:
        address = ip_address(request.client.host.split("%", maxsplit=1)[0])
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def _declared_content_length(request: Request) -> int | None:
    value = request.headers.get("content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return -1
    return parsed if parsed >= 0 else -1


def create_app() -> FastAPI:
    application = FastAPI(
        title="Nimbus Desk API",
        description="Local API for the Nimbus Desk interface.",
        version="0.2.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(_LOCAL_FRONTEND_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Accept", "Content-Type", "X-Correlation-ID"],
    )
    application.add_middleware(
        _RequestBodyLimitMiddleware,
        maximum_bytes=_MAX_DECLARED_REQUEST_BYTES,
    )

    @application.middleware("http")
    async def attach_correlation_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        supplied = request.headers.get("X-Correlation-ID", "")
        correlation_id = (
            supplied
            if _CORRELATION_ID.fullmatch(supplied)
            else f"req-{uuid4().hex}"
        )
        request.state.correlation_id = correlation_id

        if not _is_loopback_client(request):
            response = JSONResponse(
                status_code=403,
                content={"detail": "Nimbus Desk API is restricted to local access."},
            )
        elif (
            (origin := request.headers.get("origin")) is not None
            and origin not in _LOCAL_FRONTEND_ORIGINS
        ):
            response = JSONResponse(
                status_code=403,
                content={"detail": "Browser origin is not allowed."},
            )
        else:
            content_length = _declared_content_length(request)
            if content_length == -1:
                response = JSONResponse(
                    status_code=400,
                    content={"detail": "Content-Length header is invalid."},
                )
            elif (
                content_length is not None
                and content_length > _MAX_DECLARED_REQUEST_BYTES
            ):
                response = JSONResponse(
                    status_code=413,
                    content={"detail": "Request body is too large."},
                )
            else:
                response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    application.include_router(api_router, prefix="/api")
    return application


app = create_app()
