"""Bounded canonical projections; raw provider JSON never enters model context."""
import json
from typing import Annotated, Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter
from business_capabilities.schemas import Ref, Name, Text, Currency, Identity, normalize
from business_capabilities.context import failure

Money = Annotated[str, Field(pattern=r'^[0-9]+(?:\.[0-9]{1,2})?$', max_length=15)]


class Output(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Workspace(Output):
    id: UUID
    name: Name
    timezone: Annotated[str, Field(min_length=1, max_length=100)]


class Workspaces(Output):
    items: list[Workspace] = Field(max_length=100)


class Grant(Output):
    grant_id: UUID
    revision: int = Field(ge=1)
    connection_id: UUID
    workspace_id: UUID
    agent_id: UUID
    capabilities: list[str] = Field(max_length=18)


class AuthorizedContext(Output):
    workspace_id: UUID
    grant_id: UUID
    revision: int = Field(ge=1)
    capabilities: list[str] = Field(max_length=18)
    timezone: str = Field(max_length=100)


class Offering(Output):
    ref: Ref
    name: Name
    type: Literal['PRODUCT', 'SERVICE']
    description: Text | None = None
    price: Money
    currency: Currency
    duration_minutes: int | None = Field(default=None, ge=1, le=10080)
    active: bool


class Customer(Output):
    ref: Ref
    display_name: Name
    archived: bool
    identities: list[Identity] = Field(max_length=5)


class Resource(Output):
    ref: Ref
    name: Name
    kind: Literal['PERSON', 'ROOM', 'EQUIPMENT', 'OTHER']
    active: bool


class Booking(Output):
    ref: Ref
    customer_ref: Ref
    offering_ref: Ref | None = None
    resource_ref: Ref | None = None
    offering_name: Name | None = None
    resource_name: Name | None = None
    timezone: Annotated[str, Field(max_length=100)]
    start_at: AwareDatetime
    end_at: AwareDatetime
    status: Literal['CONFIRMED', 'CANCELLED', 'COMPLETED']
    revision: Ref
    # Internal comments are deliberately absent in customer-facing v1.


class Availability(Output):
    available: bool
    start_at: AwareDatetime
    end_at: AwareDatetime
    checked_at: AwareDatetime
    reservation: Literal[False]


class DealItem(Output):
    offering_ref: Ref
    name: Name
    unit_price: Money
    currency: Currency
    quantity: Annotated[str, Field(max_length=20)]


class Deal(Output):
    ref: Ref
    customer_ref: Ref
    title: Name
    currency: Currency
    amount: Money | None = None
    stage: Literal['NEW', 'IN_PROGRESS', 'WON', 'LOST']
    items: list[DealItem] = Field(max_length=20)
    revision: Ref


class Inquiry(Output):
    ref: Ref
    customer_ref: Ref
    summary: Text
    status: Literal['NEW']
    created_at: AwareDatetime


class Followup(Output):
    ref: Ref
    customer_ref: Ref
    title: Name
    due_at: AwareDatetime | None = None
    status: Literal['OPEN']


class Note(Output):
    note_ref: Ref
    customer_ref: Ref
    body: Text
    created_at: AwareDatetime


class Find(Output):
    match: Literal['NONE', 'SINGLE', 'AMBIGUOUS']
    candidates: list[Customer] = Field(max_length=1)


class OfferingPage(Output):
    items: list[Offering] = Field(max_length=20)


class ResourcePage(Output):
    items: list[Resource] = Field(max_length=20)


class BookingPage(Output):
    items: list[Booking] = Field(max_length=20)




class DeniedSection(Output):
    status: Literal['NOT_AUTHORIZED']


class BookingSection(Output):
    status: Literal['OK']
    items: list[Booking] = Field(max_length=5)
    truncated: bool
    next_cursor: Ref | None = None


class Sections(Output):
    NOTES: DeniedSection | None = None
    INQUIRIES: DeniedSection | None = None
    BOOKINGS: DeniedSection | BookingSection | None = None
    DEALS: DeniedSection | None = None
    FOLLOWUPS: DeniedSection | None = None


class CustomerContext(Output):
    customer: Customer
    sections: Sections
    truncated: bool


class BookingProposalSummary(Output):
    customer: Name
    service: Name
    resource: Name
    start_at: AwareDatetime
    end_at: AwareDatetime
    timezone: Annotated[str, Field(max_length=100)]
    price: Money | None = None
    currency: Currency | None = None
    action: Literal['reschedule'] | None = None


class CancelProposalSummary(Output):
    start_at: AwareDatetime
    end_at: AwareDatetime
    status: Literal['CONFIRMED', 'CANCELLED', 'COMPLETED']
    timezone: Annotated[str, Field(max_length=100)]
    offering_name: Name | None = None
    resource_name: Name | None = None
    action: Literal['cancel']


class ProposalItem(Output):
    name: Name
    unit_price: Money
    quantity: Annotated[str, Field(max_length=20)]


class DealProposalSummary(Output):
    customer: Name
    title: Name
    currency: Currency
    items: list[ProposalItem] = Field(min_length=1, max_length=20)


class MoveProposalSummary(Output):
    title: Name
    from_stage: Literal['NEW', 'IN_PROGRESS', 'WON', 'LOST']
    stage: Literal['NEW', 'IN_PROGRESS', 'WON', 'LOST']


class PreparedProposal(Output):
    proposal_ref: Ref
    arguments: dict
    payload_hash: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    summary: BookingProposalSummary | CancelProposalSummary | DealProposalSummary | MoveProposalSummary
    expires_at: AwareDatetime


RESULT_TYPES = {
    'catalog.search': OfferingPage, 'catalog.get': Offering,
    'customer.find': Find, 'customer.create': Customer,
    'customer.get_context': CustomerContext, 'customer.add_note': Note,
    'inquiry.capture': Inquiry, 'resource.search': ResourcePage,
    'booking.check_availability': Availability, 'booking.search': BookingPage,
    'booking.get': Booking, 'booking.create': Booking,
    'booking.reschedule': Booking, 'booking.cancel': Booking,
    'deal.create': Deal, 'deal.get': Deal, 'deal.move': Deal,
    'followup.create': Followup,
}
ERRORS = frozenset('UNAUTHENTICATED PERMISSION_DENIED VALIDATION_ERROR UNSUPPORTED UNSUPPORTED_CAPABILITY STALE_BINDING CONNECTION_REVOKED PROVIDER_UNAVAILABLE RATE_LIMITED INTERNAL_ERROR NOT_FOUND AMBIGUOUS IDENTITY_CONFLICT INACTIVE_CUSTOMER INACTIVE_OFFERING INACTIVE_RESOURCE BOOKING_CONFLICT CONFLICT STATE_CONFLICT STALE_STATE INVALID_TRANSITION CURRENCY_MISMATCH INVALID_ASSIGNEE CONFIRMATION_REQUIRED IDEMPOTENCY_CONFLICT OPERATION_IN_PROGRESS UNKNOWN_WRITE_RESULT TIMEOUT_UNKNOWN_RESULT STALE_PROPOSAL STALE_CUSTOMER_CONTEXT OPERATION_EXPIRED STALE_CURSOR RESULT_TOO_LARGE'.split())


def validate_result(capability, value, *, write=False):
    invalid = failure('UNKNOWN_WRITE_RESULT', 'RECONCILE') if write else failure('PROVIDER_UNAVAILABLE')
    try:
        normalize(value)
        if len(json.dumps(value, ensure_ascii=False).encode()) > 16384 or not isinstance(value, dict):
            return invalid
        if set(value) - {'status', 'data', 'error', 'truncated', 'next_cursor', 'operation_ref', 'effect'}:
            return invalid
        if value.get('status') == 'ERROR':
            err = value.get('error', {})
            if not isinstance(err, dict):
                return invalid
            code = err.get('code')
            if code not in ERRORS:
                return invalid
            retry = 'RECONCILE' if code in {'UNKNOWN_WRITE_RESULT', 'TIMEOUT_UNKNOWN_RESULT', 'OPERATION_IN_PROGRESS'} else 'NEVER'
            return failure(code, retry)
        if value.get('status') not in {'OK', 'AMBIGUOUS'}:
            return invalid
        data = RESULT_TYPES[capability].model_validate(value.get('data')).model_dump(mode='json', exclude_none=True)
        if capability == 'customer.find' and (data['match'] == 'AMBIGUOUS' or value['status'] == 'AMBIGUOUS'):
            data = {'match': 'AMBIGUOUS', 'candidates': []}
        result = {'status': value['status'], 'data': data}
        if 'truncated' in value:
            if not isinstance(value['truncated'], bool):
                return invalid
            result['truncated'] = value['truncated']
        if value.get('next_cursor'):
            result['next_cursor'] = TypeAdapter(Ref).validate_python(value['next_cursor'])
        if write:
            if value.get('effect') not in {'APPLIED', 'REPLAYED'}:
                return invalid
            result['effect'] = value['effect']
            result['operation_ref'] = TypeAdapter(Ref).validate_python(value['operation_ref'])
        return result
    except (ValueError, TypeError, KeyError):
        return invalid
