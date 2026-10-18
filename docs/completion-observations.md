# Completion observations

`managed_completion_observed` records finite metadata for each managed model
step on every channel. `model_ms` measures local wall time from starting the
completion request until its result has been assembled, including consumption
of streaming partial callbacks. It excludes the preceding execution guard and
business tool dispatch. These durations must not be added to overlapping
`voice_phase_timing` MODEL durations.

For actual streams, `first_delta_ms` measures time to the first nonempty text
delta or tool-call identifier/name/argument fragment. Empty role, terminal and
accounting frames do not start this clock. This is not provider-side inference
time or audio latency, and it does not count hidden reasoning tokens. Buffered
responses and buffered degradation leave this metric absent, rather than
pretending their full response duration is a first-token measurement.

The streaming assembler retains only bounded integer usage counts and reasoning
token counts. OpenRouter repeats the terminal choice in its accounting frame;
OpenAI-compatible streams may send an empty `choices` array instead. Both are
accepted without an extra partial or tool call. Accounting frames still count
towards the stream chunk limit; error frames are not accounting-only frames.
No request options are added to obtain usage. See the
[OpenRouter streaming contract](https://openrouter.ai/docs/api_reference/streaming).

Timings are captured locally, never copied from provider fields. Existing log
sanitization validates every metric and drops arbitrary metadata. Prompts,
transcripts, tool arguments/results, generation IDs and provider extras are not
retained. These observations do not change managed request identity, public
request/result schemas, tool validation, confirmation or dispatch authority.
