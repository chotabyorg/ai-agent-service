"""Trusted execution authority: assembled from persisted state, never model arguments."""
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class AgentExecutionContext:
    owner_id: UUID
    agent_id: UUID
    connection_id: UUID
    workspace_id: UUID
    grant_id: UUID
    grant_revision: int
    session_id: UUID
    audience: str = 'SELF_ASSERTED_CUSTOMER'

    def claims(self) -> dict:
        return {name: str(getattr(self, name)) for name in
                ('owner_id', 'agent_id', 'connection_id', 'grant_id', 'session_id')} | {
                    'grant_revision': self.grant_revision, 'audience': self.audience}


class BusinessRuntimeError(Exception):
    """Safe domain failure suitable for translation by the HTTP boundary."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def failure(code: str, retry: str = 'NEVER') -> dict:
    messages = {
        'STALE_CUSTOMER_CONTEXT': 'Customer details changed. Ask for the current contact details before continuing.',
        'IDENTITY_CONFLICT': 'This contact is already linked to a different customer name. Ask which name to use before continuing.',
        'AMBIGUOUS_IDENTITY': 'This contact matches more than one customer. Ask for another identifying detail before continuing.',
        'PERMISSION_DENIED': 'This action is currently unavailable.',
        'VALIDATION_ERROR': 'Check the required business details.',
        'CONFIRMATION_REQUIRED': 'Summarize the proposal and ask the customer to confirm.',
        'STALE_PROPOSAL': 'The proposal is no longer current. Prepare a new proposal.',
        'UNKNOWN_WRITE_RESULT': 'The result is being checked. Do not repeat the action or claim success.',
        'PROVIDER_UNAVAILABLE': 'The business service is temporarily unavailable.',
        'NOT_FOUND': 'The requested record was not found.',
        'BOOKING_CONFLICT': 'That appointment time is no longer available.',
        'AMBIGUOUS': 'Ask for one more identifying detail.',
    }
    return {'status': 'ERROR', 'error': {'code': code, 'message': messages.get(code, 'The action could not be completed. Check the business details.'), 'retry': retry}}
