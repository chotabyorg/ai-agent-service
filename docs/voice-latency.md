# Scoped Voice latency profile

`MANAGED_VOICE_LATENCY_PROFILES` is an optional JSON list, empty by default:

```json
[{"owner_id":"example-owner","integration_id":"00000000-0000-4000-8000-000000000001","agent_id":"00000000-0000-4000-8000-000000000002","model":"example/model-with-optional-reasoning"}]
```

With no replacement model configured, only an authenticated managed request with channel `VOICE` and the exact
owner/integration/agent/model tuple receives `reasoning.effort=none` and
`provider.sort=latency`. Public/legacy requests cannot select the profile with
their channel string. Profiles grant no authority. Changing the configured model
disables a match until an operator validates and explicitly adds the new model.

This default profile keeps the model, prompt layers, knowledge retrieval, token limit, tool definitions,
confirmation gates, business receipts and Conversation contract are unchanged.
The existing global managed reasoning-model configuration remains unchanged for
requests outside the Voice profile. There is no retry with different model or
text defaults if a provider rejects the request. All added wire fields count
toward the existing managed request budget, including tool continuation.

Activation requires direct OpenRouter transport, optional reasoning support for
the exact model, native isolation/tool tests and a synthetic provider probe.
OpenRouter documents [reasoning control](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
and [latency routing](https://openrouter.ai/docs/guides/routing/provider-selection).
Latency sorting is a preference, not a response-time guarantee; it may select a
different priced endpoint. In the default profile, provider defaults for fallbacks and privacy are not
overridden. `require_parameters` is deliberately not set: it also filters on
unchanged parameters such as temperature and can leave this model without a route.

## Optional Voice model replacement

A profile may add `completion_model`, `provider_only`, `max_input_price` and
`max_output_price`. All four are required together; prices are ceilings in USD
per million tokens. `model` still matches the original stored agent model. Only
the matching authenticated Voice request uses `completion_model`, including tool
continuation. Shared agent templates and text-channel requests keep their model.
Duplicate scope tuples are rejected at startup.

Replacement requests use `reasoning.enabled=false` and the explicit provider
allowlist as both `only` and `order`, with price ceilings. The first listed
provider is preferred; `allow_fallbacks=true` lets OpenRouter fall back only to
the later allowlisted providers under the same ceilings. Each provider
must support non-reasoning completion and the existing tool protocol. We retain
`parallel_tool_calls=false`; as above, `require_parameters` is not set because the
provider catalog may omit that parameter even when tool calling is supported.
The replacement model and routing fields count toward the context byte budget.
No prompt, history, tool authority or Conversation scheduling change is included.

The MTS runtime inspected on 2026-10-16 used Nemotron ASR, Gemma 4 26B A4B on
NextBit BF16 and Eleven Flash v2.5 / George through OpenRouter. Its old NextBit
ceilings (0.07 input / 0.26 output) now reject requests; the inspected endpoint
quotes 0.09 / 0.30. Activate only after an exact-route synthetic tool/continuation
probe, with the current bounded tariff. These component probes do not prove
audible delivery, production latency or model quality on real calls.

Rollback: remove the exact profile from the deployment configuration. There is
no agent database migration or update, and no text queue change.

## Optional signing material reuse

`reuse_signing_key: true` reuses the validated RSA private-key object inside one
matching authenticated Voice Turn. It is off by default. The request-owned
scope clears on normal completion, failure and cancellation. A different owner
cannot reuse it; changing the PEM revalidates it and an invalid replacement does
not fall back. Every CRM request still receives a fresh assertion with its own
scope, claims, 45-second expiry and unique `jti`. Authorization, grant reads,
Business guards, HTTP ownership and result freshness remain unchanged. No token,
grant, tool result or availability is cached. Text channels retain their prior
path.

On the live host, three synthetic comparisons of four signatures measured
522–539 ms without reuse and 128–155 ms with reuse (including the first parse).
These measurements send no CRM requests and do not establish end-to-end latency.

Matching Voice profiles also emit finite `voice_phase_timing` diagnostics:
`PREPARE` before the first model request, `MODEL` for each completion and `TOOL`
for each tool execution. Records contain only Turn UUID, step, phase, elapsed
milliseconds and a code-owned tool-name enum. Arguments, results and caller text
never enter these records. Text requests do not emit them. Timing observation
does not grant authority or alter the result.

## Optional short spoken replies

`concise_speech: true` adds a code-owned presentation instruction only to the
exact authenticated Voice profile. Typical replies use one or two sentences,
about 25 words, and start with useful content. Full requested details and all
facts needed for confirmation take priority over brevity. Output is not cut off,
and the model token allowance, tool schemas, authority gates, canonical history
and text-channel prompts stay unchanged. The instruction counts toward the
managed envelope budget and remains present during tool continuation.

This reduces avoidable answer generation and narration; it does not guarantee
one-second speech onset. Batch STT, end-of-speech, model prefill, tool round trips
and TTS all contribute to that interval. Provider prompt-cache hits are not an
application cache or permission to reuse business results.

An alternating synthetic salon probe on 2026-10-16 kept Gemma/NextBit, temperature
and the 1024-token allowance unchanged. In two warm repetitions of a services
and prices question, full completion took 906/1126 ms with the prior presentation
and 725/666 ms with concise speech (72/74 versus 54/55 output tokens). All answers
finished normally. This small fixture result excludes STT, runtime preparation,
CRM and TTS and is not a production latency claim.

## Native verification

When a Voice pending-decision response needs the existing one-shot prose repair,
its code-owned format message is retained only until the runtime processes that
decision. It is then removed by object identity before an ordinary answer can
continue without the semantic decision tool. Customer messages remain verbatim,
including text identical to the guidance. The first model request, repair text,
retry bound, tool schemas and runtime authorization rules remain unchanged.
This fixes contradictory follow-up guidance; it does not establish a latency
improvement or add guidance to the first model request.

Tests can use an explicitly disposable PostgreSQL database on localhost with a
name beginning `test_ai_`. Set `TEST_DATABASE_URL` and
`TEST_DATABASE_DISPOSABLE=yes`, then run `python -m pytest -q`. The native fixture
does not import or start testcontainers. Keep the production database out of tests.

An initial six-request synthetic comparison (2026-10-15, invented salon, about
4,300 prompt tokens, same model and token budget) measured baseline 4.076/5.962/2.153
seconds and scoped options 2.184/1.466/1.319 seconds. Requests alternated modes;
the small sample and provider/cache variance do not prove a production SLA.
No customer prompt, history, transcript or knowledge-base context was used.

## Pending decision admission diagnostics

For a current pending proposal, a copied model-facing decision schema advertises
only that proposal ID. It does not preselect the customer's intent, rewrite model
arguments or modify the canonical tool registry. A connection-check reply still
offers only `UNRELATED`. Exact proposal matching, one-call admission, the existing
prose-only repair bound, execution guards and later customer consent stay required.

Rejected pending responses emit only closed field/rule categories through the
existing managed Business log boundary. These distinguish call shape, target ID,
argument parsing, assistant-message serialization and envelope validation before
tool dispatch. They contain no proposal IDs, arguments, reasoning or conversation
content; observation failures do not change execution results or error codes.

The historical five-call failure had `TOOL_CALLS` telemetry and `INVALID_RESULT`
before any tool timing or effect attempt. Its raw response was not retained, so
the particular failing field is unknown. This change does not prove that a wrong
proposal ID caused that failure. Native regressions, full CI and new functional
booking/cancellation acceptance remain required. Measure provider schema adherence,
prompt cache coverage and latency; no speedup or full model-quality result follows
from the schema change alone.
