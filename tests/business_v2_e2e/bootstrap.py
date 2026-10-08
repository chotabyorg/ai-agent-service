"""Test-only HTTP process bootstrap; application/provider code stays real."""
import asyncio
import importlib.util
import importlib.metadata
import hashlib
import json
import os
import re
import socket
import sys
from pathlib import Path
from uuid import UUID, uuid4

CONFIG = json.loads(Path(os.environ['BUSINESS_E2E_CONFIG']).read_text())
SERVICE = sys.argv[1]
ROOT = Path(CONFIG['roots'][SERVICE if SERVICE not in {'model', 'recover'} else 'ai'])
if (ROOT / '.env').exists():
    raise RuntimeError('Disposable harness forbids runtime .env')
sys.path.insert(0, str(ROOT))
# Guard before application settings, SDK, or provider imports.
spec = importlib.util.spec_from_file_location('e2e_network', Path(CONFIG['roots']['ai']) / 'tests/goal3_business_cross_service/network.py')
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)
network.install_guard(CONFIG['allowlist'])
network.self_check()
STATE = Path(CONFIG['state'])


def record(name, value):
    with (STATE / name).open('a') as f:
        f.write(json.dumps(value, ensure_ascii=False, default=str) + '\n')
    (STATE / name).chmod(0o600)


runtime_sources = {}
for folder in ['api','app','business_capabilities','core','db','models','schemas','services','migrations']:
    for source in (ROOT / folder).rglob('*.py'):
        runtime_sources[str(source.relative_to(ROOT))] = hashlib.sha256(source.read_bytes()).hexdigest()
if (ROOT / 'main.py').exists():
    runtime_sources['main.py'] = hashlib.sha256((ROOT / 'main.py').read_bytes()).hexdigest()
versions = {}
for package in ['fastapi','pydantic','SQLAlchemy','asyncpg','anyio','httpx','httpcore','openai']:
    try:
        versions[package] = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        pass
record('runtime-manifests.jsonl', {'service':SERVICE, 'sources':runtime_sources, 'packages':versions})


class Observe:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        chunks = []
        async def read():
            message = await receive()
            if message['type'] == 'http.request':
                chunks.append(message.get('body', b''))
                change = STATE / 'change-facts-before-execute.json'
                if (SERVICE == 'crm' and change.exists() and '/execute/' in scope['path']
                        and not message.get('more_body') and b'"operation"' in b''.join(chunks)):
                    # Real owner HTTP edit after AI admission but before CRM
                    # acceptance. The provider's expected-facts guard must win.
                    import httpx
                    change_body = json.loads(change.read_text()); change.unlink()
                    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                        response = await client.patch('http://127.0.0.1:'+str(CONFIG['ports']['crm'])+change_body['path'],
                            json={'price':'81.00'}, headers={'X-User-Id':CONFIG['owner'],
                            'Cookie':'access_token='+CONFIG['owner'],'Origin':'https://chota.by'})
                    assert response.status_code == 200
                    record('faults.jsonl', {'kind':'FACT_CHANGED_BEFORE_PROVIDER_ACCEPTANCE'})
            return message
        buffered = []
        async def collect(message):
            buffered.append(message)
        await self.app(scope, read, collect)
        body = b''.join(chunks)
        if SERVICE == 'ai' and scope['path'] == '/internal/v1/managed-turns/execute':
            record('projections.jsonl', json.loads(body))
        if SERVICE == 'crm':
            status = next((m['status'] for m in buffered if m['type'] == 'http.response.start'), 0)
            record('crm-http.jsonl', {'path': scope['path'], 'method': scope['method'], 'status': status})
            loss = STATE / 'lose-response'
            if loss.exists() and '/execute/' in scope['path'] and b'"operation"' in body and status == 200:
                loss.unlink()
                record('faults.jsonl', {'kind': 'CRM_RESPONSE_LOST_AFTER_APP_RETURN'})
                await send({'type': 'http.response.start', 'status': 503, 'headers': [(b'content-type', b'application/json')]})
                await send({'type': 'http.response.body', 'body': b'{"code":"synthetic_response_loss"}'})
                return
        for message in buffered:
            await send(message)


