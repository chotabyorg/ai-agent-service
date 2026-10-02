"""Redact expired sensitive payloads, retaining replay-blocking tombstones."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import update
from models.business import BusinessOperation, BusinessTurn, PendingBusinessAction


async def prune_business_data(db, *, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    # Unknown effects retain evidence for reconciliation; they never become fresh intents.
    operations = await db.execute(update(BusinessOperation).where(BusinessOperation.issued_at < cutoff,
        BusinessOperation.state.in_(['COMPLETED', 'FAILED'])).values(arguments={},confirmation=None,result=None,state='EXPIRED'))
    turns = await db.execute(update(BusinessTurn).where(BusinessTurn.created_at < cutoff).values(answer=None))
    proposals = await db.execute(update(PendingBusinessAction).where(PendingBusinessAction.created_at < cutoff,
        PendingBusinessAction.state.not_in(['UNKNOWN', 'DISPATCHING'])).values(actions=[],summary='Expired proposal',state='EXPIRED'))
    await db.commit()
    return {'operations': operations.rowcount, 'turns': turns.rowcount, 'proposals': proposals.rowcount}
