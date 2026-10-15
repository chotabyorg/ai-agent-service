# Scoped Voice latency profile

`MANAGED_VOICE_LATENCY_PROFILES` is an optional JSON list, empty by default:

```json
[{"owner_id":"example-owner","integration_id":"00000000-0000-4000-8000-000000000001","agent_id":"00000000-0000-4000-8000-000000000002","model":"example/model-with-optional-reasoning"}]
```

Only an authenticated managed request with channel `VOICE` and the exact
owner/integration/agent/model tuple receives `reasoning.effort=none` and
`provider.sort=latency`. Public/legacy requests cannot select the profile with
their channel string. Profiles grant no authority. Changing the configured model
disables a match until an operator validates and explicitly adds the new model.

The model, prompt layers, knowledge retrieval, token limit, tool definitions,
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
different priced endpoint. Provider defaults for fallbacks and privacy are not
overridden. `require_parameters` is deliberately not set: it also filters on
unchanged parameters such as temperature and can leave this model without a route.

Rollback: remove the exact profile from the deployment configuration. There is
no agent database migration or update, and no text queue change.

## Native verification

Tests can use an explicitly disposable PostgreSQL database on localhost with a
name beginning `test_ai_`. Set `TEST_DATABASE_URL` and
`TEST_DATABASE_DISPOSABLE=yes`, then run `python -m pytest -q`. The native fixture
does not import or start testcontainers. Keep the production database out of tests.

An initial six-request synthetic comparison (2026-10-15, invented salon, about
4,300 prompt tokens, same model and token budget) measured baseline 4.076/5.962/2.153
seconds and scoped options 2.184/1.466/1.319 seconds. Requests alternated modes;
the small sample and provider/cache variance do not prove a production SLA.
No customer prompt, history, transcript or knowledge-base context was used.
