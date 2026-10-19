"""Canonical tool surface, separate from durable business admission mechanics."""
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from models.business import BusinessOperation
from schemas.business import BusinessAction, BusinessSafetyError, BusinessToolResult
from business_capabilities.registry import CAPABILITIES, WIRE_REGISTRY, Risk, tool_schema
from business_capabilities.chotacrm_adapter import action_key
from business_capabilities.context import BusinessRuntimeError, failure


def completed_booking_reply(operation, receipt):
    """Historical success wording from a validated retained canonical receipt."""
    from business_capabilities.results import validate_result

    capability = operation.command.capability
    wording = {
        'booking.create': ('CONFIRMED', 'Запись была подтверждена'),
        'booking.reschedule': ('CONFIRMED', 'Запись была перенесена'),
        'booking.cancel': ('CANCELLED', 'Запись была отменена'),
    }
    status, prefix = wording[capability]
    result = validate_result(capability, receipt.evidence.get('result'), write=True)
    if (result.get('status') != 'OK' or result.get('effect') not in {'APPLIED', 'REPLAYED'}
            or result.get('operation_id') != str(operation.id)
            or result.get('operation_ref') != receipt.receipt_id
            or result['data']['ref'] != receipt.object_id
            or result['data']['status'] != status):
        raise BusinessSafetyError('RECEIPT_INVALID', processing_status='UNKNOWN')
    booking = result['data']
    start = datetime.fromisoformat(booking['start_at'].replace('Z', '+00:00'))
    time = start.astimezone(ZoneInfo(booking['timezone'])).strftime('%d.%m.%Y в %H:%M')
    details = booking.get('offering_name') or 'услуга'
    if booking.get('resource_name'):
        details += f", специалист {booking['resource_name']}"
    return f"{prefix}: {details}, {time} ({booking['timezone']})."


def canonical_tools(runtime, controls):
    available = [c for c in CAPABILITIES if c.id in runtime.binding.enabled_capabilities]
    readable = [c for c in available if c.risk == Risk.READ]
    selected = readable if runtime.recovery_mode or not runtime.effect_eligible else available
    result = []
    for capability in selected:
        tool = capability.tool()
        tool['function']['description'] = capability.risk.value + ': ' + capability.description
        result.append(tool)
    if runtime.recovery_mode:
        result.append(runtime._status_tool())
        return result
    if not runtime.effect_eligible:
        return result
    # Model never chooses a provider or arbitrary plan. The one supported bundle
    # combines at most one booking and one order under the same agreement.
    propose = next(t for t in controls if t['function']['name'] == 'business_propose')
    propose['function']['description'] = (
        'Prepare and persist a proposal BEFORE asking the customer to confirm. '
        'For booking.create, pass supplied customer details directly: runtime checks the exact phone/email automatically. '
        'Do not add a separate customer_find solely for this preflight. '
        'If runtime returns CUSTOMER_REUSE_REQUIRED, make a separate proposal call using its customer_ref; clarify conflicts or ambiguity. '
        'CRM records a new customer and booking together only after confirmation. '
        'Never call customer_create first or rename or duplicate a customer. '
        'Propose one business action, or booking.create plus deal.create for the SAME EXISTING customer_ref. '
        'Other combinations are unsupported. Present the returned frozen business summary, '
        'ask once for confirmation, and wait for a later customer turn. '
        'Preparing a proposal does not execute provider effects; execution requires that later confirmation.'
    )
    actions = [{'type': 'object', 'additionalProperties': False, 'required': ['capability','arguments'],
                'properties': {'capability': {'const': c.id}, 'arguments': tool_schema(c.input)}}
               for c in available if c.risk == Risk.WRITE_CONFIRM]
    if actions:
        propose['function']['parameters']['properties']['actions'].update(maxItems=2, items={'oneOf': actions})
        result.append(propose)
    result.extend(t for t in controls if t['function']['name'] == 'business_decide_proposal')
    return result


def _lookup_refusal_category(result):
    if getattr(result, 'halt_status', None):
        return 'AUTHORITY_BLOCKED'
    payload = getattr(result, 'tool_payload', None)
    if not isinstance(payload, dict):
        return 'LOOKUP_INVALID'
    if payload.get('status') == 'ERROR':
        code = payload.get('error', {}).get('code') if isinstance(payload.get('error'), dict) else None
        if code == 'IDENTITY_CONFLICT':
            return 'IDENTITY_CONFLICT'
        if code in {'AMBIGUOUS', 'AMBIGUOUS_IDENTITY'}:
            return 'AMBIGUOUS_IDENTITY'
        return 'LOOKUP_UNAVAILABLE'
    data = payload.get('data')
    if payload.get('status') == 'AMBIGUOUS' or (isinstance(data, dict) and data.get('match') == 'AMBIGUOUS'):
        return 'AMBIGUOUS_IDENTITY'
    if not isinstance(data, dict) or not isinstance(data.get('candidates'), list):
        return 'LOOKUP_INVALID'
    count = len(data['candidates'])
    if payload.get('status') != 'OK' or not (
            data.get('match') == 'NONE' and count == 0
            or data.get('match') == 'SINGLE' and count == 1):
        return 'LOOKUP_INVALID'
    return 'NONE'


