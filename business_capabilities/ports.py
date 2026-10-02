"""Provider boundary: only the enumerated, validated business operations."""
from typing import Literal, Protocol
from uuid import UUID
from business_capabilities.context import AgentExecutionContext

CapabilityId = Literal[
    'catalog.search','catalog.get','customer.find','customer.create','customer.get_context','customer.add_note',
    'inquiry.capture','resource.search','booking.check_availability','booking.search','booking.get','booking.create',
    'booking.reschedule','booking.cancel','deal.create','deal.get','deal.move','followup.create']


class BusinessAdapter(Protocol):
    supported_capabilities: frozenset[str]
    async def context(self, context: AgentExecutionContext): ...
    async def prepare(self, context: AgentExecutionContext, capability: CapabilityId, arguments: dict, turn_id: UUID) -> dict: ...
    async def execute(self, context: AgentExecutionContext, capability: CapabilityId, arguments: dict, operation: dict | None = None) -> dict: ...
    async def reconcile(self, context: AgentExecutionContext, capability: CapabilityId, operation_id: UUID) -> dict: ...
