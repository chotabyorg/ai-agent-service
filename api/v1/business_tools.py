"""First-party connection settings. Ownership is checked before provider calls."""
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession
from api.dependencies import get_db
from core.config import get_settings
from business_capabilities.schemas import Closed
from business_capabilities.context import BusinessRuntimeError
from services.business_connection_service import BusinessConnectionService

router = APIRouter()
Db = Annotated[AsyncSession, Depends(get_db)]
Owner = Annotated[UUID, Header(alias='X-User-Id')]


class Connect(Closed):
    workspace_id: UUID


class Enable(Closed):
    enabled_capabilities: list[str] = Field(max_length=18)


def service():
    return BusinessConnectionService(get_settings())


def browser(request, *, mutation=False):
    cookie = request.headers.get('cookie', '')
    if not request.cookies.get('access_token'):
        raise HTTPException(401, 'Browser authentication required')
    if mutation and request.headers.get('origin') not in get_settings().business_config_origins:
        raise HTTPException(403, 'Origin not allowed')
    return 'access_token=' + request.cookies['access_token']


def translate(exc):
    code = {'NOT_FOUND': 404, 'PERMISSION_DENIED': 403, 'VALIDATION_ERROR': 422, 'UNKNOWN_WRITE_RESULT': 409, 'CONNECTION_CONFLICT': 409}.get(exc.code, 503)
    return HTTPException(code, {'code': exc.code, 'message': 'Business tools are unavailable' if code == 503 else exc.code})


@router.get('/{agent_id}/business-tools/workspaces')
async def workspaces(agent_id: UUID, request: Request, db: Db, owner: Owner):
    svc = service()
    try:
        await svc.owner(db, agent_id, owner)
        return await svc.adapter.workspaces(owner, browser(request))
    except BusinessRuntimeError as exc:
        raise translate(exc) from None


@router.get('/{agent_id}/business-tools')
async def state(agent_id: UUID, request: Request, db: Db, owner: Owner):
    svc = service()
    try:
        await svc.owner(db, agent_id, owner)
        available = await svc.adapter.workspaces(owner, browser(request))
        return await svc.state(db, agent_id, available)
    except BusinessRuntimeError as exc:
        raise translate(exc) from None


@router.put('/{agent_id}/business-tools')
async def connect(agent_id: UUID, payload: Connect, request: Request, db: Db, owner: Owner):
    svc = service()
    try:
        await svc.owner(db, agent_id, owner)
        return await svc.configure(db, agent_id, owner, browser(request, mutation=True), workspace_id=payload.workspace_id)
    except BusinessRuntimeError as exc:
        raise translate(exc) from None


@router.patch('/{agent_id}/business-tools')
async def enable(agent_id: UUID, payload: Enable, request: Request, db: Db, owner: Owner):
    svc = service()
    try:
        await svc.owner(db, agent_id, owner)
        return await svc.configure(db, agent_id, owner, browser(request, mutation=True), enabled=payload.enabled_capabilities)
    except BusinessRuntimeError as exc:
        raise translate(exc) from None


@router.delete('/{agent_id}/business-tools', status_code=204)
async def disconnect(agent_id: UUID, request: Request, db: Db, owner: Owner):
    svc = service()
    try:
        await svc.owner(db, agent_id, owner)
        await svc.disconnect(db, agent_id, owner, browser(request, mutation=True))
        return Response(status_code=204)
    except BusinessRuntimeError as exc:
        raise translate(exc) from None
