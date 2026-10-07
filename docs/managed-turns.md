# Internal managed AgentTurn execution v1

`POST /internal/v1/managed-turns/execute` is an internal harness contract for
Conversation Service. No public gateway route, scheduler, consumer, deployment,
production configuration or provider delivery is added. Existing channel callers
continue using `/v1/chat/completions`, with unchanged DTOs/auth/session/Redis/
prompt/KB/tool/error behavior.

Conversation owns canonical Messages, ordering, selected input, stable Turn,
claims/fencing, draft persistence and future delivery. AI loads agent configuration
and owns prompt composition, KB, context budgeting, reasoning and tools. Managed
execution invokes neither SessionService nor HistoryService. A draft is not a
delivered Message and is not persisted here.

## Private authentication and complete owner mapping

Optional `MANAGED_AI_AUTH_PRINCIPALS` is a private JSON registry; empty by default
rejects every managed request. Invalid configuration raises a fixed safe startup
error. Each of at most32 principals has a unique ASCII `principal_id`, dedicated
32..256-character `[A-Za-z0-9._~-]` token, literal caller `conversation-service`,
namespace `main-chota.conversation.v1`, and1..64 exact grants:

```json
{
  "principal_id": "local-conversation",
  "token": "<dedicated private service token>",
  "caller": "conversation-service",
  "execution_namespace": "main-chota.conversation.v1",
  "grants": [{
    "owner_id": "<exact opaque Conversation owner>",
    "integration_id": "<integration UUID>",
    "channel": "TELEGRAM",
    "agent_id": "<agent UUID>",
    "agent_owner_id": "<authoritative AI user UUID>"
  }]
}
```

The registry is an array of these objects. Placeholders above are documentation,
not usable credentials. No production registration is created by this Goal.
Duplicate principals/tokens/grants, wildcard owners, inconsistent owner mappings
and reuse of legacy/internal tokens are rejected. Opaque owner identity is not
normalized or parsed as UUID. The complete trusted grant maps it to the real AI
UUID owner. Agent lookup uses both agent ID and trusted owner UUID; unknown,
foreign and ungranted tuples share the same404.

A pure ASGI boundary checks exactly one `X-Service-Token` before consuming body
bytes, parsing JSON or opening DB resources. It compares every configured token.
It accepts only uncompressed JSON and streams at most1048576 actual body bytes.
Managed validation errors use fixed messages without rejected values. Boundary
responses are `{ "detail": { "code": "...", "message": "..." } }`:

| HTTP | Code | Fixed message |
| --- | --- | --- |
|401|UNAUTHENTICATED|Managed authentication required.|
|404|AGENT_NOT_FOUND|Agent not found.|
|413|PAYLOAD_TOO_LARGE|Managed request exceeds limit.|
|415|UNSUPPORTED_MEDIA_TYPE|Managed request requires uncompressed JSON.|
|422|INVALID_CONTEXT|Invalid managed context.|

## Request and result

All request objects forbid extra fields. Required UUIDs: `conversation_id`,
`turn_id`, `integration_id`, `agent_id`. Required `owner_id` is exact nonblank
opaque text of1..255 characters/1020 UTF8 bytes, without Cc/Cs controls or wildcard.
`channel` is TELEGRAM/INSTAGRAM/WEBSITE_WIDGET/WHATSAPP/VOICE.
`contract_version` defaults to strict integer1. Counters are strict bounded bigint
integers: positive `input_revision`, `input_first_sequence`, `input_last_sequence`,
`context_last_sequence`; nonnegative `authority_revision`.

`messages` is the full frozen canonical prefix of1..256 values, each with UUID
`id`, positive `sequence`, actor CUSTOMER/AGENT/HUMAN/SYSTEM, direction
INBOUND/OUTBOUND, literal `content_type=TEXT` and exact `text` (1..16384 characters,
32768 UTF8 bytes, no NUL/surrogate). Sequences must be exactly1..context_last,
without gaps/duplicates/reorder; total text is bounded to524288 UTF8 bytes.
`selected_input_message_ids` must equal the IDs in the inclusive first..last
range in canonical order. Selection may include multiple messages or be nonsuffix;
selected text is never appended again or reordered.

AI maps CUSTOMER to user, AGENT/HUMAN to assistant, SYSTEM to ordinary user with
`[System communication event]\n` prefix. SYSTEM never becomes privileged prompt
authority. Channel mapping uses existing prompt values: telegram/instagram/
website/whatsapp/voice. For a single suffix CUSTOMER input, model composition is
equivalent to legacy. Other selections add one AI-owned fixed system selector of
retained communication ordinal positions; no payload text enters that instruction.

