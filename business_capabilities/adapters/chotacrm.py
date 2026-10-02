"""Narrow first-party RS256 application client; no browser credentials at runtime."""
import base64
import json
import time
from uuid import UUID, uuid4
from datetime import datetime

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from business_capabilities.context import AgentExecutionContext, BusinessRuntimeError, failure
from business_capabilities.results import validate_result
from business_capabilities.registry import REGISTRY
from business_capabilities.ports import CapabilityId


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


class ChotaCRMAdapter:
    supported_capabilities = frozenset(REGISTRY)

    def __init__(self, settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    def assertion(self, scope: str, claims: dict) -> str:
        pem = self.settings.business_crm_private_key.get_secret_value()
        kid = self.settings.business_crm_key_id
        if not pem or not kid:
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
        try:
            key = serialization.load_pem_private_key(pem.encode(), password=None)
            if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
                raise ValueError('Invalid signing key')
            now = int(time.time())
            header = _b64(json.dumps({'alg': 'RS256', 'typ': 'JWT', 'kid': kid}, separators=(',', ':')).encode())
            payload = _b64(json.dumps({'iss': 'ai-agent-service', 'aud': 'chota-crm-agent', 'iat': now,
                                      'exp': now + 45, 'jti': str(uuid4()), 'scope': scope, **claims}, separators=(',', ':')).encode())
            unsigned = f'{header}.{payload}'
            signature = key.sign(unsigned.encode(), padding.PKCS1v15(), hashes.SHA256())
            return f'{unsigned}.{_b64(signature)}'
        except (ValueError, TypeError):
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE') from None

    async def request(self, method, path, *, scope, claims, body=None, cookie=None):
        if not self.settings.business_capabilities_enabled:
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
        headers = {'Authorization': 'Bearer ' + self.assertion(scope, claims)}
        if cookie is not None:
            # Configuration only; no cookies are retained in model/connection/runtime state.
            headers['Cookie'] = cookie
            headers['X-User-Id'] = claims['owner_id']
        try:
            async with httpx.AsyncClient(base_url=self.settings.business_crm_url.rstrip('/') + '/internal/agent/v1/',
                                         timeout=self.settings.business_crm_timeout_seconds,
                                         transport=self.transport, follow_redirects=False) as client:
                response = await client.request(method, path.lstrip('/'), headers=headers, json=body)
                if response.status_code in (401, 403):
                    raise BusinessRuntimeError('PERMISSION_DENIED')
                if response.status_code == 204:
                    return None
                if response.status_code >= 400:
                    raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
                if len(response.content) > 32768:
                    raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE') from None

    async def workspaces(self, owner_id: UUID, cookie: str):
        from business_capabilities.results import Workspaces
        result = await self.request('GET', 'workspaces', scope='config', claims={'owner_id': str(owner_id)}, cookie=cookie)
        try:
            return Workspaces.model_validate(result).model_dump(mode='json')
        except ValueError:
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE') from None

    async def configure(self, owner_id, agent_id, connection_id, workspace_id, capabilities, cookie):
        from business_capabilities.results import Grant
        result = await self.request('PUT', f'grants/{connection_id}', scope='config',
            claims={'owner_id': str(owner_id), 'agent_id': str(agent_id), 'connection_id': str(connection_id)},
            body={'agent_id': str(agent_id), 'workspace_id': str(workspace_id), 'capabilities': list(capabilities)}, cookie=cookie)
        try:
            grant = Grant.model_validate(result)
            if grant.agent_id != agent_id or grant.connection_id != connection_id or grant.workspace_id != workspace_id:
                raise ValueError('Grant mismatch')
            return grant
        except ValueError:
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE') from None

    async def revoke(self, owner_id, agent_id, connection_id, cookie):
        await self.request('DELETE', f'grants/{connection_id}', scope='config',
            claims={'owner_id': str(owner_id), 'agent_id': str(agent_id), 'connection_id': str(connection_id)}, cookie=cookie)

    async def context(self, context: AgentExecutionContext):
        from business_capabilities.results import AuthorizedContext
        result = await self.request('GET', 'context', scope='context', claims=context.claims())
        try:
            result = AuthorizedContext.model_validate(result)
            if (result.workspace_id != context.workspace_id or result.grant_id != context.grant_id or result.revision != context.grant_revision):
                raise ValueError('Stale context')
            return result
        except ValueError:
            raise BusinessRuntimeError('PERMISSION_DENIED') from None

    async def prepare(self, context: AgentExecutionContext, capability: CapabilityId, arguments: dict, turn_id: UUID) -> dict:
        if capability not in self.supported_capabilities:
            return failure('UNSUPPORTED_CAPABILITY')
        from business_capabilities.results import PreparedProposal
        result = await self.request('POST', f'prepare/{capability}', scope=capability, claims=context.claims(),
                                    body={'arguments': arguments, 'turn_id': str(turn_id)})
        if isinstance(result, dict) and result.get('status') == 'ERROR':
            return validate_result(capability, result)
        try:
            return {'status': 'OK', 'data': PreparedProposal.model_validate(result['data']).model_dump(mode='json')}
        except (ValueError, KeyError, TypeError):
            return failure('PROVIDER_UNAVAILABLE')

    async def execute(self, context: AgentExecutionContext, capability: CapabilityId, arguments: dict, operation: dict | None = None) -> dict:
        if capability not in self.supported_capabilities:
            return failure('UNSUPPORTED_CAPABILITY')
        body = {'arguments': arguments}
        if operation:
            body['operation'] = operation
        try:
            result = await self.request('POST', f'execute/{capability}', scope=capability, claims=context.claims(), body=body)
            return validate_result(capability, result, write=operation is not None)
        except BusinessRuntimeError as exc:
            if exc.code == 'PERMISSION_DENIED':
                return failure(exc.code)
            return failure('UNKNOWN_WRITE_RESULT', 'RECONCILE') if operation else failure(exc.code)

    async def reconcile(self, context: AgentExecutionContext, capability: CapabilityId, operation_id: UUID) -> dict:
        if capability not in self.supported_capabilities:
            return failure('UNSUPPORTED_CAPABILITY')
        try:
            result = await self.request('GET', f'operations/{operation_id}', scope=capability, claims=context.claims())
            return validate_result(capability, result, write=True)
        except BusinessRuntimeError as exc:
            return failure(exc.code) if exc.code == 'PERMISSION_DENIED' else failure('UNKNOWN_WRITE_RESULT', 'RECONCILE')
