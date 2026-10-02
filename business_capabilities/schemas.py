"""Version-one closed model inputs. Authority is deliberately absent."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal
import unicodedata

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Ref = Annotated[str, Field(min_length=1, max_length=256)]
Name = Annotated[str, Field(min_length=1, max_length=200)]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Quantity = Annotated[str, Field(pattern=r"^(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,3})?$", max_length=14)]


def normalize(value):
    if isinstance(value, str):
        if any(c == '\x00' or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError('Invalid text')
        return unicodedata.normalize('NFC', value)
    if isinstance(value, dict):
        return {normalize(k): normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    @model_validator(mode='before')
    @classmethod
    def normalize_text(cls, value):
        return normalize(value)

    @model_validator(mode='after')
    def no_explicit_nulls(self):
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError('Omit optional fields instead of null')
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime):
                setattr(self, name, value.astimezone(timezone.utc))
        return self


class Identity(Closed):
    kind: Literal['PHONE', 'EMAIL', 'TELEGRAM', 'WHATSAPP', 'INSTAGRAM', 'OTHER']
    value: Annotated[str, Field(min_length=1, max_length=320)]


class Page(Closed):
    limit: Annotated[int, Field(ge=1, le=20, strict=True)] = 10
    cursor: Annotated[str, Field(min_length=1, max_length=1024)] | None = None


class CatalogSearch(Page):
    query: Annotated[str, Field(max_length=200)] = ''
    type: Literal['PRODUCT', 'SERVICE'] | None = None


class CatalogGet(Closed):
    offering_ref: Ref


class CustomerFind(Page):
    identity: Identity
    name: Name | None = None


class CustomerCreate(Closed):
    display_name: Name
    identities: Annotated[list[Identity], Field(min_length=1, max_length=5)]
    description: Text | None = None


class CustomerContext(Closed):
    customer_ref: Ref
    sections: list[Literal['NOTES', 'INQUIRIES', 'BOOKINGS', 'DEALS', 'FOLLOWUPS']] = Field(default_factory=list, max_length=5)
    cursor: Ref | None = None


class CustomerNote(Closed):
    customer_ref: Ref
    body: Text


class InquiryCapture(Closed):
    customer_ref: Ref
    summary: Text


class ResourceSearch(Page):
    query: Annotated[str, Field(max_length=200)] = ''


class Availability(Closed):
    resource_ref: Ref
    offering_ref: Ref
    start_at: AwareDatetime


class BookingSearch(Page):
    customer_ref: Ref
    from_: AwareDatetime = Field(alias='from')
    to: AwareDatetime

    @model_validator(mode='after')
    def interval(self):
        if not timedelta(0) < self.to - self.from_ <= timedelta(days=31):
            raise ValueError('Expected interval up to 31 days')
        return self


class BookingGet(Closed):
    booking_ref: Ref


class BookingCreate(Availability):
    customer_ref: Ref
    comment: Text | None = None


class BookingReschedule(BookingGet):
    expected_revision: Ref
    resource_ref: Ref
    start_at: AwareDatetime


class BookingCancel(BookingGet):
    expected_revision: Ref


class DealItem(Closed):
    offering_ref: Ref
    quantity: Quantity

    @model_validator(mode='after')
    def positive(self):
        if Decimal(self.quantity) <= 0 or len(self.quantity.replace('.', '').lstrip('0')) > 10:
            raise ValueError('Quantity must be positive')
        return self


class DealCreate(Closed):
    customer_ref: Ref
    title: Name
    currency: Currency
    items: Annotated[list[DealItem], Field(min_length=1, max_length=20)]


class DealGet(Closed):
    deal_ref: Ref


class DealMove(DealGet):
    expected_revision: Ref
    stage: Literal['NEW', 'IN_PROGRESS', 'WON', 'LOST']


class FollowupCreate(Closed):
    customer_ref: Ref
    title: Name
    description: Text | None = None
    due_at: AwareDatetime | None = None


class PendingReference(Closed):
    pending_action_ref: Ref