KB query is selected original text joined with `\n\n`, bounded to32768 characters/
65536 UTF8 bytes. KB uses the loaded agent's UUID user, retains formatting and
fail-open semantics. Privacy remains fail-closed. Managed privacy UUID5 uses
namespace `bc7459ee-8fe0-5c64-95f1-953ae385c7aa` and compact UTF8 JSON array
`[execution_namespace,owner_id,integration_id,agent_id,channel,conversation_id]`.
Turn ID is excluded; no legacy Session row or canonical alias is created.

`MANAGED_AI_CONTEXT_BYTES` defaults to131072 actual UTF8 JSON envelope bytes;
this is a conservative byte bound, not exact token accounting. Protect system/KB/
selector and selected inputs, then drop oldest whole unselected communication
messages while preserving remaining order. Recompute selector positions. Protected
context/tool descriptors that cannot fit fail CONTEXT_LIMIT. Later steps never
drop tool protocol pairs. Legacy budgeting and configured completion token cap
remain unchanged.

Runtime results always return200 with exact UUID/counter echoes, status
DRAFT_READY/NO_RESPONSE/FAILED/UNKNOWN, `draft_text`, closed safe `error_code`, and
`operation_refs`. DRAFT_READY requires valid nonnull draft and null error; NO_RESPONSE
requires both null; FAILED/UNKNOWN require null draft and a safe code. Normal empty
model answers keep the existing fallback draft. Serialized result cap262144 bytes.
AI business PostgreSQL rows supply genuine refs through every outcome, including
model, invalid draft and cleanup failures. RESERVED maps to explicit null;
DISPATCHING/UNKNOWN maps to UNKNOWN; terminal outcomes stay immutable. Only
positively committed reservations/reads/receipts enter the collector; an unavailable
final reload retains its known conservative snapshot. The contract accepts at most32 `{origin:AI_AGENT_SERVICE,operation_id:
opaque ASCII1..128,outcome:SUCCEEDED|FAILED|UNKNOWN|null}` values; it is not a ledger.

## Shared execution and runtime lifetime

Legacy and managed adapters use one `LLMService._execute_core` for PromptBuilder,
KB, model parameters, max20 model steps, thinking removal and fallback. Legacy
keeps its allowed tools/tier selection, first-name wins and raw-call dedupe.
Managed execution skips credential lookup/decryption, MCP construction/acquisition,
subprocesses, tool listing, metadata and resource reads entirely. All42 raw writes,
all33 bundled reads and dynamic/custom/spoofed tools are quarantined. Invocation
independently denies hidden/unknown/disabled names before argument parsing or
another model response; annotations, names, dry-run and script overrides confer
no permission. No production raw-MCP bypass exists.

The real app uses immutable `EMPTY_BUSINESS_REGISTRY`. Only explicit code-owned
application dependencies may inject approved typed adapters and the independent
short-session factory on the same agent database. An overridden agent DB without
an explicit business factory fails closed; no request/env JSON can choose an
adapter, connection, owner, URL, key, receipt or operation ID. Legacy runtime=None
never opens this business runtime. Managed no-tool prompt/KB/envelope parity stays;
raw-MCP/read parity is an intentional capability limitation.

Enabled business controls freeze one immutable ordered1..32-action plan per Turn.
Identity is `(business_scope_id,origin_platform_turn_id,ordinal)`, with a separately
normalized server fingerprint. Changed plan/order/count/payload conflicts; model
call IDs/JSON formatting do not change identity. Later deliberate Turns get new
operations; confirming/recovery Turns select the original plan. Approved repeated
calls reach durable replay/conflict checks instead of the legacy string guard.
Reservation COMMIT precedes separate dispatch-permission COMMIT and provider send.
Every admission rechecks current authority/consent/facts and all same-scope
DISPATCHING/UNKNOWN blockers, including old reservations and expired attempts.
Blocking preserves RESERVED/null refs and original consent, halts continuation,
and returns processing UNKNOWN/TOOL_EXECUTION_UNKNOWN with no draft. Ordered
predecessors must be durably SUCCEEDED. No database lock spans provider/model HTTP.

