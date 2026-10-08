"""Explicit wire-name bijection and product risk/default policy."""
from dataclasses import dataclass
from enum import StrEnum
import re
from pydantic import BaseModel
from business_capabilities import schemas as s


class Risk(StrEnum):
    READ = 'READ'
    WRITE_SAFE = 'WRITE_SAFE'
    WRITE_CONFIRM = 'WRITE_CONFIRM'


@dataclass(frozen=True)
class Capability:
    id: str
    tool_name: str
    family: str
    risk: Risk
    input: type[BaseModel]
    description: str
    default_enabled: bool = True

    def tool(self):
        return {'type': 'function', 'function': {'name': self.tool_name,
                'description': self.description, 'parameters': tool_schema(self.input)}}


CAPABILITIES = (
    Capability('catalog.search', 'catalog_search', 'CATALOG', Risk.READ, s.CatalogSearch, 'Find active services and products with factual prices; empty query browses.'),
    Capability('catalog.get', 'catalog_get', 'CATALOG', Risk.READ, s.CatalogGet, 'Get the selected service or product.'),
    Capability('customer.find', 'customer_find', 'CUSTOMERS', Risk.READ, s.CustomerFind, 'Find a returning customer by exact phone or email; ask clarification on ambiguity.'),
    Capability('customer.create', 'customer_create', 'CUSTOMERS', Risk.WRITE_SAFE, s.CustomerCreate, 'Save a new customer and their supplied contact details atomically.'),
    Capability('customer.get_context', 'customer_get_context', 'CUSTOMERS', Risk.READ, s.CustomerContext, 'Get permitted customer context; customer-facing conversations can read safe bookings only.'),
    Capability('customer.add_note', 'customer_add_note', 'CUSTOMERS', Risk.WRITE_SAFE, s.CustomerNote, 'Record a relevant customer note.'),
    Capability('inquiry.capture', 'inquiry_capture', 'INQUIRIES', Risk.WRITE_SAFE, s.InquiryCapture, 'Record the customer interest or request.'),
    Capability('resource.search', 'resource_search', 'BOOKINGS', Risk.READ, s.ResourceSearch, 'Find active specialists or resources.'),
    Capability('booking.check_availability', 'booking_check_availability', 'BOOKINGS', Risk.READ, s.Availability, 'Check an appointment interval. Advisory only; creates no reservation.'),
    Capability('booking.search', 'booking_search', 'BOOKINGS', Risk.READ, s.BookingSearch, 'Find this customer appointments within a bounded time period.'),
    Capability('booking.get', 'booking_get', 'BOOKINGS', Risk.READ, s.BookingGet, 'Get current appointment details and revision.'),
    Capability('booking.create', 'booking_create', 'BOOKINGS', Risk.WRITE_CONFIRM, s.BookingCreate, 'Propose an appointment; present the returned business summary and await natural confirmation.'),
    Capability('booking.reschedule', 'booking_reschedule', 'BOOKINGS', Risk.WRITE_CONFIRM, s.BookingReschedule, 'Propose moving an appointment using its current revision.'),
    Capability('booking.cancel', 'booking_cancel', 'BOOKINGS', Risk.WRITE_CONFIRM, s.BookingCancel, 'Propose cancelling an appointment using its current revision.'),
    Capability('deal.create', 'deal_create', 'DEALS', Risk.WRITE_CONFIRM, s.DealCreate, 'Propose recording a customer order with priced items. Confirm business facts, never internal system operations.', False),
    Capability('deal.get', 'deal_get', 'DEALS', Risk.READ, s.DealGet, 'Get the safe summary of an order created in this conversation.'),
    Capability('deal.move', 'deal_move', 'DEALS', Risk.WRITE_CONFIRM, s.DealMove, 'Propose an allowed customer order state change; confirm the business outcome.', False),
    Capability('followup.create', 'followup_create', 'FOLLOWUPS', Risk.WRITE_SAFE, s.FollowupCreate, 'Record an internal follow-up task; does not send a message or promise a notification.'),
)
REGISTRY = {c.id: c for c in CAPABILITIES}
WIRE_REGISTRY = {c.tool_name: c for c in CAPABILITIES}
DEFAULTS = tuple(c.id for c in CAPABILITIES if c.default_enabled)
if len(REGISTRY) != 18 or len(WIRE_REGISTRY) != 18 or any(not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', c.tool_name) for c in CAPABILITIES):
    raise RuntimeError('Invalid capability registry')


def normalize_arguments(capability: str, arguments: object) -> dict:
    return REGISTRY[capability].input.model_validate(arguments).model_dump(mode='json', by_alias=True, exclude_none=True)


def tool_schema(model):
    """Inline local definitions and omit nullable alternatives we do not accept."""
    source = model.model_json_schema()
    definitions = source.get('$defs', {})
    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        if '$ref' in value:
            return visit(definitions[value['$ref'].removeprefix('#/$defs/')])
        result = {key: visit(item) for key, item in value.items() if key != '$defs' and not (key == 'default' and item is None)}
        if 'anyOf' in result:
            branches = [branch for branch in result['anyOf'] if branch.get('type') != 'null']
            if len(branches) == 1:
                del result['anyOf']
                result.update(branches[0])
            else:
                result['anyOf'] = branches
        return result
    return visit(source)
