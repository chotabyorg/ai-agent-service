"""FastAPI entrypoint for ai-agent-service."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.v1.agents import router as agents_router
from api.v1.chat import router as chat_router
from api.v1.mcp_services import router as mcp_services_router
from api.v1.static import router as static_router
from core.config import get_settings
from core.redis import close_redis, init_redis


settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_redis()
    try:
        yield
    finally:
        await close_redis()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

app.include_router(
    agents_router,
    prefix=f"{settings.api_v1_prefix}/agents",
    tags=["agents"],
)
app.include_router(
    chat_router,
    prefix=f"{settings.api_v1_prefix}/chat",
    tags=["chat"],
)
app.include_router(
    mcp_services_router,
    prefix=settings.api_v1_prefix,
    tags=["mcp-services"],
)
app.include_router(
    static_router,
    prefix=f"{settings.api_v1_prefix}/static",
    tags=["static"],
)


@app.get("/health", tags=["health"])
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
