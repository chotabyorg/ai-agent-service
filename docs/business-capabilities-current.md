# Business capabilities on the managed foundation

Source-only addition above AI main `246101ad21ae15dcdcc12498603c073405d3876c`.
No production channels, deployment or shared managed v1 contracts are changed.

## Ownership and routing

Conversation → ManagedTurn → AI shared LLM core → typed BusinessRegistry →
ChotaCRMAdapter → private ChotaCRM application API → its own PostgreSQL.
Conversation remains the sole communication lifecycle owner and receives only
existing opaque operation references. The Goal3 AI ledger remains the only
BusinessOperation ledger. Provider receipts are transactional idempotency evidence,
not another platform operation coordinator.

The registry is empty unless `BUSINESS_CAPABILITIES_ENABLED=true`. With the flag,
code registers only `chotacrm`, never caller-selected adapters. Each owner configures
one CRM workspace per agent. Existing complete managed grants materialize scoped
business bindings from that configuration. Every mutation rechecks live agent,
connection state/generation, binding, scope, frozen plan, enabled capabilities,
fresh provider facts and unresolved-effect state. Short database locks never span
provider/model network calls. Configuration intents block business dispatch until
the remote grant change is acknowledged and the local state is settled.

## Capabilities and product policy

The eighteen canonical actions use provider-neutral underscore tool names (for
example `catalog_search` and `booking_create`); no workspace/provider/actor arguments.
READ: catalog.search/get, customer.find/get_context, resource.search,
booking.check_availability/search/get, deal.get. WRITE_SAFE: customer.create,
customer.add_note, inquiry.capture, followup.create. WRITE_CONFIRM:
booking.create/reschedule/cancel, deal.create/move. All supported capabilities are
ON at connect except deal.create and deal.move. Explicit owner enable is sufficient;
there is no employee approval workflow. Denials are enforced in both AI and CRM.

READ calls use a typed adapter port and don't reserve a mutation plan. The existing
one immutable plan per Turn remains. A booking can accept either an existing safe
customer reference or supplied name plus exact phone/email, with optional inquiry.
CRM resolves/creates the customer, records the inquiry and creates the booking
atomically after confirmation under one operation identity. Required prerequisite
capabilities are checked. Ambiguous identity never guesses or merges customers.
Standalone customer/inquiry effects remain WRITE_SAFE; the model should use the
booking aggregate when booking a new customer in the same Turn.

The advertised booking tools accept supplied customer details directly; their
guarded preflight performs the exact identity lookup without an extra model-driven
lookup step. An existing match still requires an explicit `customer_ref` proposal;
no frozen arguments are silently rewritten. A failed/invalid model-driven
customer lookup returns a safe clarification rather than allowing a guessed
`customer.create`. Once availability or booking preparation starts (including a
booking in the same model tool-call batch), a separate `customer.create` receives
`BOOKING_AGGREGATE_REQUIRED` before any plan is reserved. A standalone contact-save
request retains its existing WRITE_SAFE policy. This is request-local guidance,
not a second ledger, retry permission, or a change to confirmation authority.

One confirmation may cover a booking.create plus deal.create bundle for the same
existing customer reference. Other mixed proposals are rejected before reservation:
reschedule/cancel/move effects invalidate their own preconditions, and new-customer
dependent plans would require a workflow engine or mutable frozen arguments.

## Natural confirmation

Only the ChotaCRM code-owned registration enables SEMANTIC_V1. Existing reference
confirmation registrations retain their original behavior/catalog fingerprints.
The model receives a bounded pending projection and classifies the complete selected
customer reply as CONFIRM, CHANGE, REJECT or UNRELATED. No yes-word matcher is used.
The UUID is internal; deterministic customer prose repeats approved business facts.
The server accepts only the latest scoped frozen proposal, from a later selected
canonical inbound customer TEXT Turn, with current permission/generation/facts and
unexpired consent. The decision supplies no replacement action arguments.
CHANGE/REJECT cancel only reserved proposals; CHANGE may freeze a new proposal in
the current Turn, requiring another later confirmation. UNRELATED grants no effect.

If Conversation proves an `INTERVENING_SPLIT`, an unacquired old proposal remains
visible only as nonconfirmable context, even after its normal TTL. The model must
classify the complete current customer input as an independently specified CHANGE;
a bare agreement cannot inherit the old proposal's facts. Only after the current
Turn is independently eligible may the runtime commit an exact NO_DISPATCH
retirement marker and refresh the model's current-turn tools. The new intent then
uses ordinary policy: WRITE_SAFE may execute under its normal current-turn checks,
while WRITE_CONFIRM creates a new current-origin proposal. Any acquired request,
permit, dispatch or attempted operation remains attached to its original plan and
cannot use this replacement path. REJECT, UNRELATED, stale CONFIRM and unavailable
or incomplete authority observations do not unlock another action.
The exact NO_DISPATCH tombstone remains the latest nonconfirmable context across
TTL expiry and process restart until a newer proposal replaces it. A normal same-Turn
WRITE_SAFE action and ordinary confirmation of a current-origin proposal keep their
existing policy; the split barrier does not add a blanket confirmation step.

