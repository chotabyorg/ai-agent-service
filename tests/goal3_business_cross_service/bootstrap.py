"""Opt-in real AI app with synthetic adapters and real commit-phase barriers.

No production startup imports this module. Test worker routes invoke the actual
runtime/repository with trusted synthetic grants; they never fake ledger/results.
"""

from tests.business_effect_helpers import open_business_runtime
import asyncio
import contextvars
import json
import os
import secrets
import socket
from datetime import datetime
from pathlib import Path
from uuid import UUID

from tests.goal3_business_cross_service.network import install_guard, self_check
from tests.goal3_business_cross_service.state import State

CURRENT_METHOD = contextvars.ContextVar("goal3_repository_method", default="")
CORE_RETURNED = contextvars.ContextVar("goal3_core_returned", default=False)


def wrap_repository(state):
    from sqlalchemy import text
    from services.business_repository import BusinessRepository
    from services.business_runtime import BusinessRuntime

    original_open = BusinessRuntime.open.__func__

    async def observed_open(cls, request, grant, *args, **kwargs):
        state.append("projection", request.model_dump(mode="json"))
        return await original_open(cls, request, grant, *args, **kwargs)

    BusinessRuntime.open = classmethod(observed_open)

    for name in ("reserve_plan", "claim_dispatch", "finalize", "recover_attempts", "finalize_reconcile", "claim_reconcile", "confirm_plan"):
        original = getattr(BusinessRepository, name)

        def wrapper(original=original, name=name):
            async def wrapped(self, *args, **kwargs):
                token = CURRENT_METHOD.set(name)
                try:
                    if name == "reserve_plan":
                        await state.phase("A")
                    if name == "finalize":
                        await state.phase("D")
                    result = await original(self, *args, **kwargs)
                    # Every after phase follows the original method's positively
                    # returned COMMIT; no transaction is replaced or bypassed.
                    if name == "reserve_plan":
                        await state.phase("B")
                    elif name == "claim_dispatch" and result.token is not None:
                        from dataclasses import asdict
                        saved = asdict(result.token)
                        saved["command"] = result.token.command.model_dump(mode="json")
                        state.append("dispatch_token", json.loads(json.dumps(saved, default=str)))
                        await state.phase("B2")
                    elif name == "claim_reconcile" and result is not None:
                        await state.phase("RCP")
                    elif name == "finalize":
                        await state.phase("E")
                    return result
                finally:
                    CURRENT_METHOD.reset(token)
            return wrapped

        setattr(BusinessRepository, name, wrapper())

    original_lock = BusinessRepository._lock_scope

    async def lock_scope(session, scope_id, **kwargs):
        row = await original_lock(session, scope_id, **kwargs)
        method = CURRENT_METHOD.get()
        if method:
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            decisive = bool(session.info.get("business_nowait"))
            state.append("scope_lock", {"method": method, "pid": pid, "scope_id": str(scope_id),
                                        "decisive": decisive})
            if method not in {"reserve_plan", "claim_dispatch", "confirm_plan"} or decisive:
                await state.phase("LOCK-" + method)
        return row

    BusinessRepository._lock_scope = staticmethod(lock_scope)

    original_probe = BusinessRepository._admission_probe

    async def observed_probe(self, *args, **kwargs):
        result = await original_probe(self, *args, **kwargs)
        method = CURRENT_METHOD.get()
        state.append("probe", {"method": method})
        # The original P's transaction and session have exited here.
        await state.phase("P-" + method)
        return result

    BusinessRepository._admission_probe = observed_probe

    from services.llm_service import LLMService
    original_core = LLMService._execute_core

    async def observed_core(self, *args, **kwargs):
        result = await original_core(self, *args, **kwargs)
        CORE_RETURNED.set(True)
        return result

    LLMService._execute_core = observed_core
    original_gate = BusinessRepository.result_gate

    async def observed_gate(self, *args, **kwargs):
        if CORE_RETURNED.get():
            await state.phase("FINAL-READ")
            if json.loads((state.directory / "faults.json").read_text()).get("final_read_failure"):
                raise RuntimeError("Synthetic unavailable final observation")
        return await original_gate(self, *args, **kwargs)

    BusinessRepository.result_gate = observed_gate

    from tests.goal3_business_cross_service.adapter import HTTPBookingAdapter
    original_request = HTTPBookingAdapter.request

    async def observed_request(self, method, path, execution, body=None):
        result = await original_request(self, method, path, execution, body)
        if path == "/facts":
            state.append("fresh", {"method": CURRENT_METHOD.get(), "revisions": result["revisions"]})
            await state.phase("FRESH-" + CURRENT_METHOD.get())
        return result

    HTTPBookingAdapter.request = observed_request

    from services import managed_turn_service
    from services.managed_tools import ManagedExecutionError
    original_close = managed_turn_service._close_client

    async def close_client(client):
        await original_close(client)
        if json.loads((state.directory / "faults.json").read_text()).get("cleanup_failure"):
            # The actual native resource close completes first. Only this
            # test-owned bootstrap injects the subsequent cleanup error.
            raise ManagedExecutionError("TOOL_CLEANUP_FAILED")

    managed_turn_service._close_client = close_client


