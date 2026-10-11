"""Canonical tool surface, separate from durable business admission mechanics."""
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select
from models.business import BusinessOperation
from schemas.business import BusinessAction, BusinessSafetyError, BusinessToolResult
from business_capabilities.registry import CAPABILITIES, WIRE_REGISTRY, Risk, tool_schema
from business_capabilities.chotacrm_adapter import action_key
from business_capabilities.context import BusinessRuntimeError, failure


def completed_booking_reply(operation, receipt):
    """Historical success wording from a validated retained canonical receipt."""
    from business_capabilities.results import validate_result

    result = validate_result('booking.create', receipt.evidence.get('result'), write=True)
    if (result.get('status') != 'OK' or result.get('effect') not in {'APPLIED', 'REPLAYED'}
            or result.get('operation_id') != str(operation.id)
            or result.get('operation_ref') != receipt.receipt_id
            or result['data']['ref'] != receipt.object_id
            or result['data']['status'] != 'CONFIRMED'):
        raise BusinessSafetyError('RECEIPT_INVALID', processing_status='UNKNOWN')
    booking = result['data']
    start = datetime.fromisoformat(booking['start_at'].replace('Z', '+00:00'))
    time = start.astimezone(ZoneInfo(booking['timezone'])).strftime('%d.%m.%Y в %H:%M')
    details = booking.get('offering_name') or 'услуга'
    if booking.get('resource_name'):
        details += f", специалист {booking['resource_name']}"
    return f"Запись была подтверждена: {details}, {time} ({booking['timezone']})."


def canonical_tools(runtime, controls):
    available = [c for c in CAPABILITIES if c.id in runtime.binding.enabled_capabilities]
    result = []
    for capability in available:
        tool = capability.tool()
        tool['function']['description'] = capability.risk.value + ': ' + capability.description
        if capability.id == 'booking.create':
            tool['function']['description'] += ' For a new customer pass their name and exact phone/email in customer; this atomically records the customer and optional inquiry with the booking after confirmation. Do not create a separate customer first in this Turn.'
        result.append(tool)
    # Model never chooses a provider or arbitrary plan. The one supported bundle
    # combines at most one booking and one order under the same agreement.
    propose = next(t for t in controls if t['function']['name'] == 'business_propose')
    propose['function']['description'] = (
        'Prepare and persist a proposal BEFORE asking the customer to confirm. '
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


async def call_capability(runtime, name, arguments):
    capability = WIRE_REGISTRY.get(name)
    if capability is None:
        if name == 'business_propose':
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
        return None
    if capability.id not in runtime.binding.enabled_capabilities:
        raise BusinessSafetyError('CAPABILITY_DISABLED')
    action = BusinessAction(capability=capability.id,
        arguments=runtime.registration.capability(capability.id).normalize(arguments))
    if capability.risk != Risk.READ:
        try:
            return await runtime.execute_plan([action])
        except BusinessRuntimeError as error:
            # Only a pre-reservation provider rejection can be ordinary business
            # feedback. Existing effects/proposals keep the durable result guard.
            if runtime.collector.snapshot():
                raise BusinessSafetyError('FACTS_UNAVAILABLE', processing_status='UNKNOWN') from None
            return BusinessToolResult(tool_payload=failure(error.code))
    gate = await runtime.guard_result(allow_incomplete=True)
    if gate is not None:
        return gate
    # Recheck current owner configuration under the existing short admission lock
    # order. Provider independently rechecks grant/tenant/projection authority.
    async with runtime.repository._session(decisive=True) as db:
        async with db.begin():
            binding, _ = await runtime.repository._authority(db, runtime.request, runtime.grant)
            if capability.id not in binding.enabled_capabilities:
                raise BusinessSafetyError('CAPABILITY_DISABLED')
    try:
        result = await runtime._adapter_call('read', runtime.scope, action)
    except BusinessRuntimeError as error:
        result = failure(error.code)
    gate = await runtime.guard_result(allow_incomplete=True)
    return gate or BusinessToolResult(tool_payload=result)


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
