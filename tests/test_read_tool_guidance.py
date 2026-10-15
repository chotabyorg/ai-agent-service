"""Read guidance must reach the model without changing durable input contracts."""
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from business_capabilities import schemas as s
from business_capabilities.registry import CAPABILITIES, DEFAULTS, tool_schema
from schemas.business import fingerprint
from services.business_runtime import BusinessRuntime


def _schema_runtime():
    runtime = BusinessRuntime()
    runtime._stale_split_plan_id = None
    runtime._stale_split_change_committed = False
    return runtime


@pytest.mark.parametrize('model,accepted_hash', [
    (s.CatalogSearch, 'b3dff5e5c9ca98011615fcd2b35352d844662437cb5c38445619180b3d7ba2b4'),
    (s.CustomerFind, '0983ee916a60c64186c10e6ea1b3a55113b846f349c85979b2eb2bb74b563dd7'),
    (s.CustomerContext, '48d4e9b6981250c1f5873940afde85fbe12b21b53e83ad20c4ff3c1da9738228'),
    (s.ResourceSearch, '061f7505df03ecfdb58c451d4418b08fb7f42e39d07fd7d6f4e014edeef9233a'),
    (s.BookingSearch, 'c61f61eecdec8e2870315f56a6cc860cd5ee2b64ba30f29f2682177e86dccf09'),
])
def test_read_guidance_preserves_durable_schema(model, accepted_hash):
    before = model.model_json_schema()
    advertised = tool_schema(model)
    Draft202012Validator.check_schema(advertised)
    assert 'Omit on the first request' in advertised['properties']['cursor']['description']
    assert 'exact next_cursor' in advertised['properties']['cursor']['description']
    assert model.model_json_schema() == before
    assert fingerprint('chota.business.schema.v1', before) == accepted_hash


def test_runtime_advertises_literal_search_and_opaque_cursor_guidance():
    runtime = _schema_runtime()
    runtime.effect_eligible = True  # inspect the full authorized tool projection
    runtime.recovery_mode = False
    runtime.binding = SimpleNamespace(enabled_capabilities=DEFAULTS)
    runtime.registration = SimpleNamespace(adapter_id='chotacrm', confirmation_mode='SEMANTIC_V1',
        capabilities=[SimpleNamespace(name=c.id) for c in CAPABILITIES])
    tools = {t['function']['name']: t['function']['parameters'] for t in runtime.openai_tools()}
    for name in ('catalog_search', 'resource_search'):
        properties = tools[name]['properties']
        assert 'literal substring' in properties['query']['description']
        assert 'empty string to browse' in properties['query']['description']
        assert 'Never invent' in properties['cursor']['description']
    assert 'description' not in tools['catalog_get']['properties']['offering_ref']


@pytest.mark.parametrize('arguments,accepted', [
    ({}, True), ({'query': 'маникюр'}, True), ({'cursor': 'cur_retained'}, True),
    # Provider authority still rejects an unissued cursor; never coerce it to page one.
    ({'cursor': '0'}, True), ({'cursor': ''}, False), ({'cursor': None}, False),
    ({'cursor': 0}, False), ({'limit': 21}, False), ({'unknown': 'field'}, False),
])
def test_guidance_does_not_relax_or_coerce_arguments(arguments, accepted):
    validator = Draft202012Validator(tool_schema(s.CatalogSearch))
    assert validator.is_valid(arguments) is accepted
    if accepted:
        actual = s.CatalogSearch.model_validate(arguments).model_dump(exclude_none=True)
        for name, value in arguments.items():
            assert actual[name] == value
    else:
        with pytest.raises(ValidationError):
            s.CatalogSearch.model_validate(arguments)


@pytest.mark.parametrize('model,accepted_hash', [
    (s.Availability, 'f2046bcb1f119075f0f8a07b23d335a184023f9748acd0792114450c8c85fbac'),
    (s.BookingCreate, '47d14b4111bd684db67be085c30c53e2ef9de89686d2a44c726bac8105b6d49c'),
    (s.BookingReschedule, '68ece496ae44f9b88b19c9560e6f6b09579efa43d425c71c3658480ac3190fec'),
])
def test_appointment_time_guidance_preserves_canonical_schema(model, accepted_hash):
    before = model.model_json_schema()
    advertised = tool_schema(model)
    Draft202012Validator.check_schema(advertised)
    description = advertised['properties']['start_at']['description']
    assert '14:00+03:00 or 11:00Z' in description
    assert 'newly requested time' in description
    assert advertised['properties']['start_at']['format'] == 'date-time'
    assert model.model_json_schema() == before
    assert fingerprint('chota.business.schema.v1', before) == accepted_hash


def test_requested_move_time_keeps_its_instant_without_silent_correction():
    arguments = dict(resource_ref='resource', offering_ref='offering')
    local = s.Availability(**arguments, start_at='2026-10-15T14:00:00+03:00')
    utc = s.Availability(**arguments, start_at='2026-10-15T11:00:00Z')
    old = s.Availability(**arguments, start_at='2026-10-15T11:00:00+03:00')
    assert local.start_at == utc.start_at
    assert local.start_at != old.start_at
    assert old.model_dump(mode='json')['start_at'] == '2026-10-15T08:00:00Z'
    with pytest.raises(ValidationError):
        s.Availability(**arguments, start_at='2026-10-15T14:00:00')


def test_actual_runtime_advertises_time_guidance_for_reads_and_moves():
    runtime = _schema_runtime()
    runtime.effect_eligible = True  # inspect the full authorized tool projection
    runtime.recovery_mode = False
    runtime.binding = SimpleNamespace(enabled_capabilities=DEFAULTS)
    runtime.registration = SimpleNamespace(adapter_id='chotacrm', confirmation_mode='SEMANTIC_V1',
        capabilities=[SimpleNamespace(name=c.id) for c in CAPABILITIES])
    tools = {t['function']['name']: t['function']['parameters'] for t in runtime.openai_tools()}
    for name in ('booking_check_availability', 'booking_create', 'booking_reschedule'):
        assert 'newly requested time' in tools[name]['properties']['start_at']['description']
    actions = tools['business_propose']['properties']['actions']['items']['oneOf']
    by_capability = {a['properties']['capability']['const']: a['properties']['arguments'] for a in actions}
    for capability in ('booking.create', 'booking.reschedule'):
        assert 'newly requested time' in by_capability[capability]['properties']['start_at']['description']
