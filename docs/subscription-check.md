# Subscription activation check

The AI service checks permission before it creates an agent with
`is_active=true` or updates an agent to `is_active=true`. It calls
`GET /v1/subscriptions/check?action=activate_agent` with a canonical
`X-User-Id` UUID. When configured, it also sends the dedicated
`X-Subscription-Check-Token` header. It never uses `INTERNAL_API_TOKEN`, the
chat credential, or an Authorization header for this call.

`SUBSCRIPTION_CHECK_ENABLED=false` keeps the free-mode behavior. When enabled,
the service requires a valid subscription-service URL and a live client. A
missing client, non-200 response (including redirects and authentication
errors), transport failure, timeout, or malformed/inconsistent response blocks
activation with a sanitized 503. A valid denial returns 402 with one of
`no_subscription`, `subscription_expired`, or `agent_limit_reached`. Creating
an inactive agent and deactivation do not call subscription-service.

The response must be a JSON object with a strict boolean `allowed`. An allowed
decision requires absent or null `reason` and integer `limits.max_agents` and
`limits.used_agents` values where `used_agents < max_agents`. A denied decision
requires a known reason. `agent_limit_reached` also requires valid limits where
`used_agents >= max_agents`; other denial reasons may omit limits. Duplicate
JSON keys, unknown fields, truthy non-boolean values, invalid limit types, and
contradictory decisions are treated as unavailable.

## Credential rollout

`SUBSCRIPTION_CHECK_AUTH_MODE=legacy` is the migration default. It preserves the
current caller behavior when `SUBSCRIPTION_CHECK_TOKEN` is empty. If a token is
configured, it must be a dedicated 32–256 character token using letters,
digits, `.`, `_`, `~`, or `-`, and it must differ from every other configured
service credential. The token is stored as a secret setting and is not included
in request logs or error responses.

Deploy the caller and subscription-service with support for the dedicated
header before setting `SUBSCRIPTION_CHECK_AUTH_MODE=required`. In required mode,
an enabled check cannot start without a valid dedicated token. During rollout,
keep the subscription feature flag enabled according to the product's configured
subscription policy. An outage must not be handled by disabling it: that would
intentionally restore free-mode activation and bypass the configured policy.

The receiver must be deployed first in legacy mode. Install its scoped AI
principal, then roll out this caller with its dedicated token. Verify every
replica before requiring authentication. The public browser check also needs a
separate gateway credential injected on `/check`; the existing P1c gateway
candidate excludes that operation and must be extended and verified first.
Current/previous overlap and revocation happen on the receiver; an old caller
without the new token must never be rolled back against a required receiver.

This PR is stacked on the pinned AI owner boundary from #141, which depends on
#138. Those existing PRs and their dependencies are unchanged. The separately
tested Subscription companion is stacked on its #8 owner boundary. The
optional compatibility mode does not prove production
enforcement. Count and activation remain separate operations, so concurrent
activations can still race for the same quota; this patch adds no quota lease.

The dedicated `SUBSCRIPTION_CHECK_TOKEN` is excluded from MCP child environment
through the existing managed-secret denylist. A focused negative test covers
ambient inheritance, case aliases, and required/optional mapping attempts to
reintroduce it. This is the only MCP environment change in this patch; no broader
child-environment policy or isolation change is included.
