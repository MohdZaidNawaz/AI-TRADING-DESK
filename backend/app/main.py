import re
from uuid import uuid4

from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router


_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


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
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Accept", "Content-Type", "X-Correlation-ID"],
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
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    application.include_router(api_router, prefix="/api")
    return application


app = create_app()
