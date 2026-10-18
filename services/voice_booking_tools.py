"""Reduce duplicate model schemas without changing canonical business inputs."""


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