def _observe_model_customer_lookup(runtime, arguments, *, result=None, refusal='NONE', lookup_result=None):
    if lookup_result is None:
        payload = getattr(result, 'tool_payload', None)
        data = payload.get('data') if isinstance(payload, dict) else None
        match = data.get('match') if isinstance(data, dict) else None
        if (getattr(result, 'halt_status', None) is not None
                or not isinstance(payload, dict) or payload.get('status') == 'ERROR'):
            lookup_result = 'ERROR'
        elif match in {'NONE', 'SINGLE', 'AMBIGUOUS'}:
            lookup_result = match
        else:
            lookup_result = 'ERROR'
            refusal = 'LOOKUP_INVALID'
    try:
        runtime.observe_business_diagnostic(
            stage='LOOKUP', capability='customer.find', refusal_category=refusal,
            lookup_source='MODEL', lookup_mode='NAME_FILTERED'
            if isinstance(arguments, dict) and arguments.get('name') is not None else 'IDENTITY_ONLY',
            lookup_result=lookup_result,
        )
    except Exception:
        pass


_REFUSAL_BY_ERROR = {
    'IDENTITY_CONFLICT': 'IDENTITY_CONFLICT',
    'AMBIGUOUS_IDENTITY': 'AMBIGUOUS_IDENTITY', 'AMBIGUOUS': 'AMBIGUOUS_IDENTITY',
    'BOOKING_CONFLICT': 'BOOKING_CONFLICT', 'STATE_CONFLICT': 'STATE_CONFLICT',
    'NOT_FOUND': 'NOT_FOUND', 'PERMISSION_DENIED': 'PERMISSION_DENIED',
    'PROVIDER_UNAVAILABLE': 'PROVIDER_UNAVAILABLE',
    'VALIDATION_ERROR': 'VALIDATION_ERROR', 'ACTION_INVALID': 'VALIDATION_ERROR',
    'PROPOSAL_BOUNDS': 'VALIDATION_ERROR', 'STALE_CUSTOMER_CONTEXT': 'STALE_CUSTOMER_CONTEXT',
}


def _observe_proposal(runtime, name, *, result=None, error=None):
    try:
        code = getattr(error, 'code', None)
        payload = getattr(result, 'tool_payload', None)
        if code is None and isinstance(payload, dict) and isinstance(payload.get('error'), dict):
            code = payload['error'].get('code')
        refusal = _REFUSAL_BY_ERROR.get(code, 'OTHER') if code else 'NONE'
        status = payload.get('status') if isinstance(payload, dict) else None
        proposal_present = status in {'AWAITING_CONFIRMATION', 'PREPARED', 'CONFIRMED', 'SUCCEEDED'}
        operation_present = False
        collector = getattr(runtime, 'collector', None)
        if collector is not None:
            operation_present = bool(collector.snapshot())
        runtime.observe_business_diagnostic(
            stage='PROPOSAL', capability=name, refusal_category=refusal,
            proposal_present=proposal_present, operation_present=operation_present,
        )
    except Exception:
        pass


