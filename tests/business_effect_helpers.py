"""Explicit deterministic Conversation-authority fakes for AI unit tests."""

from uuid import NAMESPACE_URL, uuid5

from schemas.conversation_effects import (
    EffectReason,
    ExternalEffectEligibilityResult,
    ExternalEffectPermitResult,
    sidecar_fingerprint,
)


class FakeConversationEffectPort:
    """Test-only positive authority; paired tests use the real Conversation service."""

    def __init__(self, *, eligible=True, denial_reason=EffectReason.INPUT_SUPERSEDED,
                 permit_denial=None):
        self.eligible = eligible
        self.denial_reason = denial_reason
        self.permit_denial = permit_denial
        self.eligibility_requests = []
        self.permit_requests = []
        self._permits = {}

    async def eligibility(self, request):
        self.eligibility_requests.append(request)
        return ExternalEffectEligibilityResult(
            contract_version=1,
            request_fingerprint=sidecar_fingerprint(request),
            eligible=self.eligible,
            reason=EffectReason.ELIGIBLE if self.eligible else self.denial_reason,
            authority_revision=request.authority_revision,
            authority_input_floor_sequence=0,
            split_input_floor_sequence=0,
        )

    async def permit(self, request):
        self.permit_requests.append(request)
        if not self.eligible or self.permit_denial is not None:
            from services.conversation_effects import DefinitiveEffectDenial

            raise DefinitiveEffectDenial(self.permit_denial or self.denial_reason)
        identity = sidecar_fingerprint(request)
        existing = self._permits.get(identity)
        permit_id = existing or uuid5(NAMESPACE_URL, identity)
        self._permits[identity] = permit_id
        return ExternalEffectPermitResult(
            contract_version=1,
            request_fingerprint=identity,
            permit_id=permit_id,
            replayed=existing is not None,
        )


async def open_business_runtime(request, grant, session_factory, registry, *, execution=None,
                                collector=None, effect_port=None):
    """Open production runtime with a test's explicit injected authority port."""
    from services.business_runtime import BusinessRuntime

    return await BusinessRuntime.open(
        request,
        grant,
        session_factory,
        registry,
        execution=execution,
        collector=collector,
        effect_port=effect_port or FakeConversationEffectPort(),
    )


async def prepare_effect_permit(runtime, plan, operation, *, actions=None):
    """Use the same acquisition protocol as production before a direct claim test."""
    from schemas.business import BusinessAction

    actions = actions or [BusinessAction(capability=op.command.capability,
                                         arguments=op.command.arguments)
                          for op in plan.operations]
    acquisition = await runtime.repository.prepare_effect_acquisition(
        runtime.request,
        runtime.grant,
        plan.id,
        operation.id,
        runtime.registration,
        runtime.facts_resolver(actions),
    )
    if acquisition.request is None:
        return None
    permit_id = acquisition.permit_id
    if permit_id is None:
        permit = await runtime.effect_port.permit(acquisition.request)
        permit_id = permit.permit_id
        await runtime.repository.bind_effect_permit(
            runtime.request,
            runtime.grant,
            plan.id,
            operation.id,
            acquisition.request,
            permit_id,
        )
    return acquisition.request


async def claim_dispatch_with_test_permit(runtime, plan, operation, *, actions=None,
                                          facts_resolver=None, native_replay=False):
    effect_request = await prepare_effect_permit(runtime, plan, operation, actions=actions)
    default_actions = actions or [
        {"capability": op.command.capability, "arguments": op.command.arguments}
        for op in plan.operations
    ]
    return await runtime.repository.claim_dispatch(
        runtime.request,
        runtime.grant,
        plan.id,
        operation.id,
        runtime.registration,
        facts_resolver or runtime.facts_resolver(default_actions),
        effect_request=effect_request,
        native_replay=native_replay,
    )
