# Experimental Voice prompt warmup

Status: source candidate, disabled by default. This is not a released latency claim.
Keep the configured Gemma model and provider. No text-channel scheduler changes.

`POST /internal/v1/voice/warmup` accepts only a bounded Voice cache hint from an
existing Conversation managed principal with the exact owner/integration/agent
grant. It accepts no messages, model choice, tool parameters or execution claims.
The existing Voice latency profile must additionally set `prompt_warmup: true`.

The endpoint reads the active agent and owner connection configuration in a
read-only transaction, renders the ordinary Voice system prompt and code-owned
tool schemas, and sends one model request capped at **one output token**. It never
opens Business execution, retrieves RAG, executes tools, or writes messages,
proposals, operations or history. All generated content is discarded, including
tool calls. Real turns rebuild their own fresh context and permissions.

The tool schemas must match the ordinary request's serialization to have a chance
of sharing a prefix cache. Schema preview is not an authority grant. Configuration
changes, pending proposals, time boundaries and provider routing can cause misses.
A hashed profile-scoped `session_id` supplies routing affinity for warmup and actual
Voice requests. It contains no raw tenant identifiers and carries no conversation
state. Other profiles and text channels retain their previous request options.

Admission is at most one in-flight warmup per AI process and one attempt per profile
per 30 seconds, with at most 64 retained admission timestamps. This is a cost bound,
not an assertion about provider cache TTL. The operation deadline is 2.5 seconds;
the existing managed owner joins cancellation and bounded HTTP/DB cleanup.

Conversation must start the hint only after a new call's trusted admission commits.
Its separate opt-in is `VOICE_PROMPT_WARMUP_ENABLED=true`. It cancels an unfinished
hint on the first admitted utterance, call end or application shutdown. Neither
call admission nor actual turn execution waits for prefill. Replayed starts do not
trigger another attempt. No Voice Gateway protocol or media deployment is needed.

## Measurements, 2026-10-16

Synthetic requests from Main to the existing Gemma/NextBit route, with invented
salon data and a non-executed fictional tool. No customer audio or conversation
content was used. Separate HTTP clients were used per request in the stricter runs,
alternating cold/warm order and using a fresh prefix per trial arm. Responses were
10 tokens; input was approximately 5,430 tokens. These are full model request
durations, **not end-of-speech to first telephone sound**.

| Variant | Pairs | Cold median | Warm median | Warm cache hits |
|---|---:|---:|---:|---:|
| One-token prefill | 5 | 702 ms | 607 ms | 1 / 5 |
| Finished short prefill, max 8 tokens | 4 | 907 ms | 587.5 ms | 0 / 4 |
| One-token prefill + explicit routing session | 5 | 548 ms | 575 ms | 0 / 5 |

The first run's apparent 95 ms improvement did not reproduce with routing affinity.
Provider variance dominates this small sample. **Reliable acceleration is not
demonstrated; do not enable by default or claim a one-second call response.**
One prefill cost about $0.000489 at the tested prompt size. The three strict runs
cost $0.02040406 total. Earlier exploratory runs are additional.

Provider reports are retained on Main under `/root/chota-voice-synthetic-warmup-`
with `final-20261016.json`, `complete-20261016.json`, and `sticky-20261016.json`
suffixes. Only safe timing, usage, validation booleans and provider metadata are in
those reports. An attempted benchmark using the live connection configuration was
blocked by automatic approval review and was not run; the replacement used only
invented data/schema.

## Release and larger latency work

Before any pilot activation: build exact AI and Conversation images, review the
pair, publish through GitOps/Argo, verify health and existing text regressions,
and prove benefit on the single authorized number. Disable both flags to roll back
the optimization. Do not patch running images or bypass Argo. The latest observed
GitHub Actions job was blocked by account billing/spending limits.

The current call path waits for a complete managed draft and Conversation playback
permission before TTS. Large gains require Voice-specific incremental output with
authority/cancellation fencing; simply speaking raw model tokens would bypass that
contract. Audio TTS already streams and uses PCM, so enabling HTTP streaming again
will not remove the full-draft wait. Streaming input transcription would overlap
recognition with caller speech; the current Whisper endpoint consumes a whole audio
segment. These are separate changes, not implemented by this warmup feature.

References checked on 2026-10-16:
- [OpenRouter caching and session affinity](https://openrouter.ai/docs/guides/best-practices/prompt-caching)
- [OpenRouter streaming](https://openrouter.ai/docs/api/reference/streaming)
- [OpenRouter transcription endpoint](https://openrouter.ai/blog/tutorials/transcription-on-openrouter/)
- [ElevenLabs latency guidance](https://elevenlabs.io/docs/api-reference/reducing-latency)
- [ElevenLabs streaming voice pipeline](https://elevenlabs.io/blog/voice-agent-latency-optimization)
