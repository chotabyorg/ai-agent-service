"""One model READ with ordinary sequential guarded reads; no CRM capability."""
from business_capabilities.registry import REGISTRY, normalize_arguments
from business_capabilities.schemas import BookingCustomer
from schemas.business import BusinessSafetyError, BusinessToolResult

TOOL_NAME = 'booking_search_options'
REQUIRED_CAPABILITIES = frozenset({'catalog.search', 'resource.search'})


def search_tool(*, include_customer=False):
    tool = {'type': 'function', 'function': {
        'name': TOOL_NAME,
        'description': (
            'READ: Prefer this for initial booking selection when BOTH the service and specialist '
            'must be found: get both real search results in one call. This does not check availability '
            'or prepare or execute a booking. catalog searches the service name; resources searches '
            'the specialist name, never the service or profession. For no specialist preference use '
            '{"catalog":{"query":"стрижка","type":"SERVICE"},"resources":{}}. '
            'For a named specialist use resources={"query":"Анна"}. Both objects are required; '
            'each accepts only its advertised search fields. An empty object browses. Copy returned '
            'refs into booking preparation. If only one search needs changing or paging, use its '
            'individual catalog_search or resource_search tool. Do not repeat an unchanged search.'),
        'parameters': {'type': 'object', 'additionalProperties': False,
            'required': ['catalog', 'resources'],
            'properties': {'catalog': REGISTRY['catalog.search'].tool()['function']['parameters'],
                           'resources': REGISTRY['resource.search'].tool()['function']['parameters']}}}}
    if include_customer:
        tool['function']['parameters']['properties']['customer'] = (
            REGISTRY['booking.create'].tool()['function']['parameters']['properties']['customer'])
        tool['function']['description'] += (
            ' When the booking customer name and exact phone/email are already known, include '
            'customer={display_name,identity} to check the existing contact in this same READ. '
            'Use a SINGLE result customer ref explicitly in your subsequent booking proposal. '
            'For NONE keep the supplied customer details; this does not create a customer. '
            'Never invent a name/contact or repeat already known facts just to fill this option. '
            'Identity/name conflicts, ambiguity or unavailable lookup require clarification, not a new customer.')
    return tool


def normalize_search_arguments(arguments):
    if (not isinstance(arguments, dict) or not {'catalog', 'resources'} <= set(arguments)
            or set(arguments) - {'catalog', 'resources', 'customer'}):
        raise BusinessSafetyError('ACTION_INVALID')
    # Validate BOTH before the first read, keeping the canonical normalizers and
    # cursor/page rules. No partially valid compound input reaches a provider.
    normalized = {'catalog': normalize_arguments('catalog.search', arguments['catalog']),
                  'resources': normalize_arguments('resource.search', arguments['resources'])}
    if 'customer' in arguments:
        try:
            normalized['customer'] = BookingCustomer.model_validate(arguments['customer']).model_dump(mode='json')
        except (TypeError, ValueError):
            raise BusinessSafetyError('ACTION_INVALID') from None
    return normalized


async def search_options(runtime, arguments):
    normalized = normalize_search_arguments(arguments)
    if not REQUIRED_CAPABILITIES <= set(runtime.binding.enabled_capabilities):
        raise BusinessSafetyError('CAPABILITY_DISABLED')
    if ('customer' in normalized and ('customer.find' not in runtime.binding.enabled_capabilities
            or runtime.registration.capability('customer.find') is None)):
        raise BusinessSafetyError('CAPABILITY_DISABLED')
    results = {}
    for key, tool in (('catalog', 'catalog_search'), ('resources', 'resource_search')):
        # Each existing call owns all of its pre/post guards, fresh authority,
        # normalization and deadline. Shared ManagedRuntime has one task owner:
        # do not replace this sequence with gather or cache a guard/witness.
        result = await runtime.call_tool(tool, normalized[key])
        if (result.halt_status is not None or result.safe_draft is not None
                or not isinstance(result.tool_payload, dict) or result.tool_payload.get('status') != 'OK'):
            return result
        results[key] = result.tool_payload
    if 'customer' in normalized:
        from services.managed_preparation import lookup_booking_customer
        result = await lookup_booking_customer(runtime, normalized['customer'],
                                               name=TOOL_NAME, return_lookup=True)
        if (result.halt_status is not None or result.safe_draft is not None
                or not isinstance(result.tool_payload, dict) or result.tool_payload.get('status') != 'OK'):
            return result
        results['customer'] = result.tool_payload
    return BusinessToolResult(tool_payload={'status': 'OK', **results})
