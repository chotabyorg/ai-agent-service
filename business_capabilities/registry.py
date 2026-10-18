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
    Capability('customer.find', 'customer_find', 'CUSTOMERS', Risk.READ, s.CustomerFind, 'Find a returning customer by exact phone or email. For booking identity checks, omit the name filter so existing contacts with a different supplied name are detected; ask for clarification on ambiguity.'),
    Capability('customer.create', 'customer_create', 'CUSTOMERS', Risk.WRITE_SAFE, s.CustomerCreate, 'Save contact details only when that is the standalone customer request. Never use this as a prerequisite for booking: supply customer details directly to booking_create or business_propose instead. A failed customer lookup does not mean the customer is new.'),
    Capability('customer.get_context', 'customer_get_context', 'CUSTOMERS', Risk.READ, s.CustomerContext, 'Get permitted customer context; customer-facing conversations can read safe bookings only.'),
    Capability('customer.add_note', 'customer_add_note', 'CUSTOMERS', Risk.WRITE_SAFE, s.CustomerNote, 'Record a relevant customer note.'),
    Capability('inquiry.capture', 'inquiry_capture', 'INQUIRIES', Risk.WRITE_SAFE, s.InquiryCapture, 'Record the customer interest or request.'),
    Capability('resource.search', 'resource_search', 'BOOKINGS', Risk.READ, s.ResourceSearch, 'Find active specialists or resources by their name, not the service name. Parameters are only query, limit and cursor. With no specialist preference call resource_search({}); for a named specialist use {"query":"Анна"}. Do not pass service/offering refs, filters or an arguments wrapper. Copy returned refs into availability or booking preparation; do not repeat an unchanged search.'),
    Capability('booking.check_availability', 'booking_check_availability', 'BOOKINGS', Risk.READ, s.Availability, 'Check an appointment interval. Advisory only; creates no reservation.'),
    Capability('booking.search', 'booking_search', 'BOOKINGS', Risk.READ, s.BookingSearch, 'Find this customer appointments within a bounded time period.'),
    Capability('booking.get', 'booking_get', 'BOOKINGS', Risk.READ, s.BookingGet, 'Get current appointment details and revision.'),
    Capability('booking.create', 'booking_create', 'BOOKINGS', Risk.WRITE_CONFIRM, s.BookingCreate, 'Prepare a booking directly with supplied customer name and exact phone/email, or an already verified customer_ref. Runtime checks supplied identity automatically; do not add a separate customer_find just to prepare this booking. Follow CUSTOMER_REUSE_REQUIRED with a separate call using its customer_ref; clarify conflicts or ambiguity. CRM records a new customer and booking together only after confirmation. Never call customer_create first or rename or duplicate a customer. Present the proposal and await confirmation.'),
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
    result = visit(source)
    # Explain existing read semantics only on the model-facing surface. Keep
    # canonical schemas/fingerprints unchanged for retained Business evidence.
    if issubclass(model, s.Page) or model is s.CustomerContext:
        result['properties']['cursor']['description'] = (
            'Omit on the first request. For a later page, copy an exact next_cursor '
            'returned by this tool for the same search. Never invent a cursor, '
            'page number, or "0". If no next_cursor was returned, omit cursor.'
        )
    if model in (s.CatalogSearch, s.ResourceSearch):
        result['properties']['query']['description'] = (
            'Case-insensitive literal substring of the name, not semantic search. '
            'Use a short distinctive word, not the full customer sentence. '
            'Omit or use an empty string to browse; if no matches, broaden the query.'
        )
        if model is s.ResourceSearch:
            result['properties']['query']['description'] += (
                ' This is the specialist/resource name, not a service or profession. '
                'When no specialist is requested, omit query to browse.'
            )
    if issubclass(model, s.Availability) or model is s.BookingReschedule:
        result['properties']['start_at']['description'] = (
            'The customer-requested appointment start as an ISO 8601 instant. '
            'Keep the requested local clock time with its correct timezone offset, '
            'or convert the clock time to UTC and use Z; do not apply both conversions. '
            'For example, 14:00 in UTC+03:00 is 14:00+03:00 or 11:00Z, '
            'never 11:00+03:00. For a move, use the newly requested time, '
            'not the existing appointment start returned by booking lookup.'
        )
    if model is s.BookingCreate:
        # Expose the existing runtime validator to the model without changing
        # the durable catalog schema fingerprint or its accepted input set.
        result['oneOf'] = [{'required': ['customer_ref']}, {'required': ['customer']}]
    return result
