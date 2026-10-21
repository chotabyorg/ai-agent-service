"""Customer wording preserves local booking facts and canonical receipt checks."""
import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from business_capabilities.runtime_tools import completed_booking_reply
from schemas.business import BusinessSafetyError


def retained_receipt(capability, start, timezone, effect):
    operation=SimpleNamespace(id=uuid4(),command=SimpleNamespace(capability=capability))
    status='CANCELLED' if capability=='booking.cancel' else 'CONFIRMED'
    result=dict(status='OK',effect=effect,operation_id=str(operation.id),operation_ref='op_test',
        data=dict(ref='booking_test',customer_ref='customer_test',offering_name='Стрижка',
            resource_name='Анна',timezone=timezone,start_at=start,end_at=start,status=status,revision='1'))
    receipt=SimpleNamespace(receipt_id='op_test',object_id='booking_test',evidence={'result':result})
    return operation,receipt


@pytest.mark.parametrize('capability,prefix',[
    ('booking.create','Запись была подтверждена'),
    ('booking.reschedule','Запись была перенесена'),
    ('booking.cancel','Запись была отменена')])
@pytest.mark.parametrize('effect',['APPLIED','REPLAYED'])
@pytest.mark.parametrize('start,timezone,expected',[
    ('2026-10-20T21:30:00Z','Europe/Minsk','21.10.2026 в 00:30'),
    ('2026-07-20T08:30:00Z','Europe/Warsaw','20.07.2026 в 10:30'),
    ('2026-12-20T09:30:00Z','Europe/Warsaw','20.12.2026 в 10:30'),
])
def test_receipt_uses_business_local_clock_without_technical_timezone(capability,prefix,effect,start,timezone,expected):
    operation,receipt=retained_receipt(capability,start,timezone,effect)
    original=copy.deepcopy(receipt.evidence)
    reply=completed_booking_reply(operation,receipt)
    assert reply==f'{prefix}: Стрижка, специалист Анна, {expected} по местному времени.'
    assert timezone not in reply and 'booking_test' not in reply
    assert receipt.evidence==original


@pytest.mark.parametrize('field,value',[
    ('status','ERROR'),('effect','NONE'),('operation_id',str(uuid4())),
    ('operation_ref','op_other'),('ref','booking_other'),('booking_status','CANCELLED')])
def test_presentation_never_promotes_mismatched_receipt_to_success(field,value):
    operation,receipt=retained_receipt('booking.create','2026-10-21T07:30:00Z','Europe/Minsk','APPLIED')
    result=receipt.evidence['result']
    if field=='ref':
        result['data']['ref']=value
    elif field=='booking_status':
        result['data']['status']=value
    else:
        result[field]=value
    with pytest.raises(BusinessSafetyError):
        completed_booking_reply(operation,receipt)
