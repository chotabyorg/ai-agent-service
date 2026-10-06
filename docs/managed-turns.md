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
Current main always returns empty refs: tools have no durable business receipts.
The reserved extension accepts at most32 `{origin:AI_AGENT_SERVICE,operation_id:
opaque ASCII1..128,outcome:SUCCEEDED|FAILED|UNKNOWN|null}` values; it is not a ledger.

## Shared execution and runtime lifetime

Legacy and managed adapters use one `LLMService._execute_core` for existing
PromptBuilder, KB, model parameters, allowed tools/tier selection, first-name wins,
max20 model steps, raw-call dedupe, thinking removal and fallback. The legacy
completion hook persists only original history/current user/final answer inside
its ExitStack before MCP cleanup. Cleanup failure can still yield plain500 after
Redis save, matching existing behavior. Legacy raw tool-error continuation remains.

Managed configuration, DB work, client/MCP acquisition, execution and teardown
run in one retained owner task. HTTP cancellation/disconnect signals the active
operation CancelScope and joins that owner before propagating cancellation;
it never cancels the owner task or closes resources from another task. Repeated
cancellation still joins the same owner. Every runtime await uses one monotonic
work deadline (`MANAGED_AI_EXECUTION_SECONDS`, default90, validated1..240).
Direct AsyncOpenAI owns/closes its HTTP pool, finite timeout and max_retries0.
Independent KB/privacy clients close in bounded finally blocks.

Pinned MCP1.26.0 and AnyIO4.12.1 scopes enter/exit in the same owner and strict LIFO.
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

## Ambiguity and retries

The endpoint is not idempotent. Turn echoes are association, not execution receipts.
After any actual tool attempt, tool/model/privacy/budget/result/cleanup failure is
UNKNOWN, with no draft and no refs. The dispatch flag is set before awaiting tool
execution. Protocol isError and known nominal-success Bitrix root error/singleton/
exact result-wrapper or Google ok=false/error envelopes stop immediately, before
result append, another tool or another model call. Structured and each ordinary
complete JSON text representation are independently bounded/inspected; either error
wins. Caps:131072 combined UTF8 bytes,32 blocks, depth32,4096 JSON nodes, no cycles,
surrogates, duplicate keys or nonfinite values. Raw error never enters model/result/log.
Legacy bypasses this classifier and retains its error text continuation.

Before any tool attempt, known failures return FAILED with safe configuration,
model, timeout, context, setup, result or cleanup codes. KB remains fail-open.
Cancellation can prevent any response and does not undo effects. Conversation
must not retry a marked dispatch or treat transport timeout as proof of no effect.
Hidden/opaque/free-text errors, adapter-internal behavior, semantically repeated
successful calls and cross-Turn effects remain Goal3 business receipt/idempotency
work. No real channel is migrated or safe business retry guarantee claimed here.

No AI schema migration is added. Source syntax/import, package closure, full tests,
existing migration/drift and Python3.11 Docker build are author/fresh-verifier gates.
This repository has no configured lint/typecheck gate. Final acceptance additionally
requires independently reviewed and verified two-service HTTP tests.
