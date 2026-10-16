# End-to-end voice draft streaming: contract proposal

Goal: first audible answer ~1.5s after end of speech instead of ~3.3s, by
speaking finished sentences while the model still decodes the rest.

Measured baseline (pilot call, UTC 2026-10-16): STT final 0.14s,
conversation turn ~2.9s (PREPARE ~0.55s + full-draft MODEL ~1.85s for
~55 tokens), voice fetch + first TTS chunk ~0.7s. STT/TTS are already
streaming; the turn pipeline is fully buffered at every hop.

## Non-goals

- No change to default (non-voice) behavior anywhere.
- No playback of unauthorized text: only Conversation authorizes speech.
- No fallback models, no retries, no persistence of partials.

## Hop 1: ai-agent-service (done, PR #68)

`services/managed_streaming.py::stream_completion()` requests SSE and forwards
cumulative text partials while assembling the complete result for the existing
validation. Tool-call deltas stop partial emission; buffered answers pass
through untouched; bounds are explicit. Verified: live path uses direct
OpenRouter (`privacy_filter_enabled` is off in prod), so no proxy change is
needed for SSE. Not wired into any caller yet.

## Hop 2: managed turn request/response (proposed)

- `ManagedTurnRequest` gains explicit opt-in: `stream_partials: bool = False`
  (requires contract version 2) plus bounded `max_partial_chars`. Default
  off: byte-identical behavior.
- Streaming responses are server-sent events: interim
  `{"partial_text": <new complete sentence>, "partial_final": false}` events
  followed by one final event carrying the complete validated
  `ManagedTurnResult` with `partial_final: true`. Full `draft_text`
  semantics unchanged; partials never substitute the final text in
  transcripts or receipts. A revised, failed or tool-continued turn ends
  partial emission; the final event stays authoritative and always closes
  the stream. Sentence splitting is a documented heuristic (terminal mark +
  whitespace + uppercase); decimals never split.
- `_execute_core` accepts an optional partial hook and uses
  `stream_completion()` only when set; tool activity suppresses partials.

## Hop 3: conversation-service (proposed)

- Turn gains a partial-draft column updated monotonically; partials are
  metadata, never canonical history.
- `responses/next` may return an authorized partial sentence; `begin` grants
  per-sentence playback leases fenced by the same settling/authority
  revisions as full responses. A revised final cancels unplayed partials;
  already-played audio stays a playback fact, never evidence of hearing.
- Barge-in and hangup cancel partial playback through the existing paths.

## Hop 4: voice-gateway (proposed)

- `ConversationHTTPPort` accepts authorized partial sentences into the
  existing TTS fragment pipeline (prefetch/streaming already implemented).
  No change to VAD, endpointing, Conversation authority or playback-evidence
  rules. Partial text is never admitted to dialog memory; only finals are.

## Acceptance

- Unit/contract tests per slice; loopback streaming tests where transports
  are involved; synthetic provider probes for any model/voice change.
- Live pilot: first-audio latency from end of speech, per-turn phase timings
  (`PREPARE`/`MODEL` already emitted), and a controlled inbound call covering
  response, barge-in and hangup before any production activation.
- Each slice merges independently; nothing activates in prod until the full
  chain is verified and explicitly approved.
