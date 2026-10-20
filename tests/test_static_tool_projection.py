"""Static schema reuse cannot retain a caller mutation or live tool authority."""
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict

from business_capabilities import registry
from services.business_runtime import BusinessRuntime


def test_each_projection_matches_canonical_input_and_is_mutation_isolated():
    before = {c.id: c.input.model_json_schema() for c in registry.CAPABILITIES}
    for capability in registry.CAPABILITIES:
        expected = registry._build_tool_schema(capability.input)
        first = capability.tool()['function']['parameters']
        assert first == expected
        first['properties'].clear()
        first.setdefault('required', []).append('injected')
        assert capability.tool()['function']['parameters'] == expected
    assert {c.id: c.input.model_json_schema() for c in registry.CAPABILITIES} == before


def test_dynamic_models_are_not_retained():
    class Dynamic(BaseModel):
        model_config = ConfigDict(extra='forbid')
        value: str

    before = registry._canonical_tool_schema.cache_info()
    assert registry.tool_schema(Dynamic) == registry._build_tool_schema(Dynamic)
    assert registry._canonical_tool_schema.cache_info() == before


def test_warm_schema_cannot_keep_disabled_tools_or_effect_permission():
    runtime = BusinessRuntime()
    runtime.registration = SimpleNamespace(adapter_id='chotacrm',
        confirmation_mode='SEMANTIC_V1', capabilities=[SimpleNamespace(name=c.id) for c in registry.CAPABILITIES])
    runtime.binding = SimpleNamespace(enabled_capabilities=[c.id for c in registry.CAPABILITIES])
    runtime.recovery_mode = False
    runtime.effect_eligible = True
    runtime._stale_split_plan_id = None
    names = lambda: {t['function']['name'] for t in runtime.openai_tools()}
    assert {'booking_create', 'booking_search_options', 'business_decide_proposal'} <= names()
    runtime.effect_eligible = False
    assert 'booking_create' not in names() and 'business_decide_proposal' not in names()
    runtime.binding.enabled_capabilities = ['catalog.search']
    assert names() == {'catalog_search'}
    runtime.recovery_mode = True
    assert names() == {'catalog_search', 'business_operation_status'}
    runtime.binding.enabled_capabilities = []
    assert names() == {'business_operation_status'}