def model_app():
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    app = FastAPI()
    queue = []
    @app.get('/health')
    async def health():
        return {'status': 'ok'}
    @app.get('/api/v1/me')
    async def me(request: Request):
        value = request.cookies.get('access_token')
        if value != CONFIG['owner']:
            return JSONResponse(status_code=401, content={'detail': 'synthetic auth denied'})
        return {'id': value, 'email_verified': True, 'consent_required': False}
    @app.post('/script')
    async def script(request: Request):
        queue[:] = await request.json()
        return {'queued': len(queue)}
    @app.post('/v1/chat/completions')
    async def complete(request: Request):
        body = await request.json()
        record('model-requests.jsonl', body)
        assert queue, 'Scripted model exhausted'
        step = queue.pop(0)
        tool_names = {}
        results = {}
        for message in body['messages']:
            for call in message.get('tool_calls', []) or []:
                tool_names[call['id']] = call['function']['name']
            if message['role'] == 'tool':
                results[tool_names.get(message['tool_call_id'])] = json.loads(message['content'])
        def resolve(value):
            if isinstance(value, dict) and '$tool' in value:
                result = results[value['$tool']]
                for part in value.get('path', []):
                    result = result[part]
                return result
            if isinstance(value, dict) and '$pending' in value:
                text = '\n'.join(m.get('content') or '' for m in body['messages'] if m['role'] == 'system')
                found = re.search(r'"proposal_id"\s*:\s*"([0-9a-f-]{36})"', text)
                assert found, 'No pending proposal in real runtime prompt'
                return found.group(1)
            if isinstance(value, dict):
                return {k: resolve(v) for k,v in value.items()}
            if isinstance(value, list):
                return [resolve(v) for v in value]
            return value
        message = {'role': 'assistant', 'content': step.get('answer')}
        if 'tool' in step:
            message['tool_calls'] = [{'id': 'call_' + uuid4().hex, 'type': 'function', 'function': {
                'name': step['tool'], 'arguments': json.dumps(resolve(step.get('arguments', {})), ensure_ascii=False)}}]
        if 'answer_from' in step:
            message['content'] = str(resolve(step['answer_from']))
        if step.get('booking_answer'):
            from datetime import datetime
            from zoneinfo import ZoneInfo
            booking = results['customer_get_context']['data']['sections']['BOOKINGS']['items'][0]
            date = datetime.fromisoformat(booking['start_at'].replace('Z','+00:00')).astimezone(ZoneInfo(booking['timezone']))
            message['content'] = 'У вас запись '+date.strftime('%d.%m в %H:%M')+' на '+booking['offering_name']+'.'
        return {'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 1, 'model': 'synthetic',
                'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if 'tool' in step else 'stop'}]}
    return app


if SERVICE == 'recover':
    # Explicit operator recovery of the original selected plan, never a new
    # business command or a Conversation resubmission. All receipt HTTP is real.
    from core.config import get_settings
    from db.database import AsyncSessionLocal
    from schemas.managed_turn import ManagedTurnRequest
    from business_capabilities.registration import create_business_registry
    from services.business_runtime import BusinessRuntime
    async def recover():
        request = next(json.loads(line) for line in (STATE / 'projections.jsonl').read_text().splitlines()
                       if json.loads(line)['turn_id'] == sys.argv[2])
        settings = get_settings()
        runtime = await BusinessRuntime.open(ManagedTurnRequest.model_validate(request),
            settings.managed_principals[0].grants[0], AsyncSessionLocal,
            create_business_registry(settings, AsyncSessionLocal))
        plan = await runtime.repository.selected_plan(runtime.request, runtime.grant)
        for operation in plan.operations:
            await runtime.reconcile(operation.id)
        refs = await runtime.collect_refs()
        record('recovery.jsonl', [v.model_dump(mode='json') for v in refs])
    asyncio.run(recover())
    raise SystemExit(0)
elif SERVICE == 'model':
    app = model_app()
elif SERVICE == 'crm':
    from app.main import app
elif SERVICE == 'cs':
    from main import create_app
    from core.config import Settings
    app = create_app(Settings(_env_file=None))
else:
    import main
    from db.database import AsyncSessionLocal
    from models.agent import AgentTemplate
    from services.encryption_service import EncryptionService
    async def seed():
        async with AsyncSessionLocal() as db:
            if await db.get(AgentTemplate, UUID(CONFIG['agent'])) is None:
                db.add(AgentTemplate(id=UUID(CONFIG['agent']), user_id=UUID(CONFIG['owner']),
                    name='Synthetic Beauty', system_prompt='Вы администратор салона. Отвечайте кратко по-русски.',
                    model_name='synthetic', llm_provider='openrouter', temperature=0, is_active=True,
                    timezone='Europe/Minsk', encrypted_openrouter_key=EncryptionService().encrypt('synthetic-model-key')))
                await db.commit()
    async def seed_and_close():
        await seed()
        from db.database import engine
        await engine.dispose()
    asyncio.run(seed_and_close())
    async def noop():
        pass
    main.init_redis = main.close_redis = noop
    # Managed path must not touch legacy stores, even though Redis is absent.
    from services.history_service import HistoryService
    from services.session_service import SessionService
    async def forbidden(*args, **kwargs):
        record('forbidden.jsonl', {'kind': 'legacy_store_called'})
        raise AssertionError('Managed path touched legacy Session/history')
    for cls,names in ((HistoryService, ['load_history','save_history']),
                      (SessionService, ['get_or_create_session','create_session','get_session_by_id'])):
        for name in names:
            setattr(cls, name, forbidden)
    app = main.app

import uvicorn
if 'fds' in CONFIG:
    listener = socket.fromfd(CONFIG['fds'][SERVICE], socket.AF_INET, socket.SOCK_STREAM)
else:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('0.0.0.0', CONFIG['ports'][SERVICE]))
    listener.listen(128)
uvicorn.Server(uvicorn.Config(Observe(app), host='127.0.0.1', port=CONFIG['ports'][SERVICE],
    access_log=False, log_level='warning', timeout_graceful_shutdown=3)).run(sockets=[listener])
