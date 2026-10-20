import logging
from types import SimpleNamespace
from uuid import UUID

import pytest

from core.managed_business_diagnostics import (
    ManagedBusinessDiagnostic, _IDENTITY, emit_business_diagnostic,
)
from core.managed_security import install_managed_log_boundary, managed_logging


TURN_ID = UUID("00000000-0000-4000-8000-000000000062")
PRIVATE = "business-diagnostic-private-sentinel"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_business_diagnostic_rebuilds_only_closed_fields():
    install_managed_log_boundary()
    logger = logging.getLogger("uvicorn.error")
    previous_level = logger.level
    previous_propagate = logger.propagate
    previous_disabled = logger.disabled
    previous_global_disable = logging.root.manager.disable
    capture = Capture()
    logger.setLevel(logging.INFO)
    logger.disabled = False
    logging.disable(logging.NOTSET)
    logger.propagate = False
    logger.addHandler(capture)
    token = managed_logging.set(True)
    try:
        emit_business_diagnostic(ManagedBusinessDiagnostic(
            turn_id=TURN_ID, stage="LOOKUP", capability="customer.find",
            refusal_category="NONE", lookup_source="MODEL", lookup_mode="IDENTITY_ONLY",
            lookup_result="SINGLE", proposal_present=False, operation_present=False,
            schema_rule="type", schema_field="cursor",
            offering_ref_source="READ_THIS_TURN", resource_ref_source="WRONG_KIND_THIS_TURN",
            customer_ref_source="NOT_OBSERVED_THIS_TURN",
        ))
    finally:
        managed_logging.reset(token)
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        logger.disabled = previous_disabled
        logging.disable(previous_global_disable)

    records = [record for record in capture.records
               if getattr(record, "event", None) == "managed_business_observed"]
    assert len(records) == 1
    record = records[0]
    assert record.turn_id == str(TURN_ID)
    assert record.stage == "LOOKUP" and record.capability == "customer.find"
    assert record.lookup_source == "MODEL" and record.lookup_mode == "IDENTITY_ONLY"
    assert record.lookup_result == "SINGLE" and record.refusal_category == "NONE"
    assert record.proposal_present is False and record.operation_present is False
    assert record.schema_rule == "type" and record.schema_field == "cursor"
    assert record.offering_ref_source == "READ_THIS_TURN"
    assert record.resource_ref_source == "WRONG_KIND_THIS_TURN"
    assert record.customer_ref_source == "NOT_OBSERVED_THIS_TURN"
    assert PRIVATE not in repr(record.__dict__)


@pytest.mark.parametrize('malformed_field', ['stage', 'schema_rule', 'schema_field',
    'offering_ref_source', 'resource_ref_source', 'customer_ref_source'])
def test_malformed_marked_diagnostic_collapses_to_generic_record(malformed_field):
    install_managed_log_boundary()
    logger = logging.getLogger("uvicorn.error")
    previous_level = logger.level
    previous_propagate = logger.propagate
    previous_disabled = logger.disabled
    previous_global_disable = logging.root.manager.disable
    capture = Capture()
    logger.setLevel(logging.INFO)
    logger.disabled = False
    logging.disable(logging.NOTSET)
    logger.propagate = False
    logger.addHandler(capture)
    token = managed_logging.set(True)
    try:
        logger.info(PRIVATE, extra={
            "_business_identity": _IDENTITY,
            "_business_diagnostic": ManagedBusinessDiagnostic(
                turn_id=TURN_ID, capability="customer.find",
                refusal_category="NONE", lookup_source="MODEL", lookup_mode="IDENTITY_ONLY",
                lookup_result="SINGLE", proposal_present=False, operation_present=False,
                **{'stage': 'LOOKUP', malformed_field: PRIVATE},
            ),
            "untrusted": PRIVATE,
        })
    finally:
        managed_logging.reset(token)
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        logger.disabled = previous_disabled
        logging.disable(previous_global_disable)

    assert not [record for record in capture.records
                if getattr(record, "event", None) == "managed_business_observed"]
    record = capture.records[-1]
    assert record.getMessage() == "Managed execution diagnostic"
    assert PRIVATE not in repr(record.__dict__)
    assert "untrusted" not in record.__dict__


def test_diagnostic_logging_failure_is_nonfatal(monkeypatch):
    install_managed_log_boundary()
    token = managed_logging.set(True)
    try:
        monkeypatch.setattr(logging.Logger, "info", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError()))
        emit_business_diagnostic(ManagedBusinessDiagnostic(
            turn_id=TURN_ID, stage="LOOKUP", capability="customer.find",
            refusal_category="NONE", lookup_source="MODEL", lookup_mode="IDENTITY_ONLY",
            lookup_result="NONE", proposal_present=False, operation_present=False,
        ))
    finally:
        managed_logging.reset(token)


def test_lookup_diagnostic_mode_tracks_normalized_provider_arguments(monkeypatch):
    import business_capabilities.runtime_tools as runtime_tools
    from business_capabilities.schemas import CustomerFind
    from schemas.business import BusinessToolResult

    observed = []
    sent = []
    definition = SimpleNamespace(normalize=lambda value: CustomerFind.model_validate(value).model_dump(
        mode="json", by_alias=True, exclude_none=True))
    runtime = SimpleNamespace(
        registration=SimpleNamespace(capability=lambda _name: definition),
        observe_business_diagnostic=lambda **values: observed.append(values),
    )

    async def read(_runtime, _name, arguments):
        sent.append(arguments)
        return BusinessToolResult(tool_payload={
            "status": "OK", "data": {"match": "NONE", "candidates": []},
        })

    monkeypatch.setattr(runtime_tools, "_call_capability_impl", read)

    async def run():
        base = {"identity": {"kind": "PHONE", "value": "+375291112233"}}
        await runtime_tools.call_capability(runtime, "customer_find", base)
        with pytest.raises(ValueError):
            await runtime_tools.call_capability(runtime, "customer_find", {**base, "name": None})
        await runtime_tools.call_capability(runtime, "customer_find", {**base, "name": "Synthetic"})

    import asyncio
    asyncio.run(run())

    assert ["name" in arguments for arguments in sent] == [False, True]
    assert [event["lookup_mode"] for event in observed] == ["IDENTITY_ONLY", "IDENTITY_ONLY", "NAME_FILTERED"]
    assert observed[1]["lookup_result"] == "NOT_RUN"
    assert PRIVATE not in repr(observed)
