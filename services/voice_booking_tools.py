"""Reduce duplicate model schemas without changing canonical business inputs."""
from copy import deepcopy


def catalog_offering_choices(name, payload):
    """Ephemeral model hints from this turn's guarded read, never admission facts."""
    if name not in {'catalog_search', 'booking_search_options'}:
        return None
    if not isinstance(payload, dict) or payload.get('status') != 'OK':
        return ()
    catalog = payload.get('catalog') if name == 'booking_search_options' else payload
    if not isinstance(catalog, dict) or catalog.get('status') != 'OK':
        return ()
    data = catalog.get('data')
    items = data.get('items') if isinstance(data, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        return ()
    refs = [item.get('ref') if isinstance(item, dict) else None for item in items]
    if any(not isinstance(ref, str) or not 1 <= len(ref) <= 256 for ref in refs):
        return ()
    return tuple(dict.fromkeys(refs))


def booking_offering_tools(tools, refs):
    """Narrow only advertised choices. Canonical schemas and CRM checks stay fresh."""
    if not refs:
        return tools
    result = []
    for tool in tools:
        if tool['function']['name'] == 'booking_create':
            tool = deepcopy(tool)
            properties = tool['function'].get('parameters', {}).get('properties', {})
            if 'offering_ref' in properties:
                properties['offering_ref']['enum'] = list(refs)
        result.append(tool)
    return result


def booking_tools(tools, *, enabled):
    if not enabled:
        return tools
    by_name = {tool['function']['name']: tool for tool in tools}
    propose = by_name.get('business_propose')
    if propose is None or 'business_decide_proposal' not in by_name:
        return tools
    branches = (propose['function'].get('parameters', {}).get('properties', {})
                .get('actions', {}).get('items', {}).get('oneOf', []))
    direct = {'booking.create': 'booking_create', 'booking.reschedule': 'booking_reschedule',
              'booking.cancel': 'booking_cancel'}
    if not branches:
        return tools
    seen = set()
    for branch in branches:
        properties = branch.get('properties', {})
        capability = properties.get('capability', {}).get('const')
        name = direct.get(capability)
        # Keep generic proposals for bundles, unfamiliar contracts or missing
        # direct tools. Only identical advertised booking inputs are redundant.
        if (name is None or capability in seen or name not in by_name
                or properties.get('arguments') != by_name[name]['function'].get('parameters')):
            return tools
        seen.add(capability)
    return [tool for tool in tools if tool['function']['name'] != 'business_propose']
