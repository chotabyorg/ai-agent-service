"""Validate a model-authored continuation of a side-effect-free preparation."""
from copy import deepcopy

from services.managed_tools import ManagedExecutionError


def customer_reuse_arguments(name, arguments, payload):
    """Return validation expectations, never dispatch or rewrite a tool call.

    The ordinary runtime already validated the exact customer lookup. The model
    must explicitly author the same preparation using that returned reference.
    No new customer, different booking, additional action or prose can replace it.
    """
    if not isinstance(payload, dict) or payload.get('status') != 'CUSTOMER_REUSE_REQUIRED':
        return None
    ref = payload.get('customer_ref')
    if not isinstance(ref, str) or not ref or len(ref) > 256:
        raise ManagedExecutionError('INVALID_RESULT')
    expected = deepcopy(arguments)
    if name == 'booking_create':
        booking = expected
    elif (name == 'business_propose' and set(expected) == {'actions'}
            and len(expected['actions']) == 1
            and expected['actions'][0]['capability'] == 'booking.create'):
        booking = expected['actions'][0]['arguments']
    else:
        raise ManagedExecutionError('INVALID_RESULT')
    if 'customer' not in booking or 'customer_ref' in booking:
        raise ManagedExecutionError('INVALID_RESULT')
    del booking['customer']
    booking['customer_ref'] = ref
    return expected
