"""Current-proposal guidance cannot replace strict decision admission."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from core.managed_business_diagnostics import (
    ManagedBusinessDiagnostic, _IDENTITY, _trusted_business_record,
)
from services.managed_tools import ManagedExecutionError
from services.pending_decision_contract import (
    pending_decision_tools, validate_pending_decision,
)
from tests.test_pending_proposal_decision import decision
from tests.test_managed_business_diagnostics import PRIVATE, TURN_ID


PROPOSAL = '00000000-0000-4000-8000-000000000070'


@pytest.mark.parametrize('presence', [False, True])
def test_model_schema_advertises_only_current_target_without_mutating_canonical_tools(presence):
    tool = {'type': 'function', 'function': {'name': 'business_decide_proposal', 'parameters': {
        'type': 'object', 'additionalProperties': False, 'required': ['proposal_id', 'intent'],
        'properties': {
            'proposal_id': {'type': 'string', 'format': 'uuid'},
            'intent': {'type': 'string', 'enum': ['CONFIRM', 'CHANGE', 'REJECT', 'UNRELATED']},
        },
    }}}
    original = deepcopy(tool)
    first = pending_decision_tools([tool], PROPOSAL, presence_reply=presence)[0]
    second = pending_decision_tools([tool], PRIVATE)[0]
    properties = first['function']['parameters']['properties']
    assert properties['proposal_id'] == {'type': 'string', 'format': 'uuid', 'enum': [PROPOSAL]}
    assert properties['intent']['enum'] == (['UNRELATED'] if presence else original[
        'function']['parameters']['properties']['intent']['enum'])
    assert second['function']['parameters']['properties']['proposal_id']['enum'] == [PRIVATE]
    assert first['function']['parameters']['additionalProperties'] is False
    assert first['function']['parameters']['required'] == ['proposal_id', 'intent']
    assert tool == original


@pytest.mark.parametrize('intent', ['CONFIRM', 'CHANGE', 'REJECT', 'UNRELATED'])
def test_valid_intent_is_not_rewritten_or_logged(intent):
    observed = []
    runtime = SimpleNamespace(business=SimpleNamespace(
        observe_business_diagnostic=lambda **fields: observed.append(fields)))
    call = decision(PROPOSAL, intent)
    arguments = call.function.arguments
    selected = SimpleNamespace(branch='TOOL_CALLS', message=SimpleNamespace(tool_calls=[call]))
    validate_pending_decision(selected, PROPOSAL, presence_reply=False, runtime=runtime)
    assert call.function.arguments == arguments and observed == []


@pytest.mark.parametrize('invalid,code,rule,field', [
    ('prose', 'INVALID_RESULT', 'type', 'tool_calls'),
    ('multiple', 'INVALID_RESULT', 'maxItems', 'tool_calls'),
    ('wrong_type', 'INVALID_RESULT', 'const', 'type'),
    ('missing_id', 'INVALID_RESULT', 'minLength', 'tool_call_id'),
    ('wrong_tool', 'INVALID_RESULT', 'const', 'tool_name'),
    ('malformed_json', 'TOOL_SETUP_FAILED', 'format', 'arguments'),
    ('duplicate_keys', 'TOOL_SETUP_FAILED', 'format', 'arguments'),
    ('invalid_unicode', 'INVALID_RESULT', 'format', 'arguments'),
    ('missing_intent', 'INVALID_RESULT', 'required', 'ROOT'),
    ('extra_arguments', 'INVALID_RESULT', 'additionalProperties', 'ROOT'),
    ('wrong_proposal', 'INVALID_RESULT', 'const', 'proposal_id'),
    ('presence_confirm', 'INVALID_RESULT', 'enum', 'intent'),
])
@pytest.mark.parametrize('observer_fails', [False, True])
def test_rejections_preserve_code_and_never_log_response_data(invalid, code, rule, field, observer_fails):
    observed = []

    def observe(**fields):
        observed.append(fields)
        if observer_fails:
            raise RuntimeError(PRIVATE)

    runtime = SimpleNamespace(business=SimpleNamespace(observe_business_diagnostic=observe))
    call = decision(PROPOSAL, 'CONFIRM')
    message = SimpleNamespace(tool_calls=[call], content=PRIVATE)
    selected = SimpleNamespace(branch='TOOL_CALLS', message=message)
    if invalid == 'prose':
        selected.branch = 'FINAL'
        message.tool_calls = None
    elif invalid == 'multiple':
        message.tool_calls.append(decision(PROPOSAL, 'REJECT'))
    elif invalid == 'wrong_type':
        call.type = PRIVATE
    elif invalid == 'missing_id':
        call.id = ''
    elif invalid == 'wrong_tool':
        call.function.name = PRIVATE
    elif invalid == 'malformed_json':
        call.function.arguments = PRIVATE
    elif invalid == 'duplicate_keys':
        call.function.arguments = '{"proposal_id":"' + PROPOSAL + '","intent":"CONFIRM","intent":"REJECT"}'
    elif invalid == 'invalid_unicode':
        call.function.arguments = '{"proposal_id":"\\ud800","intent":"CONFIRM"}'
    elif invalid == 'missing_intent':
        call.function.arguments = '{"proposal_id":"' + PROPOSAL + '"}'
    elif invalid == 'extra_arguments':
        call.function.arguments = '{"proposal_id":"' + PROPOSAL + '","intent":"CONFIRM","private":"' + PRIVATE + '"}'
    elif invalid == 'wrong_proposal':
        call.function.arguments = '{"proposal_id":"' + PRIVATE + '","intent":"CONFIRM"}'
    with pytest.raises(ManagedExecutionError) as error:
        validate_pending_decision(selected, PROPOSAL,
            presence_reply=invalid == 'presence_confirm', runtime=runtime)
    assert error.value.code == code
    assert observed == [{'stage': 'TOOL_INPUT', 'capability': 'business_decide_proposal',
        'refusal_category': 'VALIDATION_ERROR', 'schema_rule': rule, 'schema_field': field}]
    assert PRIVATE not in repr(observed) and PROPOSAL not in repr(observed)


@pytest.mark.parametrize('field', ['proposal_id', 'intent', 'tool_calls', 'tool_call_id',
                                   'tool_name', 'type', 'assistant_message', 'envelope'])
def test_semantic_rejection_diagnostic_survives_closed_log_boundary(field):
    import logging
    value = ManagedBusinessDiagnostic(turn_id=TURN_ID, stage='TOOL_INPUT',
        capability='business_decide_proposal', refusal_category='VALIDATION_ERROR',
        lookup_source='NOT_APPLICABLE', lookup_mode='NOT_APPLICABLE', lookup_result='NOT_RUN',
        proposal_present=True, operation_present=False, schema_rule='const', schema_field=field)
    raw = logging.LogRecord('untrusted', 20, PRIVATE, 0, PRIVATE, (), None)
    raw.__dict__.update(_business_identity=_IDENTITY, _business_diagnostic=value, private=PRIVATE)
    safe = _trusted_business_record(raw)
    assert safe is not None and safe.schema_field == field
    assert safe.capability == 'business_decide_proposal'
    assert PRIVATE not in repr(safe.__dict__)


@pytest.mark.integration
@pytest.mark.parametrize('invalid,field', [
    ('wrong_target', 'proposal_id'), ('reasoning', 'assistant_message'), ('content', 'envelope'),
])
@pytest.mark.parametrize('observer_fails', [False, True])
async def test_pending_rejection_is_observable_before_any_effect(
        api_client, db_session, monkeypatch, invalid, field, observer_fails):
    from sqlalchemy import select
    from models.business import BusinessOperation
    from schemas.managed_turn import ManagedTurnRequest
    from services.business_runtime import BusinessRuntime
    from tests.fakes import build_llm_response
    from tests.test_business_runtime import later
    from tests.test_managed_turns import execute
    from tests.test_voice_pending_prose_repair import prepared

    value, proposal_id, _, calls, effects, model = await prepared(api_client, db_session, monkeypatch)
    observed = []
    original_observe = BusinessRuntime.observe_business_diagnostic

    def observe(self, **fields):
        if fields['capability'] == 'business_decide_proposal':
            observed.append(fields)
            if observer_fails:
                raise RuntimeError(PRIVATE)
        return original_observe(self, **fields)

    monkeypatch.setattr(BusinessRuntime, 'observe_business_diagnostic', observe)
    response = build_llm_response(tool_calls=[decision(
        PRIVATE if invalid == 'wrong_target' else proposal_id, 'CONFIRM')])
    if invalid == 'reasoning':
        response.choices[0].message.reasoning_details = {}
    elif invalid == 'content':
        response.choices[0].message.content = '\ud800'
    model.chat.completions.queue(response)
    request = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю запись.')
    original_request = request.model_dump_json()
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    assert result['status'] == 'FAILED' and result['error_code'] == 'INVALID_RESULT'
    assert len(model.chat.completions.calls) == 2
    assert calls['execute'] == 0 and not effects.permit_requests
    assert request.model_dump_json() == original_request
    assert len(observed) == 1 and observed[0]['schema_field'] == field
    assert PRIVATE not in repr(observed) and proposal_id not in repr(observed)
    async with db_session() as db:
        operation = (await db.scalars(select(BusinessOperation))).one()
        assert operation.status == 'RESERVED' and operation.attempt_count == 0
