"""One model READ, two ordinary sequential guarded reads; no new CRM capability."""
from business_capabilities.registry import REGISTRY, normalize_arguments
from schemas.business import BusinessSafetyError, BusinessToolResult

TOOL_NAME = 'booking_search_options'
REQUIRED_CAPABILITIES = frozenset({'catalog.search', 'resource.search'})


def search_tool():
    return {'type': 'function', 'function': {
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


def normalize_search_arguments(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {'catalog', 'resources'}:
        raise BusinessSafetyError('ACTION_INVALID')
    # Validate BOTH before the first read, keeping the canonical normalizers and
    # cursor/page rules. No partially valid compound input reaches a provider.
    return {'catalog': normalize_arguments('catalog.search', arguments['catalog']),
            'resources': normalize_arguments('resource.search', arguments['resources'])}


async def search_options(runtime, arguments):
    normalized = normalize_search_arguments(arguments)
    if not REQUIRED_CAPABILITIES <= set(runtime.binding.enabled_capabilities):
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
    return BusinessToolResult(tool_payload={'status': 'OK', **results})
