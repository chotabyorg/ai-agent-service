"""Local advertised-schema checks, never runtime admission or normalization."""
from jsonschema import Draft202012Validator, FormatChecker

from services.managed_tools import ManagedExecutionError

PREPARATION_TOOLS = frozenset({'business_propose', 'booking_create'})
SAFE_RULES = frozenset({'type', 'required', 'additionalProperties', 'oneOf', 'anyOf', 'allOf',
    'not', 'const', 'enum', 'minLength', 'maxLength', 'pattern', 'format', 'minItems', 'maxItems',
    'uniqueItems', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf'})


class PreparationSchema:
    def __init__(self, schema):
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
        return {'status': 'ERROR', 'error': {
            'code': 'TOOL_ARGUMENT_VALIDATION_FAILED', 'path': '/' + '/'.join(path),
            'rule': error.validator if error.validator in SAFE_RULES else 'schema'},
            'correction': 'No runtime action was executed. Return exactly one corrected call to the same tool, '
                          'using its advertised schema and a new tool-call ID.'}
