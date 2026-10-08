"""The code-owned ChotaCRM catalog populated through Goal3's typed registry."""
from schemas.business import canonical_value, fingerprint
from services.business_registry import (AdapterRegistration, BusinessRegistry,
    CapabilityDefinition, ExecutionContract, ProviderProfile, EMPTY_BUSINESS_REGISTRY)
from business_capabilities.registry import CAPABILITIES, Risk


class CanonicalDefinition(CapabilityDefinition):
    def normalize(self, arguments):
        return canonical_value(self.argument_model.model_validate(arguments).model_dump(
            mode='python', by_alias=True, exclude_none=True))


def create_business_registry(settings, session_factory):
    if not settings.business_capabilities_enabled:
        return EMPTY_BUSINESS_REGISTRY
    from business_capabilities.chotacrm_adapter import ChotaCRMAdapter
    registration = AdapterRegistration(
        adapter_id='chotacrm',
        profile=ProviderProfile(
            profile_revision=fingerprint('chota.crm.provider.v1', {'protocol': 'managed-facts-lookup-v1'}),
            mode='LOOKUP', dispatch_timeout_seconds=10, proposal_ttl_seconds=600),
        capabilities=tuple(CanonicalDefinition(
            name=c.id, argument_model=c.input,
            requires_confirmation=c.risk == Risk.WRITE_CONFIRM,
            public_fields=tuple(c.input.model_fields)) for c in CAPABILITIES),
        adapter=ChotaCRMAdapter(settings, session_factory),
        execution_contract=ExecutionContract(expected_facts='CONDITIONAL_EXPECTED_FACTS_V1'),
        confirmation_mode='SEMANTIC_V1',
    )
    return BusinessRegistry((registration,))