class Observe:
    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        is_managed = scope["type"] == "http" and scope["path"] == "/internal/v1/managed-turns/execute"
        is_evidence = scope["type"] == "http" and scope["path"] == "/internal/v1/managed-turns/evidence"
        if is_managed or is_evidence:
            self.state.append("execute" if is_managed else "evidence", {"path": scope["path"]})
        body = bytearray()

        async def observe(event):
            projection = scope.get("managed_turn_request")
            if is_managed and projection is not None and not scope.get("goal3_observed_projection"):
                self.state.append("projection", projection.model_dump(mode="json"))
                scope["goal3_observed_projection"] = True
            if is_managed and event["type"] == "http.response.body":
                body.extend(event.get("body", b""))
                if not event.get("more_body", False):
                    self.state.append("result", json.loads(body))
            await send(event)

        # Boundary attaches the actual validated projection before the route.
        async def observed_receive():
            event = await receive()
            return event

        try:
            await self.app(scope, observed_receive, observe)
        finally:
            projection = scope.get("managed_turn_request")
            if is_managed and projection is not None and not scope.get("goal3_observed_projection"):
                self.state.append("projection", projection.model_dump(mode="json"))


async def seed(config, registrations):
    from core.managed_security import ManagedGrant
    from db.database import AsyncSessionLocal, engine
    from services.business_repository import BusinessRepository
    from tests.goal2_cross_service_bootstrap import seed as seed_agents

    await seed_agents(config)
    repo = BusinessRepository(AsyncSessionLocal)
    registration = registrations["fake_" + config["profile"]["mode"].lower()]
    for record, grant_value in zip(config["agents"], config["grants"]):
        await repo.configure_binding(ManagedGrant.model_validate(grant_value),
            adapter_id=registration.adapter_id, connection_id=UUID(record["connection_id"]),
            catalog_revision=registration.legacy_catalog_revision if config["profile"].get("retained_legacy") else registration.catalog_revision,
            enabled_capabilities=[cap.name for cap in registration.capabilities])
    await engine.dispose()


