"""Turn-local, bounded reference provenance; never an admission decision."""
import hashlib

_LIMIT = 96
_READ_ROLES = {'catalog.search': 'offering', 'resource.search': 'resource', 'customer.find': 'customer'}


def _digest(value):
    if type(value) is not str or not 1 <= len(value) <= 512:
        return None
    return hashlib.sha256(value.encode('utf-8')).digest()


def observe_read_references(runtime, capability, payload):
    """Remember only validated successful READ refs after the fresh post-guard."""
    try:
        role = _READ_ROLES.get(capability)
        if role is None or type(payload) is not dict or payload.get('status') != 'OK':
            return
        data = payload.get('data')
        if type(data) is not dict:
            return
        if role == 'customer':
            if data.get('match') != 'SINGLE':
                return
            values = data.get('candidates')
            if type(values) is not list or len(values) != 1:
                return
        else:
            values = data.get('items')
            if type(values) is not list or len(values) > 20:
                return
        state = getattr(runtime, '_reference_observations', None)
        if state is None:
            state = {key: {'digests': set(), 'limited': False} for key in ('offering', 'resource', 'customer')}
            runtime._reference_observations = state
        bucket = state[role]
        for value in values:
            digest = _digest(value.get('ref')) if type(value) is dict else None
            if digest is None or digest in bucket['digests']:
                continue
            if len(bucket['digests']) >= _LIMIT:
                bucket['limited'] = True
            else:
                bucket['digests'].add(digest)
    except Exception:
        # This observer must not change a READ result, guard or caller control.
        return


def booking_reference_sources(runtime, name, arguments):
    """Only finite membership categories leave this observer; no ref or digest."""
    fields = {role + '_ref_source': 'NOT_APPLICABLE' for role in ('offering', 'resource', 'customer')}
    try:
        booking = arguments if name == 'booking_create' else None
        if name == 'business_propose' and type(arguments) is dict:
            actions = arguments.get('actions')
            if type(actions) is list:
                matches = [a.get('arguments') for a in actions if type(a) is dict and a.get('capability') == 'booking.create']
                if len(matches) == 1:
                    booking = matches[0]
        if type(booking) is not dict:
            return fields
        state = getattr(runtime, '_reference_observations', {})
        for role in ('offering', 'resource', 'customer'):
            field = role + '_ref_source'
            value = booking.get(role + '_ref')
            if value is None:
                fields[field] = 'NOT_SUPPLIED'
                continue
            digest = _digest(value)
            bucket = state.get(role, {})
            if digest is not None and digest in bucket.get('digests', ()):
                fields[field] = 'READ_THIS_TURN'
            elif digest is not None and any(digest in other.get('digests', ())
                    for key, other in state.items() if key != role):
                fields[field] = 'WRONG_KIND_THIS_TURN'
            else:
                fields[field] = 'OBSERVATION_LIMIT' if bucket.get('limited') else 'NOT_OBSERVED_THIS_TURN'
        return fields
    except Exception:
        return {key: 'UNAVAILABLE' for key in fields}
