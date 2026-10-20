"""Validate preparation input and perform guarded customer lookup without dispatch."""
import hashlib
import json
from collections import OrderedDict
import unicodedata

from jsonschema import Draft202012Validator, FormatChecker

from business_capabilities.registry import CAPABILITIES, Risk
from schemas.business import BusinessAction, BusinessSafetyError, BusinessToolResult
from services.managed_tools import ManagedExecutionError

PREPARATION_TOOLS = frozenset({'business_propose', 'booking_create'})
PROPOSAL_TOOLS = PREPARATION_TOOLS | frozenset({'booking_cancel', 'booking_reschedule'})
# Read arguments use the same closed, advertised schema as proposal input.
# This permits one correction before dispatch, never a retry of an effect.
SCHEMA_VALIDATED_TOOLS = PROPOSAL_TOOLS | frozenset(
    capability.tool_name for capability in CAPABILITIES if capability.risk == Risk.READ) | {'booking_search_options'}
SAFE_RULES = frozenset({'type', 'required', 'additionalProperties', 'oneOf', 'anyOf', 'allOf',
    'not', 'const', 'enum', 'minLength', 'maxLength', 'pattern', 'format', 'minItems', 'maxItems',
    'uniqueItems', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf'})
_PREPARATION_SCHEMA_CACHE: OrderedDict[str, "PreparationSchema"] = OrderedDict()
_PREPARATION_SCHEMA_CACHE_LIMIT = 64


def _customer_name_key(value):
    # Russian ё/е spellings compare equally without rewriting stored names,
    # customer identity arguments, references or confirmation material.
    return unicodedata.normalize('NFC', value).casefold().replace('ё', 'е')


class PreparationSchema:
    def __new__(cls, schema):
        # Advertised tool parameters are rebuilt every Voice turn; reuse the
        # compiled validator when the canonical schema bytes match.
        key = hashlib.sha256(json.dumps(schema, ensure_ascii=False, allow_nan=False,
                                        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        cached = _PREPARATION_SCHEMA_CACHE.get(key)
        if cached is not None:
            _PREPARATION_SCHEMA_CACHE.move_to_end(key)
            return cached
        self = super().__new__(cls)
        self._cache_key = key
        return self

    def __init__(self, schema):
        if getattr(self, 'validator', None) is not None:
            return
        self.properties = set()
        pending = [schema]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                # Canonical advertised schemas are inlined. Never resolve a
                # reference supplied by a schema over a network or filesystem.
                if {'$ref', '$dynamicRef', '$recursiveRef'} & value.keys():
                    raise ManagedExecutionError('TOOL_SETUP_FAILED')
                self.properties.update(value.get('properties', {}))
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        Draft202012Validator.check_schema(schema)
        self.validator = Draft202012Validator(schema, format_checker=FormatChecker())
        key = getattr(self, '_cache_key', None)
        if key is not None:
            _PREPARATION_SCHEMA_CACHE[key] = self
            _PREPARATION_SCHEMA_CACHE.move_to_end(key)
            while len(_PREPARATION_SCHEMA_CACHE) > _PREPARATION_SCHEMA_CACHE_LIMIT:
                _PREPARATION_SCHEMA_CACHE.popitem(last=False)

    def feedback(self, arguments):
        error = next(self.validator.iter_errors(arguments), None)
        if error is None:
            return None
        # Instance keys may contain private/untrusted text. Only schema-owned
        # property names and bounded array ordinals enter the correction path.
        path = []
        for part in list(error.absolute_path)[:8]:
            if isinstance(part, str) and part in self.properties and len(part) <= 64:
                path.append(part)
            elif type(part) is int and 0 <= part < 32:
                path.append(str(part))
            else:
                path.append('?')
        return self._feedback('/' + '/'.join(path),
            error.validator if error.validator in SAFE_RULES else 'schema')

    def invalid_json_feedback(self):
        return self._feedback('/', 'type')

    def _feedback(self, path, rule):
        return {'status': 'ERROR', 'error': {
            'code': 'TOOL_ARGUMENT_VALIDATION_FAILED', 'path': path, 'rule': rule},
            # Code-owned schema only: never echo the malformed argument keys,
            # values or jsonschema exception text into correction feedback.
            'expected_parameters': self.validator.schema,
            'correction': 'No runtime action was executed. Return exactly one corrected call to the same tool, '
                          'using expected_parameters and a new tool-call ID. Remove fields not declared '
                          'in properties; do not nest the parameters inside an arguments object. '
                          'Omit optional fields you do not need, including cursor on the first page.'}


def _validated_preparation_actions(runtime, name, arguments):
    """Validate the full advertised preparation before any CRM read or write."""
    try:
        if not isinstance(arguments, dict):
            raise BusinessSafetyError('ACTION_INVALID')
        if name == 'booking_create':
            definition = runtime.registration.capability('booking.create')
            if definition is None:
                raise BusinessSafetyError('ACTION_INVALID')
            actions = [BusinessAction(capability='booking.create', arguments=definition.normalize(arguments))]
        elif name == 'business_propose':
            if set(arguments) != {'actions'} or not isinstance(arguments['actions'], list):
                raise BusinessSafetyError('ACTION_INVALID')
            values = arguments['actions']
            if not 1 <= len(values) <= 2 or any(not isinstance(value, dict) for value in values):
                raise BusinessSafetyError('ACTION_INVALID')
            capabilities = [value.get('capability') for value in values]
            if (any(type(capability) is not str for capability in capabilities)
                    or len({capability.split('.')[0] for capability in capabilities}) != len(values)):
                raise BusinessSafetyError('ACTION_INVALID')
            if len(values) == 2:
                by_name = {value.get('capability'): value.get('arguments', {}) for value in values}
                booking, deal = by_name.get('booking.create'), by_name.get('deal.create')
                if (not isinstance(booking, dict) or not isinstance(deal, dict)
                        or not booking.get('customer_ref') or booking.get('customer_ref') != deal.get('customer_ref')
                        or 'customer' in booking):
                    raise BusinessSafetyError('ACTION_INVALID')
            actions = []
            for value in values:
                action = BusinessAction.model_validate(value)
                definition = runtime.registration.capability(action.capability)
                if definition is None:
                    raise BusinessSafetyError('ACTION_INVALID')
                actions.append(BusinessAction(capability=action.capability,
                    arguments=definition.normalize(action.arguments)))
        else:
            return []

        for action in actions:
            if action.capability not in runtime.binding.enabled_capabilities:
                raise BusinessSafetyError('CAPABILITY_DISABLED')
        return actions
    except BusinessSafetyError:
        raise
    except Exception:
        raise BusinessSafetyError('ACTION_INVALID') from None


def _safe_lookup_draft(category):
    if category == 'VALIDATION_ERROR':
        return 'Подскажите, пожалуйста, контактный номер с кодом страны.'
    if category == 'IDENTITY_CONFLICT':
        return 'Этот контакт уже связан с другим именем. Уточните, пожалуйста, какое имя указать для записи.'
    if category == 'AMBIGUOUS_IDENTITY':
        return 'По этому контакту найдено несколько клиентов. Уточните, пожалуйста, другой контакт.'
    if category == 'CAPABILITY_DISABLED':
        return 'Не удалось проверить контакт для записи. Уточните, пожалуйста, контакт или попробуйте позже.'
    return 'Не удалось проверить контакт. Попробуйте, пожалуйста, чуть позже.'


def _observe_preparation(runtime, *, name, lookup_result='NOT_RUN', refusal='NONE',
                         lookup_mode='NOT_APPLICABLE'):
    try:
        runtime.observe_business_diagnostic(
            stage='PREPARATION', capability=name, refusal_category=refusal,
            lookup_source='BOOKING_PREFLIGHT', lookup_mode=lookup_mode,
            lookup_result=lookup_result,
        )
    except Exception:
        pass


def _observe_lookup_failure(runtime, error):
    """Keep the original finite failure category before safe caller wording."""
    if isinstance(error, BusinessSafetyError):
        refusal = 'AUTHORITY_BLOCKED' if error.processing_status == 'UNKNOWN' else error.code
    elif isinstance(error, (TypeError, ValueError)):
        refusal = 'LOOKUP_INVALID'
    elif isinstance(error, TimeoutError):
        refusal = 'PROVIDER_UNAVAILABLE'
    else:
        refusal = 'OTHER'
    try:
        runtime.observe_business_diagnostic(stage='LOOKUP', capability='customer.find',
            refusal_category=refusal, lookup_source='BOOKING_PREFLIGHT',
            lookup_mode='IDENTITY_ONLY', lookup_result='ERROR')
    except Exception:
        pass


async def _preflight_guard(runtime):
    try:
        return await runtime.execution_guard(allow_incomplete=True)
    except Exception:
        try:
            return runtime._halt(True)
        except Exception:
            return BusinessToolResult(halt_status='UNKNOWN', error_code='TOOL_EXECUTION_UNKNOWN')


async def _preflight_feedback_guard(runtime):
    """Do not emit ordinary preflight feedback over retained effect evidence."""
    gate = await _preflight_guard(runtime)
    if gate is not None:
        return gate
    try:
        if runtime.collector.snapshot():
            return runtime._halt(True)
    except Exception:
        return runtime._halt(True)
    return None


def _missing_spoken_booking_contact(runtime, name, arguments):
    """Recognize an incomplete inline Voice contact without repairing arguments."""
    if not isinstance(arguments, dict):
        return False
    booking = arguments if name == 'booking_create' else None
    if (name == 'business_propose' and set(arguments) == {'actions'}
            and isinstance(arguments['actions'], list) and len(arguments['actions']) == 1):
        action = arguments['actions'][0]
        if isinstance(action, dict) and set(action) == {'capability', 'arguments'} and action['capability'] == 'booking.create':
            booking = action['arguments']
    if not isinstance(booking, dict) or booking.get('customer_ref') is not None:
        return False
    customer = booking.get('customer')
    identity = customer.get('identity') if isinstance(customer, dict) else None
    # Email and verified-reference flows retain their ordinary validation. A
    # missing phone never authorizes a lookup using an invented model contact.
    if not isinstance(identity, dict) or identity.get('kind') == 'EMAIL':
        return False
    if (runtime.binding is None or 'booking.create' not in runtime.binding.enabled_capabilities
            or runtime.registration.capability('booking.create') is None):
        return False
    from services.voice_contact import voice_phone_facts
    caller = getattr(getattr(runtime, 'execution', None), 'caller_phone', '')
    return voice_phone_facts(getattr(runtime, 'request', None), caller) == ()


async def prepare_booking_customer_reuse(runtime, name, arguments):
    """Resolve supplied booking identity through the guarded customer.find read.

    The helper only supplies a validated reference to the model. It never edits
    booking arguments or reserves an operation as part of the lookup.
    """
    if name not in PREPARATION_TOOLS:
        return None
    if _missing_spoken_booking_contact(runtime, name, arguments):
        # STT can deliver the appointment/name before the following phone
        # fragment. Ask for the absent fact before normalizing a model's
        # placeholder contact/name; no normalized action, lookup or plan exists.
        gate = await _preflight_feedback_guard(runtime)
        _observe_preparation(runtime, name=name, refusal='AUTHORITY_BLOCKED' if gate is not None
                             else 'VALIDATION_ERROR', lookup_mode='IDENTITY_ONLY')
        if gate is not None:
            return gate
        runtime._booking_workflow_started = True
        return BusinessToolResult(safe_draft='Подскажите, пожалуйста, контактный номер для записи.')
    try:
        actions = _validated_preparation_actions(runtime, name, arguments)
    except BusinessSafetyError as error:
        refusal = 'CAPABILITY_DISABLED' if error.code == 'CAPABILITY_DISABLED' else 'OTHER'
        _observe_preparation(runtime, name=name, refusal=refusal)
        raise
    except Exception as error:
        refusal = 'VALIDATION_ERROR' if getattr(error, 'code', None) == 'ACTION_INVALID' else 'OTHER'
        _observe_preparation(runtime, name=name, refusal=refusal)
        raise

    booking = next((action for action in actions if action.capability == 'booking.create'), None)
    if booking is not None:
        runtime._booking_workflow_started = True
    customer = booking.arguments.get('customer') if booking is not None else None
    if customer is None:
        _observe_preparation(runtime, name=name)
        return None
    return await lookup_booking_customer(runtime, customer, name=name)


async def preflight_booking_contact(runtime, customer, *, name):
    """Reject an ungrounded Voice phone before unrelated booking reads.

    Valid contacts do no I/O here. The later identity lookup keeps its own
    fresh authority checks and never reuses this preflight as a witness.
    """
    identity = customer['identity']
    if identity['kind'] != 'PHONE':
        return None
    from services.voice_contact import voice_phone_facts
    request = getattr(runtime, 'request', None)
    caller = getattr(getattr(runtime, 'execution', None), 'caller_phone', '')
    phones = voice_phone_facts(request, caller)
    if phones is None or (len(phones) == 1 and identity['value'] == phones[0]):
        return None
    gate = await _preflight_feedback_guard(runtime)
    _observe_preparation(runtime, name=name, refusal='AUTHORITY_BLOCKED' if gate is not None
                         else 'VALIDATION_ERROR', lookup_mode='IDENTITY_ONLY')
    if gate is not None:
        return gate
    return BusinessToolResult(safe_draft=(
        'Продиктуйте, пожалуйста, контактный номер по цифрам с кодом страны.'
        if phones else 'Подскажите, пожалуйста, контактный номер для записи.'))


async def lookup_booking_customer(runtime, customer, *, name, return_lookup=False):
    """Read an explicit booking contact through the existing identity gates.

    The optional early READ returns the ordinary validated lookup to the model.
    It never edits a booking call, caches a witness or creates a proposal.
    """

    # This fresh gate preserves retained UNKNOWN/proposal evidence before any
    # lookup feedback can be returned to the model or customer.
    gate = await _preflight_feedback_guard(runtime)
    if gate is not None:
        _observe_preparation(runtime, name=name, refusal='AUTHORITY_BLOCKED')
        return gate

    contact = await preflight_booking_contact(runtime, customer, name=name)
    if contact is not None:
        return contact

    find_capability = runtime.registration.capability('customer.find')
    if (find_capability is None or runtime.binding is None
            or 'customer.find' not in runtime.binding.enabled_capabilities):
        _observe_preparation(runtime, name=name, refusal='CAPABILITY_DISABLED',
                             lookup_mode='IDENTITY_ONLY')
        return BusinessToolResult(safe_draft=_safe_lookup_draft('CAPABILITY_DISABLED'))

    lookup_mode = 'IDENTITY_ONLY'
    try:
        from business_capabilities.runtime_tools import call_capability
        lookup_arguments = find_capability.normalize({'identity': customer['identity']})
        result = await call_capability(runtime, 'customer_find', lookup_arguments,
                                       lookup_source='BOOKING_PREFLIGHT')
    except ManagedExecutionError:
        raise
    except BusinessSafetyError as error:
        _observe_lookup_failure(runtime, error)
        try:
            retained = runtime.collector.snapshot()
        except Exception:
            raise BusinessSafetyError('RESULT_GATE_UNAVAILABLE', processing_status='UNKNOWN') from None
        if error.processing_status == 'UNKNOWN' or retained:
            if error.processing_status != 'UNKNOWN':
                raise BusinessSafetyError('RESULT_GATE_UNAVAILABLE', processing_status='UNKNOWN') from None
            raise
        gate = await _preflight_feedback_guard(runtime)
        if gate is not None:
            return gate
        refusal = 'CAPABILITY_DISABLED' if error.code == 'CAPABILITY_DISABLED' else 'LOOKUP_UNAVAILABLE'
        _observe_preparation(runtime, name=name, lookup_result='ERROR', refusal=refusal,
                             lookup_mode=lookup_mode)
        return BusinessToolResult(safe_draft=_safe_lookup_draft(refusal))
    except Exception as error:
        _observe_lookup_failure(runtime, error)
        try:
            retained = runtime.collector.snapshot()
        except Exception:
            return runtime._halt(True)
        if retained:
            return runtime._halt(True)
        gate = await _preflight_feedback_guard(runtime)
        if gate is not None:
            return gate
        _observe_preparation(runtime, name=name, lookup_result='ERROR', refusal='LOOKUP_UNAVAILABLE',
                             lookup_mode=lookup_mode)
        return BusinessToolResult(safe_draft=_safe_lookup_draft('LOOKUP_UNAVAILABLE'))

    # The ordinary read path performs this guard itself. Check once more before
    # making a preflight result visible or continuing to booking facts.
    gate = await _preflight_feedback_guard(runtime)
    if gate is not None:
        return gate
    if result is None or result.halt_status is not None:
        if result is not None and result.halt_status == 'UNKNOWN':
            return result
        _observe_preparation(runtime, name=name, lookup_result='ERROR',
                             refusal='LOOKUP_UNAVAILABLE', lookup_mode=lookup_mode)
        return BusinessToolResult(safe_draft=_safe_lookup_draft('LOOKUP_UNAVAILABLE'))
    if result.safe_draft is not None:
        _observe_preparation(runtime, name=name, lookup_result='ERROR',
                             refusal='LOOKUP_UNAVAILABLE', lookup_mode=lookup_mode)
        return BusinessToolResult(safe_draft=_safe_lookup_draft('LOOKUP_UNAVAILABLE'))

    payload = result.tool_payload
    if not isinstance(payload, dict) or payload.get('status') not in {'OK', 'AMBIGUOUS'}:
        error_code = payload.get('error', {}).get('code') if isinstance(payload, dict) else None
        if error_code == 'AMBIGUOUS_IDENTITY':
            refusal, draft = 'AMBIGUOUS_IDENTITY', _safe_lookup_draft('AMBIGUOUS_IDENTITY')
        elif error_code == 'IDENTITY_CONFLICT':
            refusal, draft = 'IDENTITY_CONFLICT', _safe_lookup_draft('IDENTITY_CONFLICT')
        elif error_code == 'VALIDATION_ERROR':
            refusal, draft = 'VALIDATION_ERROR', _safe_lookup_draft('VALIDATION_ERROR')
        else:
            refusal, draft = 'LOOKUP_UNAVAILABLE', _safe_lookup_draft('LOOKUP_UNAVAILABLE')
        _observe_preparation(runtime, name=name, lookup_result='ERROR', refusal=refusal,
                             lookup_mode=lookup_mode)
        return BusinessToolResult(safe_draft=draft)

    try:
        from business_capabilities.results import Find
        data = Find.model_validate(payload.get('data'))
    except Exception:
        _observe_preparation(runtime, name=name, lookup_result='ERROR', refusal='LOOKUP_INVALID',
                             lookup_mode=lookup_mode)
        return BusinessToolResult(safe_draft=_safe_lookup_draft('LOOKUP_INVALID'))

    if payload.get('status') == 'AMBIGUOUS' or data.match == 'AMBIGUOUS':
        _observe_preparation(runtime, name=name, lookup_result='AMBIGUOUS',
                             refusal='AMBIGUOUS_IDENTITY', lookup_mode=lookup_mode)
        return BusinessToolResult(safe_draft=_safe_lookup_draft('AMBIGUOUS_IDENTITY'))
    if data.match == 'NONE':
        if data.candidates:
            _observe_preparation(runtime, name=name, lookup_result='ERROR', refusal='LOOKUP_INVALID',
                                 lookup_mode=lookup_mode)
            return BusinessToolResult(safe_draft=_safe_lookup_draft('LOOKUP_INVALID'))
        _observe_preparation(runtime, name=name, lookup_result='NONE', lookup_mode=lookup_mode)
        return result if return_lookup else None
    if data.match != 'SINGLE' or len(data.candidates) != 1:
        _observe_preparation(runtime, name=name, lookup_result='ERROR', refusal='LOOKUP_INVALID',
                             lookup_mode=lookup_mode)
        return BusinessToolResult(safe_draft=_safe_lookup_draft('LOOKUP_INVALID'))

    candidate = data.candidates[0]
    if candidate.archived:
        _observe_preparation(runtime, name=name, lookup_result='SINGLE',
                             refusal='IDENTITY_CONFLICT', lookup_mode=lookup_mode)
        return BusinessToolResult(safe_draft='Этот контакт связан с архивной записью. Уточните, пожалуйста, другой контакт.')
    if _customer_name_key(candidate.display_name) != _customer_name_key(customer['display_name']):
        _observe_preparation(runtime, name=name, lookup_result='SINGLE',
                             refusal='IDENTITY_CONFLICT', lookup_mode=lookup_mode)
        return BusinessToolResult(safe_draft=_safe_lookup_draft('IDENTITY_CONFLICT'))

    _observe_preparation(runtime, name=name, lookup_result='SINGLE', lookup_mode=lookup_mode)
    if return_lookup:
        return result
    return BusinessToolResult(tool_payload={
        'status': 'CUSTOMER_REUSE_REQUIRED',
        'customer_ref': candidate.ref,
        'instruction': 'Call the booking tool again with this customer_ref and omit customer.',
    })