def worker_router(config, registrations, state):
    import anyio
    from fastapi import APIRouter, Request, Response
    from core.managed_security import ManagedGrant
    from db.database import AsyncSessionLocal
    from schemas.business import BusinessAction, BusinessSafetyError
    from schemas.managed_turn import ManagedTurnRequest
    from services.business_repository import BusinessRepository
    from services.managed_runtime import ManagedRuntime

    router = APIRouter()
    grants = [ManagedGrant.model_validate(value) for value in config["grants"]]

    @router.post("/__goal3__/worker")
    async def run_worker(request: Request):
        token = request.headers.get("X-Goal3-Worker", "")
        if not secrets.compare_digest(token, config["worker_token"]):
            return Response(status_code=401)
        value = await request.json()
        projection = ManagedTurnRequest.model_validate(value["request"])
        grant = next((g for g in grants if g.scope() == (projection.owner_id, projection.integration_id,
                     projection.channel, projection.agent_id)), None)
        if grant is None:
            return Response(status_code=404)
        execution = ManagedRuntime(35, request=projection)
        action, result = value["action"], None
        with anyio.CancelScope(shield=True) as lifecycle:
            try:
                repo = BusinessRepository(AsyncSessionLocal)
                if action in {"reserve_direct", "confirm_direct", "reconcile_direct", "finalize_again"}:
                    from schemas.business import FrozenProviderCommand, SucceededReceipt
                    from services.business_repository import DispatchToken, scope_value
                    registration = registrations["fake_" + config["profile"]["mode"].lower()]
                    async with AsyncSessionLocal() as db:
                        actual_scope = scope_value(await repo._scope_query(db, projection, grant))
                    if action == "reserve_direct":
                        selected = tuple(BusinessAction.model_validate(v) for v in value["actions"])
                        async def facts_resolver():
                            return await registration.adapter.resolve_facts(actual_scope, selected, execution=execution)
                        plan = await repo.reserve_plan(projection, grant, registration, selected, facts_resolver,
                                                       propose=value.get("propose", False))
                        result = {"plan_id": str(plan.id)}
                    else:
                        plan = (await repo.lookup_plan(projection, grant, UUID(value["plan_id"]))
                                if action == "confirm_direct" else await repo.selected_plan(projection, grant))
                        if action == "confirm_direct":
                            selected = tuple(BusinessAction(capability=o.command.capability, arguments=o.command.arguments)
                                             for o in plan.operations)
                            async def facts_resolver():
                                return await registration.adapter.resolve_facts(actual_scope, selected, execution=execution)
                            await repo.confirm_plan(projection, grant, plan.id, registration, facts_resolver)
                            result = {"plan_id": str(plan.id)}
                        elif action == "reconcile_direct":
                            token = await repo.claim_reconcile(projection, grant, plan.id, UUID(value["operation_id"]),
                                                              timeout_seconds=registration.profile.dispatch_timeout_seconds)
                            result = {"permitted": token is not None}
                            if token is not None:
                                receipt = await registration.adapter.lookup(token.command, execution=execution)
                                await repo.finalize_reconcile(projection, grant, token, receipt)
                        else:
                            records = [json.loads(line)["value"] for line in (state.directory / "trace.jsonl").read_text().splitlines()
                                       if json.loads(line)["kind"] == "dispatch_token"]
                            saved = next(v for v in reversed(records) if v["operation_id"] == value["operation_id"])
                            command = FrozenProviderCommand.model_validate(saved["command"])
                            token = DispatchToken(UUID(saved["operation_id"]), UUID(saved["scope_id"]), UUID(saved["plan_id"]),
                                saved["state_version"], saved["dispatch_epoch"], UUID(saved["dispatch_nonce"]), command,
                                native_replay=saved.get("native_replay", False),
                                accept_before=datetime.fromisoformat(saved["accept_before"])
                                if saved.get("accept_before") else None, execution_contract=saved["execution_contract"])
                            provider_receipt = await registration.adapter.lookup(command, execution=execution)
                            receipt = SucceededReceipt.model_validate(provider_receipt.model_dump(mode="json"))
                            await repo.finalize(projection, grant, token, receipt)
                            result = {}
                elif action == "configure":
                    registration = registrations["fake_" + config["profile"]["mode"].lower()]
                    record = next(record for record in config["agents"] if record["id"] == str(grant.agent_id))
                    await repo.configure_binding(grant, adapter_id=registration.adapter_id,
                        connection_id=UUID(record["connection_id"]), catalog_revision=registration.catalog_revision,
                        enabled_capabilities=value["enabled_capabilities"],
                        authorization_generation=value["authorization_generation"],
                        context_generation=value.get("context_generation", 1), active=value.get("active", True))
                    result = {}
                elif action == "recover":
                    result = {"count": await repo.recover_attempts(projection, grant)}
                elif action == "claim":
                    # Deliberately does not call Runtime.open/normalize: this
                    # exercises D against an expired-but-still-DISPATCHING row.
                    plan = await repo.selected_plan(projection, grant)
                    registration = registrations[plan.operations[0].command.adapter_id]
                    actions = tuple(BusinessAction(capability=o.command.capability,
                        arguments=o.command.arguments) for o in plan.operations)
                    async def facts_resolver():
                        return await registration.adapter.resolve_facts(plan.scope, actions, execution=execution)
                    operation = plan.operations[value.get("ordinal", 0)]
                    decision = await repo.claim_dispatch(projection, grant, plan.id, operation.id,
                        registration, facts_resolver, native_replay=value.get("native", False))
                    result = {"permitted": decision.token is not None, "reason": decision.reason}
                    if decision.token is not None and value.get("send", False):
                        receipt = await registration.adapter.execute(decision.token.dispatch, execution=execution)
                        from schemas.business import SucceededReceipt, DefinitiveNoEffect
                        plan = (await repo.finalize(projection, grant, decision.token, receipt)
                                if isinstance(receipt, (SucceededReceipt, DefinitiveNoEffect))
                                else await repo.mark_unknown(projection, grant, decision.token,
                                    lookup_available=registration.profile.mode != "BLIND"))
                    else:
                        plan = decision.plan
                    result["operation_refs"] = [r.model_dump(mode="json") for r in plan.references]
                else:
                    runtime = await open_business_runtime(projection, grant, AsyncSessionLocal,
                        registrations, execution=execution)
                    if action == "reserve":
                        plan = await runtime.freeze_plan(value["actions"], propose=value.get("propose", False))
                        result = {"plan_id": str(plan.id)}
                    elif action == "execute":
                        await runtime.prompt_context()
                        result = (await runtime.call_tool("business_execute_plan",
                            {"actions": value["actions"]})).model_dump(mode="json")
                    elif action == "confirm":
                        await runtime.prompt_context()
                        result = (await runtime.call_tool("business_confirm",
                            {"proposal_id": value["plan_id"]})).model_dump(mode="json")
                    elif action == "confirm_only":
                        plan = await repo.lookup_plan(projection, grant, UUID(value["plan_id"]))
                        async def facts_resolver():
                            return await runtime._facts([BusinessAction(capability=o.command.capability,
                                arguments=o.command.arguments) for o in plan.operations])
                        plan = await repo.confirm_plan(projection, grant, plan.id, runtime.registration, facts_resolver)
                        result = {"plan_id": str(plan.id)}
                    elif action == "reconcile":
                        await runtime.reconcile(UUID(value["operation_id"]))
                        result = {}
                    elif action == "native":
                        await runtime.replay_native(UUID(value["operation_id"]))
                        result = {}
                    elif action == "execute_stale":
                        plan = await repo.selected_plan(projection, grant)
                        await state.phase("STALE")
                        result = (await runtime._execute_frozen(plan)).model_dump(mode="json")
                    elif action == "raw_facts":
                        from schemas.business import MaterialFacts
                        plan = await repo.selected_plan(projection, grant)
                        if value["method"] == "reserve":
                            await repo.reserve_plan(projection, grant, runtime.registration,
                                value["actions"], MaterialFacts())
                        elif value["method"] == "confirm":
                            await repo.confirm_plan(projection, grant, plan.id, runtime.registration, MaterialFacts())
                        else:
                            await repo.claim_dispatch(projection, grant, plan.id, plan.operations[0].id,
                                runtime.registration, MaterialFacts())
                        result = {"rejected": False}
                    else:
                        raise ValueError("Unknown synthetic worker action")
                result["operation_refs"] = [r.model_dump(mode="json") for r in await repo.collect_refs(projection, grant)]
            except BusinessSafetyError as exc:
                result = {"safety_code": exc.code, "halt_status": exc.processing_status,
                          "operation_refs": [r.model_dump(mode="json") for r in
                                             await repo.collect_refs(projection, grant)]}
            finally:
                await execution.cleanup(lifecycle)
        return result

    return router


