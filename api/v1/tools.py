"""REST endpoints for MCP tool filtering per agent."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db
from mcp_clients.manager import MCPManager
from models.agent import AgentCredentials
from schemas.tools import ToolInfo, UpdateEnabledToolsRequest
from services.agent_service import AgentService

router = APIRouter()
agent_service = AgentService()
DbSession = Annotated[AsyncSession, Depends(get_db)]
UserIdHeader = Annotated[UUID, Header(alias="x-user-id")]


async def _get_owned_credentials(
    db: AsyncSession,
    agent_id: UUID,
    service_name: str,
    user_id: UUID,
) -> AgentCredentials:
    agent = await agent_service.get_agent_by_id(db, agent_id, user_id=user_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    credentials = await db.get(
        AgentCredentials,
        {"agent_id": agent_id, "service_name": service_name},
    )
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credentials not found")
    return credentials


async def _build_tool_list(credentials: AgentCredentials) -> list[ToolInfo]:
    async with MCPManager(
        service_name=credentials.service_name,
        credentials=credentials.credentials,
    ) as manager:
        openai_tools = manager.get_openai_tools()
        metadata = await manager.get_tools_metadata()

    enabled_tools = credentials.enabled_tools
    result: list[ToolInfo] = []
    for t in openai_tools:
        name = t["function"]["name"]
        meta = metadata.get(name, {})
        cat = meta.get("category", "other")
        tier = meta.get("tier", "core")

        if enabled_tools is None:
            enabled = tier == "core"
        elif "*" in enabled_tools:
            enabled = True
        else:
            enabled = name in enabled_tools

        result.append(ToolInfo(
            name=name,
            description=t["function"].get("description", ""),
            enabled=enabled,
            category=cat,
            tier=tier,
        ))
    return result


@router.get(
    "/{agent_id}/tools/{service_name}",
    response_model=list[ToolInfo],
)
async def get_agent_tools(
    agent_id: UUID,
    service_name: str,
    db: DbSession,
    user_id: UserIdHeader,
) -> list[ToolInfo]:
    credentials = await _get_owned_credentials(db, agent_id, service_name, user_id)
    return await _build_tool_list(credentials)


@router.put(
    "/{agent_id}/tools/{service_name}",
    response_model=list[ToolInfo],
)
async def update_agent_tools(
    agent_id: UUID,
    service_name: str,
    payload: UpdateEnabledToolsRequest,
    db: DbSession,
    user_id: UserIdHeader,
) -> list[ToolInfo]:
    credentials = await _get_owned_credentials(db, agent_id, service_name, user_id)
    credentials.enabled_tools = payload.enabled_tools
    await db.commit()
    await db.refresh(credentials)
    return await _build_tool_list(credentials)