async def call_capability(runtime, name, arguments, *, lookup_source='MODEL'):
    if name in {'booking_create', 'business_propose'}:
        from services.managed_preparation import prepare_booking_customer_reuse
        prepared = await prepare_booking_customer_reuse(runtime, name, arguments)
        if prepared is not None:
            return prepared

    if name == 'customer_find' and lookup_source == 'MODEL':
        try:
            normalized_arguments = runtime.registration.capability('customer.find').normalize(arguments)
        except Exception:
            _observe_model_customer_lookup(runtime, {}, refusal='VALIDATION_ERROR', lookup_result='NOT_RUN')
            raise
        try:
            result = await _call_capability_impl(runtime, name, normalized_arguments)
        except BusinessSafetyError as error:
            refusal = ('CAPABILITY_DISABLED' if error.code == 'CAPABILITY_DISABLED'
                       else 'AUTHORITY_BLOCKED' if error.processing_status == 'UNKNOWN'
                       or error.code in {'BINDING_UNAVAILABLE', 'EFFECT_AUTHORITY_UNAVAILABLE'}
                       else 'LOOKUP_UNAVAILABLE')
            _observe_model_customer_lookup(runtime, normalized_arguments, refusal=refusal,
                lookup_result='NOT_RUN' if refusal in {'CAPABILITY_DISABLED', 'AUTHORITY_BLOCKED'} else 'ERROR')
            raise
        except (TypeError, ValueError):
            _observe_model_customer_lookup(runtime, normalized_arguments, refusal='LOOKUP_INVALID', lookup_result='ERROR')
            raise
        except Exception:
            _observe_model_customer_lookup(runtime, normalized_arguments, refusal='LOOKUP_UNAVAILABLE', lookup_result='ERROR')
            raise
        refusal = _lookup_refusal_category(result)
        _observe_model_customer_lookup(runtime, normalized_arguments, result=result, refusal=refusal)
        if refusal in {'LOOKUP_UNAVAILABLE', 'LOOKUP_INVALID', 'AMBIGUOUS_IDENTITY', 'IDENTITY_CONFLICT'}:
            from services.managed_preparation import _preflight_feedback_guard, _safe_lookup_draft
            # A failed lookup is not NONE. Stop the model before it improvises a
            # customer.create and strands the following booking behind that write.
            gate = await _preflight_feedback_guard(runtime)
            if gate is not None:
                return gate
            return BusinessToolResult(safe_draft=_safe_lookup_draft(refusal))
        return result
    try:
        result = await _call_capability_impl(runtime, name, arguments)
    except BusinessSafetyError as error:
        if name in {'booking_create', 'business_propose'}:
            _observe_proposal(runtime, name, error=error)
        raise
    except Exception:
        if name in {'booking_create', 'business_propose'}:
            _observe_proposal(runtime, name)
        raise
    if name in {'booking_create', 'business_propose'}:
        _observe_proposal(runtime, name, result=result)
    return result


async def _call_capability_impl(runtime, name, arguments):
    """Validate the selected action and recheck authority before CRM access."""
    capability = WIRE_REGISTRY.get(name)
    if capability is None:
        if name == 'business_propose':
            if set(arguments) != {'actions'} or not isinstance(arguments['actions'], list):
                raise BusinessSafetyError('ACTION_INVALID')
            actions = arguments.get('actions', [])
            if (not 1 <= len(actions) <= 2 or any(not isinstance(a, dict) for a in actions)
                    or len({a.get('capability','').split('.')[0] for a in actions}) != len(actions)):
                raise BusinessSafetyError('ACTION_INVALID')
            if len(actions) == 2:
                by_name = {a.get('capability'): a.get('arguments', {}) for a in actions}
                booking, deal = by_name.get('booking.create'), by_name.get('deal.create')
                if (not isinstance(booking, dict) or not isinstance(deal, dict)
                        or not booking.get('customer_ref') or booking.get('customer_ref') != deal.get('customer_ref')
                        or 'customer' in booking):
                    raise BusinessSafetyError('ACTION_INVALID')
            try:
                return await runtime.propose_plan(actions)
            except BusinessRuntimeError as error:
                if runtime.collector.snapshot():
                    raise BusinessSafetyError('FACTS_UNAVAILABLE', processing_status='UNKNOWN') from None
                return BusinessToolResult(tool_payload=failure(error.code))
        return None
    if capability.id not in runtime.binding.enabled_capabilities:
        raise BusinessSafetyError('CAPABILITY_DISABLED')
    try:
        action = BusinessAction(capability=capability.id,
            arguments=runtime.registration.capability(capability.id).normalize(arguments))
    except (TypeError, ValueError):
        runtime.observe_business_diagnostic(stage='TOOL_INPUT', capability=capability.id,
            refusal_category='VALIDATION_ERROR')
        raise
    if capability.id == 'booking.check_availability':
        runtime._booking_workflow_started = True
    if capability.id == 'customer.create' and getattr(runtime, '_booking_workflow_started', False):
        from services.managed_preparation import _preflight_feedback_guard
        # A separate safe write would consume this turn's one immutable plan.
        # Redirect before reserving anything, while preserving retained effects.
        gate = await _preflight_feedback_guard(runtime)
        if gate is not None:
            return gate
        return BusinessToolResult(tool_payload={
            'status': 'BOOKING_AGGREGATE_REQUIRED',
            'instruction': 'No customer was created. Continue booking_create or business_propose '
                           'with customer details (or a verified customer_ref). '
                           'Do not call customer_create as a booking prerequisite.',
        })
    if capability.risk != Risk.READ:
        try:
            return await runtime.execute_plan([action])
        except BusinessRuntimeError as error:
            # Only a pre-reservation provider rejection can be ordinary business
            # feedback. Existing effects/proposals keep the durable result guard.
            if runtime.collector.snapshot():
                raise BusinessSafetyError('FACTS_UNAVAILABLE', processing_status='UNKNOWN') from None
            return BusinessToolResult(tool_payload=failure(error.code))
    gate = await runtime.execution_guard(allow_incomplete=True)
    if gate is not None:
        return gate
    # Recheck current owner configuration under the existing short admission lock
    # order. Provider independently rechecks grant/tenant/projection authority.
    await _authorize_read(runtime, capability.id)
    try:
        result = await runtime._adapter_call('read', runtime.scope, action)
    except BusinessRuntimeError as error:
        runtime.observe_business_diagnostic(stage='TOOL_READ', capability=capability.id,
            refusal_category=_REFUSAL_BY_ERROR.get(error.code, 'OTHER'))
        result = failure(error.code)
    except BusinessSafetyError as error:
        runtime.observe_business_diagnostic(stage='TOOL_READ', capability=capability.id,
            refusal_category='CAPABILITY_DISABLED' if error.code == 'CAPABILITY_DISABLED' else 'AUTHORITY_BLOCKED')
        raise
    except (TypeError, ValueError):
        runtime.observe_business_diagnostic(stage='TOOL_READ', capability=capability.id,
            refusal_category='LOOKUP_INVALID')
        raise
    gate = await runtime.execution_guard(allow_incomplete=True)
    return gate or BusinessToolResult(tool_payload=result)