def run():
    allowed = install_guard([("127.0.0.1", 55438)])
    self_check()
    config = json.loads(Path(os.environ["GOAL3_BOOTSTRAP_CONFIG"]).read_text())
    if (Path.cwd() / ".env").exists():
        raise RuntimeError("Goal3 forbids runtime .env")
    allowed.update((str(host), int(port)) for host, port in config["allowlist"])
    from tests.goal2_cross_service_bootstrap import State as LegacyState, prepare_fakes
    from tests.goal3_business_cross_service.adapter import registry

    state = State(config["state_dir"])
    state.append("guard", {"denials": 3})
    prepare_fakes(LegacyState(config["state_dir"]))
    registrations = registry(config["provider_url"], native_policy=config["profile"].get(
        "native_policy", "DURABLE_TEST"), horizon=config["profile"].get("horizon"),
        timeout=config["profile"].get("timeout", 1), proposal_ttl=config["profile"].get("proposal_ttl", 300),
        bounded_acceptance=config["profile"].get("bounded_acceptance", "PROVIDER_ACCEPT_BEFORE_V1"),
        retained_legacy=config["profile"].get("retained_legacy", False))
    asyncio.run(seed(config, registrations))
    wrap_repository(state)
    import main
    import uvicorn
    from db.database import AsyncSessionLocal

    main.app.state.business_session_factory = AsyncSessionLocal
    main.app.state.business_registry = registrations
    main.app.include_router(worker_router(config, registrations, state))
    listener = socket.fromfd(config["fd"], socket.AF_INET, socket.SOCK_STREAM)
    server = uvicorn.Server(uvicorn.Config(Observe(main.app, state), host="127.0.0.1",
        port=config["port"], access_log=False, log_level="warning", timeout_graceful_shutdown=3))
    server.run(sockets=[listener])


if __name__ == "__main__":
    run()
