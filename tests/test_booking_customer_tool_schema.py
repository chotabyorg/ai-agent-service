"""Advertised booking inputs must express the existing customer choice rule."""
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from business_capabilities.registry import CAPABILITIES, DEFAULTS
from business_capabilities.schemas import BookingCreate
from services.business_runtime import BusinessRuntime
from schemas.business import fingerprint


CUSTOMER = {"display_name": "Synthetic", "identity": {"kind": "PHONE", "value": "+12025550112"}}
BASE = {"offering_ref": "ref_offering", "resource_ref": "ref_resource",
        "start_at": "2030-01-02T11:00:00+03:00"}


def test_advertised_choice_preserves_accepted_durable_schema_identity():
    # Accepted 53dfe45 BookingCreate identity: previously frozen plans retain it.
    assert fingerprint('chota.business.schema.v1', BookingCreate.model_json_schema()) == (
        '47d14b4111bd684db67be085c30c53e2ef9de89686d2a44c726bac8105b6d49c')


def tools():
    runtime = BusinessRuntime()
    runtime.binding = SimpleNamespace(enabled_capabilities=DEFAULTS)
    runtime.registration = SimpleNamespace(adapter_id="chotacrm", confirmation_mode="SEMANTIC_V1",
        capabilities=[SimpleNamespace(name=c.id) for c in CAPABILITIES])
    return {t["function"]["name"]: t["function"]["parameters"] for t in runtime.openai_tools()}


@pytest.mark.parametrize("customer_fields,accepted", [
    ({"customer_ref": "ref_existing"}, True),
    ({"customer": CUSTOMER}, True),
    ({}, False),
    ({"customer_ref": "ref_existing", "customer": CUSTOMER}, False),
    ({"customer_ref": "", "customer": CUSTOMER}, False),
    ({"customer_ref": None, "customer": CUSTOMER}, False),
    ({"customer": None}, False),
    ({"customer": CUSTOMER, "unknown": "field"}, False),
])
def test_both_model_surfaces_match_runtime_customer_choice(customer_fields, accepted):
    arguments = {**BASE, **customer_fields}
    try:
        BookingCreate.model_validate(arguments)
        runtime_accepts = True
    except ValidationError:
        runtime_accepts = False
    assert runtime_accepts is accepted
    schemas = tools()
    for name, value in (
        ("booking_create", arguments),
        ("business_propose", {"actions": [{"capability": "booking.create", "arguments": arguments}]}),
    ):
        validator = Draft202012Validator(schemas[name])
        validator.check_schema(schemas[name])
        assert validator.is_valid(value) is accepted, name
