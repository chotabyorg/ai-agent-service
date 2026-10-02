# Business Capability Runtime v2: implementation boundary

This is an opt-in reference implementation, not production rollout evidence.
Read the root accepted design and current product overrides together. Provider
contract: the paired ChotaCRM repository `docs/agent-api-v1.md` and exported
`docs/agent-capabilities.json`. No Bitrix adapter or Google migration is included.

## Deliberate reconciliations

- `READ`, `WRITE_SAFE`, `WRITE_CONFIRM` replace historical C0/C1/C2. The model
  interprets conversational consent; a server-owned reference identifies the
  exact immutable proposal, with separate customer turns, expiration and current
  grant authorization. No keyword matcher or employee approval is used.
- Exact self-asserted PHONE/EMAIL lookup is permitted. Only a bounded self-service
  customer projection is returned, with no internal notes/audit/contact list.
- Customer-facing booking output includes business names and workspace timezone,
  and omits absent offering/resource fields for historical browser bookings.
  Context BOOKING sections use the same safe Booking projection. These additions
  preserve useful answers when catalog/resource tools are separately disabled.
- Provider prepare returns a typed business-facts summary object, serialized as
  data for the model's natural recap. It contains no provider/native IDs. Pending
  summaries are bounded to 8k characters and proposal data to 16k bytes. The
  model-facing proposal is not a provider-specific instruction.
- CapabilityBinding rows are per-capability enablement overrides referring to one
  authoritative active CRM connection. Family comes from the immutable registry;
  there is no per-capability/family route selection in the owner API. This compact
  representation avoids six duplicated family rows and an additional permission
  table while preserving a future family-routing seam. A future multi-provider
  phase must enforce family coherence explicitly; it is not implemented here.
- `BusinessConfiguration` survives disconnect. The owner sees CANONICAL versus
  LEGACY mode; dormant Bitrix credentials cannot become an execution bypass.
  Google tools retain their original separate schemas and execution path.
- Stable optional chat `event_id` gives request-delivery replay protection. The
  current messenger callers need not change for opt-in compatibility; callers
  without it have server-assigned processing turns. This limits transport event
  deduplication, while immutable pending IDs, logical write intents and provider
  receipts still prevent blind repeated effects.

## Authority and concurrency

Configuration checks agent ownership before upstream identity calls; both exact
Origin and access-token cookie are needed for writes. RS256 config assertions
carry only owner/agent/connection; the first-party provider verifies the cookie
against its identity service. Runtime uses only dedicated service assertions.
The model never supplies owner, workspace, connection, scope, actor or operation
keys. The current customer-facing audience is always SELF_ASSERTED_CUSTOMER.

Generation labels isolate history, references and pending actions after permission
changes/disconnect/reconnect. Before any history/model/cached answer, CRM context
revalidates current membership and grant. Each model request and final answer
rechecks authorization. Optional dedicated caller credentials isolate ingress
namespaces; a shared legacy credential has one explicitly shared namespace.

The database enforces one active CRM plus composite agent/owner/connection
provenance. Configuration and operation dispatch persistence share the agent row
lock: either configuration sees DISPATCHING/UNKNOWN and is blocked, or a waiting
writer sees the new generation and never sends the old grant. A dedicated
PostgreSQL session advisory lock serializes same-conversation requests across
workers and survives durable ledger commits. Locks release on worker failure.

## Effects and uncertainty

WRITE_SAFE identity is the persisted processing turn plus normalized arguments;
WRITE_CONFIRM identity is the provider-frozen proposal plus normalized arguments.
The fingerprint of arguments is a separate unresolved-write guard, not permanent
business deduplication. A later deliberate identical safe write is a new intent.
Operation IDs and immutable issuance are committed before network dispatch.
Writes have no automatic retry path. Unknown outcomes reconcile using the same
operation; receipt absence, permission loss or malformed output is not proof of
no effect. The next model step/turn automatically checks unsettled receipts and
gets a bounded authoritative recovery projection, avoiding duplicate actions or
unnecessary fresh consent when the previous action already succeeded.

A two-action booking+deal proposal for the same customer can be confirmed once.
Each provider command has its own atomic receipt. This is sequential composition,
not a distributed transaction: partial failure reports completed effects and
keeps the unresolved receipt. It does not promise all-or-nothing composition.

Settled sensitive bodies/results and saved turn answers are redacted after30days
by the explicit maintenance command. Event/intent/hash tombstones remain and
expired event reuse fails. UNKNOWN evidence is retained until reconciled. The
maintenance command is not scheduled or run on any existing service database.

## Verification limits

Unit tests use strict contract/transport fakes. Integration tests use real
PostgreSQL, real API/session/runtime/prompt code, fake LLM decisions and synthetic
provider responses. This verifies deterministic control of effects; it does not
prove real-model semantic understanding. Separate full-stack QA must use real
ChotaCRM/PostgreSQL and inspect all18 capability behavior. Real-model smoke needs
an explicitly safe company-controlled test configuration; no production keys or
customer data are used for this implementation.
