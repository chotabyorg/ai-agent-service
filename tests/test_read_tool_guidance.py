"""Read guidance must reach the model without changing durable input contracts."""
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from business_capabilities import schemas as s
from business_capabilities.registry import CAPABILITIES, DEFAULTS, tool_schema
from schemas.business import fingerprint
from services.business_runtime import BusinessRuntime


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
    runtime = BusinessRuntime()
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