A positive receipt COMMIT survives draft non-delivery and downstream faults.
UNKNOWN is not FAILED; native recovery is an explicit trusted same-original-key
permission with current checks and sufficient retained guarantee, never model or
Conversation retry. Non-native ambiguity permits only authoritative read lookup
or manual attention; BLIND may remain UNKNOWN indefinitely. Real providers are
unregistered. Pending proposals return deterministic bounded frozen public fields
and their handle. Consent requires a later selected canonical CUSTOMER TEXT equal
to `confirm <canonical-lowercase-proposal-uuid>` or `подтверждаю <canonical-lowercase-proposal-uuid>`
after outer ASCII spaces only; prompt/model booleans or bare yes/да grant nothing.
FAILED/UNKNOWN business results stop additional model/mutation continuation.

The legacy completion hook persists only original history/current user/final answer inside
its ExitStack before MCP cleanup. Cleanup failure can still yield plain500 after
Redis save, matching existing behavior. Legacy raw tool-error continuation remains.

Managed configuration, DB work, model/business transport acquisition, execution and teardown
run in one retained owner task. HTTP cancellation/disconnect signals the active
operation CancelScope and joins that owner before propagating cancellation;
it never cancels the owner task or closes resources from another task. Repeated
cancellation still joins the same owner. Every runtime await uses one monotonic
work deadline (`MANAGED_AI_EXECUTION_SECONDS`, default90, validated1..240).
Direct AsyncOpenAI owns/closes its HTTP pool, finite timeout and max_retries0.
Independent KB/privacy clients use `trust_env=false` and close in their owner's
finally blocks. KB retrieval/formatting stays fail-open; resource cleanup is a
separate certainty requirement. A raised or expired two-second helper close stops
execution with TOOL_CLEANUP_FAILED (FAILED before any tool, UNKNOWN afterward),
even if the retained final teardown subsequently succeeds. No model/tool/HTTP
request is repeated for cleanup.

HTTPX0.28.1 and httpcore1.0.9 are pinned to the already inspected versions. Their
client/pool bookkeeping can report closed/remove connections before socket close
finishes. Before any HTTP operation, the narrow managed cleanup module installs a
request-local backend/stream owner on every native HTTP/1 pool, including default
forward and CONNECT proxy mounts. It retains actual AnyIO stream close handles,
native transport abort handles and captured socket objects immediately after
TCP/Unix acquisition, with no await before retention,
and retains them across TLS setup. A malformed header, read/body failure or cancelled
TLS handshake can therefore discard pool/protocol bookkeeping without losing the
actual owner. Operation-time stream close also has a two-second detection window;
its error/deadline marks cleanup uncertain and prevents later model/tool work.
Every final close joins acquired streams even when ordinary client.close succeeds
against an empty pool. It always aborts the captured native transport and joins
the captured socket's physical closure and empty native write buffer, including already-closing state
left by the native close checkpoint's own error/deadline. Native Unix closure uses
its captured socket owner. No bare descriptor integer is closed. A failed finite
close joins all retained streams/pools in the same owner;
it does not rely on client.is_closed or repeat a potentially no-op client.aclose.
Legacy HTTP helper defaults/context manager/lifetime remain unchanged. No environment
proxy pool is used in managed helper requests. The final retained join, like the
SDK's final process reap, cannot promise an absolute bound for an uncooperative
resource/OS; the owner remains joined instead of abandoning resources.
Supported private ABI is HTTPX0.28.1/httpcore1.0.9 with AnyIO4.12.1 on asyncio.
Socketless MockTransport is an explicit test seam; HTTP/2, custom transports/backends
and other native versions are unsupported, rather than silently assumed safe.

The direct managed OpenAI client uses the same retained HTTP teardown after inner
business transport scopes exit; direct component tests also exercise MCP scopes.
OpenAI2.26.0 is also pinned to the inspected existing runtime because
that narrow seam reads its SDK-owned HTTPX client before SDK.close. The initial
SDK close still runs; a failure/deadline subsequently joins actual retained sockets,
connection handles and pools in the same owner and returns TOOL_CLEANUP_FAILED.
Proxy connection wrappers are traversed to their native HTTP/1 stream when present.
SDK/client closed flags and an emptied pool alone are never cleanup evidence.
The legacy direct SDK constructor, retry/default behavior and lifetime are unchanged.
The managed DB provider is the first resource registered and the last to exit.
After all MCP/HTTP scopes unwind, its exit joins SQLAlchemy's shielded session-close
task under a final owner shield. An already expired outer cleanup budget cannot
detach that close task or release the dependency early. The outer budget miss still
prevents a successful result. This final retained provider join also has no absolute
bound if the underlying resource remains uncooperative; the owner stays joined.
The lifetime-only DB provider is resumed normally so the production async-for
relay drains its inner session provider; an earlier SDK cleanup exception stays
with the ExitStack instead of being thrown into that relay. Managed session exit
does not commit a transaction. Legacy dependency behavior is unchanged.

