"""Advertise the current decision target and diagnose strict admission failures."""

from copy import deepcopy

from services.managed_tools import ManagedExecutionError, parse_managed_tool_arguments


def pending_decision_tools(tools, proposal_id, *, presence_reply=False):
    """Narrow only the model-facing copy; canonical tools and IDs stay unchanged."""
    result = []
    for tool in tools:
        if tool['function']['name'] == 'business_decide_proposal':
            tool = deepcopy(tool)
            properties = tool['function']['parameters']['properties']
            properties['proposal_id']['enum'] = [proposal_id]
            if presence_reply:
                properties['intent']['enum'] = ['UNRELATED']
        result.append(tool)
    return result


def observe_pending_rejection(runtime, rule, field):
    """Only code-owned categories; never arguments, model prose or proposal IDs."""
    try:
        runtime.business.observe_business_diagnostic(
            stage='TOOL_INPUT', capability='business_decide_proposal',
            refusal_category='VALIDATION_ERROR', schema_rule=rule, schema_field=field)
    except Exception:
        # Observations cannot change the result, including a failing observer.
        pass


def validate_pending_decision(selected, proposal_id, *, presence_reply, runtime):
    """Preserve the existing fail-closed pre-dispatch checks without a retry."""
    def reject(rule, field):
        observe_pending_rejection(runtime, rule, field)
        raise ManagedExecutionError('INVALID_RESULT')

    calls = getattr(selected.message, 'tool_calls', None)
    if selected.branch != 'TOOL_CALLS' or type(calls) is not list:
        reject('type', 'tool_calls')
    if len(calls) != 1:
        reject('minItems' if not calls else 'maxItems', 'tool_calls')
    call = calls[0]
    function = getattr(call, 'function', None)
    if getattr(call, 'type', None) != 'function':
        reject('const', 'type')
    if not isinstance(getattr(call, 'id', None), str) or not call.id:
        reject('minLength', 'tool_call_id')
    if getattr(function, 'name', None) != 'business_decide_proposal':
        reject('const', 'tool_name')
    try:
        arguments = parse_managed_tool_arguments(getattr(function, 'arguments', None))
    except ManagedExecutionError:
        observe_pending_rejection(runtime, 'format', 'arguments')
        raise
    required = {'proposal_id', 'intent'}
    if set(arguments) != required:
        reject('required' if not required <= set(arguments) else 'additionalProperties', 'ROOT')
    if arguments['proposal_id'] != proposal_id:
        reject('const', 'proposal_id')
    if presence_reply and arguments['intent'] != 'UNRELATED':
        reject('enum', 'intent')
