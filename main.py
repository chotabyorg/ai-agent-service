"""FastAPI entrypoint for ai-agent-service."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.internal.agents import router as internal_agents_router
from api.v1.agents import router as agents_router
from api.v1.chat import router as chat_router
from api.v1.mcp_services import router as mcp_services_router
from api.v1.models import router as models_router
from api.v1.openrouter_key import router as openrouter_key_router
from api.v1.tools import router as tools_router
from core.config import get_settings
from core.redis import close_redis, init_redis
from services.subscription_client import SubscriptionClient


settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()
    if settings.subscription_check_enabled and settings.subscription_service_url:
        app.state.subscription_client = SubscriptionClient(
            settings.subscription_service_url
        )
    else:
        app.state.subscription_client = None
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
    models_router,
    prefix=settings.api_v1_prefix,
    tags=["models"],
)
app.include_router(
    tools_router,
    prefix=f"{settings.api_v1_prefix}/agents",
    tags=["tools"],
)
app.include_router(
    openrouter_key_router,
    prefix=f"{settings.api_v1_prefix}/agents",
    tags=["openrouter-key"],
)
app.include_router(
    internal_agents_router,
    prefix="/internal/v1",
)


@app.get("/health", tags=["health"])
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
