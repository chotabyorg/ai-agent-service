"""Removing schema annotations must preserve real business fields named title."""
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from business_capabilities.registry import REGISTRY


@pytest.mark.parametrize('capability', ['deal.create', 'followup.create'])
@pytest.mark.parametrize('title,accepted', [('Заказ', True), ('', False), ('x' * 201, False)])
def test_title_business_field_remains_required_and_validated(capability, title, accepted):
    definition = REGISTRY[capability]
    arguments = {'customer_ref': 'customer_test', 'title': title}
    if capability == 'deal.create':
        arguments.update(currency='BYN', items=[{'offering_ref': 'offering_test', 'quantity': '1'}])
    schema = definition.tool()['function']['parameters']
    validator = Draft202012Validator(schema)
    assert validator.is_valid(arguments) is accepted
    try:
        definition.input.model_validate(arguments)
        canonical_accepts = True
    except ValidationError:
        canonical_accepts = False
    assert canonical_accepts is accepted
    assert not validator.is_valid({key: value for key, value in arguments.items() if key != 'title'})
    assert not validator.is_valid(dict(arguments, unexpected='extra'))