async def _authorize_read(runtime, capability_id):
    """Reload authority under compatible config-reader locks before a CRM read.

    No provider call or write reservation occurs inside this transaction. Keep
    the existing lock order and fresh authority checks. Shared config locks
    exclude configuration writers without making independent reads wait for
    one another. A bounded writer wait still fails closed, without a retry.
    """
    try:
        async with runtime.repository._session() as db:
            async with db.begin():
                await db.execute(text("SET LOCAL lock_timeout = '250ms'"))
                binding, _ = await runtime.repository._authority(
                    db, runtime.request, runtime.grant, shared_config_locks=True)
                if capability_id not in binding.enabled_capabilities:
                    raise BusinessSafetyError('CAPABILITY_DISABLED')
    except DBAPIError as error:
        if not runtime.repository._contention(error):
            raise
        raise BusinessSafetyError('ADMISSION_CONTENDED') from None


async def successful_results(runtime, plan):
    results = []
    async with runtime.repository._session() as db:
        for operation in plan.operations:
            row = await db.get(BusinessOperation, operation.id)
            if row is None or row.scope_id != runtime.scope.scope_id or row.status != 'SUCCEEDED':
                raise BusinessSafetyError('RECEIPT_CONFLICT', processing_status='UNKNOWN')
            # This is already a closed canonical projection stored by the adapter.
            result = row.receipt_evidence.get('evidence', {}).get('result')
            if not result:
                raise BusinessSafetyError('RECEIPT_CONFLICT', processing_status='UNKNOWN')
            results.append(dict(capability=operation.command.capability, result=result))
    return BusinessToolResult(tool_payload=dict(status='SUCCEEDED', results=results))


def proposal_summary(plan):
    parts = []
    for operation in plan.operations:
        command = operation.command
        action = BusinessAction(capability=command.capability, arguments=command.arguments)
        summary = command.facts['actions'][action_key(action)]['summary']
        if command.capability.startswith('booking.'):
            tz = ZoneInfo(summary['timezone'])
            time = datetime.fromisoformat(summary['start_at'].replace('Z','+00:00')).astimezone(tz).strftime('%d.%m в %H:%M')
            service = summary.get('service') or summary.get('offering_name') or 'услуга'
            specialist = summary.get('resource') or summary.get('resource_name')
            verb = {'booking.create': 'Записать вас', 'booking.reschedule': 'Перенести запись', 'booking.cancel': 'Отменить запись'}[command.capability]
            parts.append(f'{verb} на {service} {time}' + (f', мастер — {specialist}' if specialist else ''))
            if summary.get('price') and summary.get('currency'):
                parts[-1] += f", стоимость {summary['price']} {summary['currency']}"
            if command.arguments.get('customer'):
                customer = command.arguments['customer']
                parts[-1] += f". Имя: {summary['customer']}, контакт: {customer['identity']['value']}"
            if summary.get('inquiry_summary'):
                parts[-1] += f". Ваш запрос: {summary['inquiry_summary']}"
        elif command.capability == 'deal.create':
            items = ', '.join(f"{item['name']} × {item['quantity']} ({item['unit_price']} {summary['currency']})" for item in summary['items'])
            parts.append(f"Оформить заказ «{summary['title']}»: {items}")
        elif command.capability == 'deal.move':
            stage = {'NEW':'новый','IN_PROGRESS':'в работе','WON':'выполнен','LOST':'отменён'}[summary['stage']]
            parts.append(f"Изменить статус заказа «{summary['title']}» на «{stage}»")
    text = '. '.join(parts) + '.'
    if not parts or len(text) > 8000:
        raise BusinessSafetyError('PROPOSAL_BOUNDS')
    return text