## Identity and projections

Runtime claims always use the server-selected SELF_ASSERTED_CUSTOMER audience and
complete AI BusinessScope UUID as provider session namespace. Exact supplied
identity may establish useful self-service customer/booking context. Responses
are validated closed DTOs; notes, audit, internal comments and arbitrary deal data
are not included in the customer-facing projection. Ambiguity returns clarification
without exposing a list of other customers. No cross-channel merge is performed.

## Provider trust, receipts and UNKNOWN

Dedicated RS256 assertions use the AI-only private key and CRM public-key registry,
not user JWT signing secrets. Configuration uses the owner's existing access cookie
for CRM's independent identity/membership check; the cookie is never stored or used
at runtime. S2S signed claims carry server-selected owner/agent/grant/connection and
scope. HTTP redirects and environment proxies are disabled and responses bounded.
The private signing key is excluded from legacy MCP subprocess environments.

The provider profile is LOOKUP, with no native retry guarantee and no automatic
resubmission. Before dispatch, Goal3 durably reserves the operation UUID and freezes
arguments plus deterministic `/facts` hashes. CRM compares expected facts under its
transaction lock and commits the domain aggregate with its receipt. Managed receipts
echo the original operation UUID in addition to their separate provider receipt ID.
Response loss remains UNKNOWN until scoped read-only lookup proves original success;
missing/denied/malformed evidence remains UNKNOWN. Recovering an effect grants no
permission for a new one. No exactly-once guarantee is claimed. Managed provider
receipt retention is documented in the CRM repository.

Known managed business rejections use a durable provider `NOT_APPLIED` receipt
bound to the same original operation UUID. Lookup can therefore prove FAILED even
after the negative response is lost; the original command remains fenced against
a later effect. An unreceipted error never establishes original no-effect evidence.

## Configuration and migrations

Owner routes: GET business-tools/workspaces; GET/PUT/PATCH/DELETE business-tools,
under `/v1/agents/{agent_id}`. Gateway exposes only corresponding protected owner
routes. Cookie + owner match and allowed Origin protect changes. Configuration is
one additive `business_connections` table; it contains no provider secret. A stable
connection/intent survives lost connect/update/revoke responses. Unresolved ledger
effects prevent reconfiguration/disconnect. Terminal evidence has no cascade from
mutable agents/connections.

Migration chain ends `d5e6f7g8h9i0` (current-main managed safety) →
`bv2_20261008_01` (connections). Historical AI #43's migration with the same old ID
and different semantics was not copied. None of its parallel ledger/session/plan
tables were introduced. Existing migrated data and the ledger remain authoritative.

`g5_20261015_01` adds one immutable Conversation effect-request identity and
optional permit UUID to each AI operation. The request tuple is all-null or
all-present with three non-null SHA-256 hashes; the database permits a single
assignment while the original operation is still RESERVED and rejects rebinding
or combining acquisition with a dispatch/state transition. A partial unique
scope index also treats a requested-but-not-yet-dispatched acquisition as an
unresolved blocker. The migration downgrade refuses once any acquisition is
retained.

Every new managed AI business effect checks the current Conversation authority
through `BUSINESS_CONVERSATION_URL` and its dedicated `BUSINESS_CONVERSATION_TOKEN`
using `X-Service-Token`. Configuration must be supplied as a pair and cannot
reuse the existing AI service tokens. With no pair configured, new writes fail
closed while reads remain available. Only an exact bounded JSON
`EXTERNAL_EFFECT_DENIED` response proves no dispatch; transport faults, conflicts,
malformed bodies and oversized responses remain uncertain.

After a committed effect request, later Conversation eligibility cannot revoke
the operation's permit; a still-live original Turn may finish that operation. A
later Turn is checked at the Conversation's current input floor and split
boundary when a fresh permit is requested. If the AI process stops between
acquisition and dispatch, a later Turn enters sticky RECOVERY for the original
operation. Recovery can close only a still-RESERVED acquisition under its scope
lock, then permit read/reconciliation and model context while forcing the final
answer from retained evidence. It cannot reserve, confirm, cancel or dispatch
another action. Customer-turn catch-up coalescing is not part of this phase;
intermediate queued turns remain a follow-up boundary.

## Compatibility and verification

Legacy `/v1/chat/completions`, Redis sessions, Bitrix and Google remain available.
Prompt Runtime V2 applies to raw and structured agents on both paths; canonical CRM
business execution requires the managed path. Owner settings do not migrate channels.
No BitrixAdapter, Google adapter, workflow engine or multi-provider routing is added.

Run the ordinary AI suite, migration drift checks, natural confirmation and typed
adapter tests. Real cross-service acceptance is under `tests/business_v2_e2e` and
must pass independently of fake-provider tests. Review/evidence is tracked in the
delivery report; this document alone makes no acceptance or deployment claim.
