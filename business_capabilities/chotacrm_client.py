"""Narrow first-party RS256 application client; no browser credentials at runtime."""
import base64
import json
import time
from http.cookiejar import CookieJar, DefaultCookiePolicy
from uuid import UUID, uuid4

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from business_capabilities.context import BusinessRuntimeError
from business_capabilities.registry import REGISTRY
from core.managed_wait_diagnostics import observe_managed_crm_wait


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


class _NoResponseCookies(DefaultCookiePolicy):
    """A reused application transport must not acquire browser credentials."""

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class ChotaCRMClient:
    supported_capabilities = frozenset(REGISTRY)

    def __init__(self, settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    @staticmethod
    def _validated_key(pem):
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError('Invalid signing key')
        return key

    def assertion(self, scope: str, claims: dict, *, execution=None) -> str:
        pem = self.settings.business_crm_private_key.get_secret_value()
        kid = self.settings.business_crm_key_id
        if not pem or not kid:
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
        try:
            from services.voice_signing import signing_key
            key = signing_key(execution, pem, self._validated_key)
            now = int(time.time())
            header = _b64(json.dumps({'alg': 'RS256', 'typ': 'JWT', 'kid': kid}, separators=(',', ':')).encode())
            payload = _b64(json.dumps({'iss': 'ai-agent-service', 'aud': 'chota-crm-agent', 'iat': now,
                                      'exp': now + 45, 'jti': str(uuid4()), 'scope': scope, **claims}, separators=(',', ':')).encode())
            unsigned = f'{header}.{payload}'
            signature = key.sign(unsigned.encode(), padding.PKCS1v15(), hashes.SHA256())
            return f'{unsigned}.{_b64(signature)}'
        except (ValueError, TypeError):
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE') from None

    @staticmethod
    def _http_phase(method, path):
        # Only code-owned route classes leave the client, never URLs, paths,
        # assertions, headers, claims or request/response payloads.
        if method == 'GET' and path == 'context':
            return 'CRM_CONTEXT_HTTP'
        if isinstance(path, str):
            if method == 'POST' and path.startswith('facts/'):
                return 'CRM_FACTS_HTTP'
            if method == 'POST' and path.startswith('execute/'):
                return 'CRM_EXECUTE_HTTP'
            if method == 'GET' and path.startswith('operations/'):
                return 'CRM_STATUS_HTTP'
        return 'CRM_OTHER_HTTP'

    async def request(self, method, path, *, scope, claims, body=None, cookie=None, execution=None):
        if not self.settings.business_capabilities_enabled:
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
        with observe_managed_crm_wait(execution, 'CRM_ASSERTION'):
            headers = {'Authorization': 'Bearer ' + self.assertion(scope, claims, execution=execution)}
        if cookie is not None:
            # Configuration only; no cookies are retained in model/connection/runtime state.
            headers['Cookie'] = cookie
            headers['X-User-Id'] = claims['owner_id']
        from services.managed_http_cleanup import own_managed_http, close_managed_helper
        from services.managed_runtime import ManagedRuntime
        from services.managed_tools import ManagedExecutionError
        # Only the real Turn owner supplies a lifecycle for connection reuse.
        # Configuration/browser requests and other trusted callers retain their
        # existing one-request close. No grant, assertion, facts or result is cached.
        pooled = isinstance(execution, ManagedRuntime) and cookie is None
        if pooled and execution._crm_http_closed:
            raise ManagedExecutionError('TOOL_CLEANUP_FAILED')
        base_url = self.settings.business_crm_url.rstrip('/') + '/internal/agent/v1/'
        timeout = self.settings.business_crm_timeout_seconds
        key = (self, base_url, timeout, self.transport)
        with observe_managed_crm_wait(execution, 'CRM_CLIENT_SETUP'):
            client = execution._crm_http_clients.get(key) if pooled else None
            if client is not None and client.is_closed:
                raise ManagedExecutionError('TOOL_CLEANUP_FAILED')
            if client is None:
                client = httpx.AsyncClient(base_url=base_url, timeout=timeout,
                    transport=self.transport, follow_redirects=False, trust_env=False,
                    cookies=CookieJar(policy=_NoResponseCookies()))
        retained = False
        try:
            if execution is not None:
                own_managed_http(client)
            if pooled:
                if key not in execution._crm_http_clients:
                    execution._crm_http_clients[key] = client

                    async def close_turn_client():
                        try:
                            with observe_managed_crm_wait(execution, 'CRM_CLIENT_CLOSE'):
                                await close_managed_helper(client)
                        finally:
                            execution._crm_http_clients.pop(key, None)

                    execution.stack.push_async_callback(close_turn_client)
                retained = True
            with observe_managed_crm_wait(execution, self._http_phase(method, path)):
                async with client.stream(method, path.lstrip('/'), headers=headers, json=body) as response:
                    chunks = bytearray()
                    async for part in response.aiter_bytes():
                        chunks.extend(part)
                        if len(chunks) > 32768:
                            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
                    if response.status_code in (401, 403):
                        raise BusinessRuntimeError('PERMISSION_DENIED')
                    if response.status_code == 204:
                        return None
                    if response.status_code >= 400:
                        raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
                    return json.loads(chunks)
        except (httpx.HTTPError, ValueError):
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE') from None
        finally:
            if not retained:
                with observe_managed_crm_wait(execution, 'CRM_CLIENT_CLOSE'):
                    if execution is not None:
                        await close_managed_helper(client)
                    else:
                        await client.aclose()

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
