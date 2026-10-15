"""AI-owned live ownership proof for enrolled Handoff Telegram scopes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db
from core.config import get_settings
from core.managed_boundary import boundary_error
from models.agent import AgentTemplate
from schemas.handoff import AgentProofResponse
from services.handoff_lifecycle import HandoffLifecycleConflict, record_proven_scope

router = APIRouter(tags=["internal-handoff-lifecycle"])
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("/handoff/agent-proof", response_model=AgentProofResponse)
async def prove_handoff_agent(request: Request, db: DbSession):
    value = request.scope["agent_proof_request"]
    owner_id = request.scope["agent_proof_owner_id"]
    agent = await db.scalar(select(AgentTemplate).where(
        AgentTemplate.id == value.agent_id,
        AgentTemplate.user_id == owner_id,
    ).with_for_update())
    if agent is None:
        return boundary_error(404, "AGENT_NOT_FOUND")
    if (not agent.is_active or agent.handoff_lifecycle_state != "STABLE"
            or agent.handoff_lifecycle_generation < 1):
        return boundary_error(409, "EXECUTION_CONFLICT")
    identity = (value.owner_id, value.integration_id, value.channel, value.agent_id)
    scope = next((item for item in get_settings().handoff_scopes if item.key() == identity), None)
    if scope is None:
        return boundary_error(404, "AGENT_NOT_FOUND")
    try:
        await record_proven_scope(agent, scope, owner_id, db)
    except HandoffLifecycleConflict:
        return boundary_error(409, "EXECUTION_CONFLICT")
    return AgentProofResponse(
        contract_version=1,
        owner_id=value.owner_id,
        integration_id=value.integration_id,
        channel=value.channel,
        agent_id=value.agent_id,
        agent_owner_id=agent.user_id,
        agent_lifecycle_generation=agent.handoff_lifecycle_generation,
    )