Raw MCP is unreachable through the managed endpoint. Direct component regression
coverage retains pinned MCP1.26.0 and AnyIO4.12.1 acquisition/partial setup/stdio/
cancellation/child-reap scopes in the same owner and strict LIFO, without a
production managed-policy override.
One lifecycle shield enters before any SDK scope. The owner's ExitStack registers
devnull before stdio, then stdio/session before initialize/list/metadata awaits.
Only unsupported/missing credentials before acquisition may skip a service;
postspawn setup failure always unwinds. Cleanup changes the existing lifecycle
deadline in place to6s per started stdio +2s (at most14s for current two families).
The SDK closes stdin, waits2s, sends process-group TERM, then KILL after another2s.
No new shield or timeout wraps stack.aclose across SDK scopes. Devnull closes after
transport; SDK/client close runs after MCP scopes. Final OS reap/task joins can
exceed any absolute guarantee under uninterruptible OS behavior; the implementation
does not abandon owners to claim a hard bound. Abrupt process/host death is outside
in-process cleanup, and Conversation retains its durable one-attempt marker.

Managed logs use a ContextVar-aware record boundary that strips exception/body/
SQL/DSN/tool/key/token details, including SDK child-task records, through cleanup.
Concurrent legacy logs retain their existing behavior. Managed MCP child stderr
goes to owned devnull. Newly introduced managed secret env keys are filtered
case-insensitively from subprocess env on both adapters, and denied after credential
mappings. Existing per-agent mapped tool credentials remain available. This is
not a subprocess filesystem sandbox.

## Retained read-only evidence and crash F

`POST /internal/v1/managed-turns/evidence` uses the same independent managed
principal registry. Authentication precedes body receive; exact scope grant
selection precedes DB work. Only the canonical POST path is accepted; evidence
slash aliases return safe404 and other methods safe405, with no body read.
Actual streamed uncompressed UTF8 JSON is capped at4096 bytes. Duplicate keys,
nonfinite values, extra fields and invalid versions/types are rejected using the
same fixed safe errors and no-store. Old internal/chat tokens cannot authorize it.

Request fields: strict `contract_version=1`, exact `owner_id`, `integration_id`,
`channel`, `agent_id`, `conversation_id`, `turn_id`. Response: version1,
`conversation_id`, `turn_id`, `agent_id`, and the existing sorted unique≤32
`operation_refs`. Foreign/guessed/nonexistent scope/Turn share404; retained Turn
with no selected plan returns[]. It accepts no caller operation ID/provider
receipt/hash/URL. Complete existing grants may read retained evidence after live
agent deletion/revocation. It invokes no BusinessRuntime, model, MCP, provider,
reconciliation, recovery normalization or state transition. Receipt payloads,
credentials and confirmation details never cross this boundary.

Conversation already stores genuine refs on the same AgentTurn through existing
complete/fail under winning proof, lease and immutable terminal fingerprint.
No Conversation source/schema/second ledger is added. The explicit unconsumed
crash-F procedure is: kill CS after AI result but before its finalizer COMMIT;
restart/reclaim the same T1 with new proof; read scoped AI evidence; submit existing
CS fail(EXECUTION_UNKNOWN,refs) before marker-only execute recovery. AI/model/provider
execution is not repeated. A CS result already terminalized with empty refs cannot
be enriched by current APIs; retained AI origin evidence remains independently
readable. No automatic scheduler/enrichment or supersede policy is supplied.

## Ambiguity and retries

The reasoning HTTP attempt is not a general safe retry API. Business operations
have durable identity and provider-specific recovery authority; Conversation still
must not blindly repeat its marked managed execute. A received draft is neither
delivery nor rollback permission. Processing failure after a committed attempted
effect returns UNKNOWN with a null draft and genuine independent refs. No earlier
SUCCEEDED/UNKNOWN is deleted or downgraded because a later model/draft/cleanup fails.
Before any business attempt, known failures use existing FAILED/error vocabulary;
a scope/predecessor block explicitly uses UNKNOWN even for a RESERVED candidate.
Cancellation may prevent an HTTP response; scoped evidence recovers durable refs.

All native KB/privacy/OpenAI timeout/body/TLS/proxy/socket ownership and last DB
close guarantees above remain. Post-effect resource faults use typed fake adapters
through the real ledger; raw MCP resource regressions use direct component seams.
No real channel, provider, deployment or GitOps state changes. Full relevant
legacy/managed/business suites, additive migration/drift/import/package/Python3.11
build and independent paired-process verification are separate required gates.
