# P1c: AI owner API gateway workload admission

This is a dormant receiver change stacked on P1a commit
`076e723af7b4771f1633bd9029bc833b89072d96`. The PR base is
`security/p1a-internal-ownership-purpose`; it cannot merge before P1a and final
main compatibility review. Current main at implementation start is
`e44a3b0977523ae7ab73fef9a6e57c80c975a7d2`; P1a and main have diverged by two
commits each from `05d229c55cf141edfdc3ad72e066baf53cbae5ed`.

## Receiver contract

`GATEWAY_AI_AUTH_PRINCIPALS` is a SecretStr JSON array of at most 32 principals.
Each principal has exactly these fields:

| Field | Contract |
| --- | --- |
| `principal_id` | Stable unique identifier, 1–128 characters, same pattern as P1a |
| `caller` | Exactly `chota-gateway` |
| `purpose` | Exactly `gateway-owner-api` |
| `audience` | Exactly `ai-agent-service` |
| `current` | P1a credential shape: `version`, secret `token`, optional `revoked=false` |
| `previous` | Optional credential with a different version and token |
| `revoked` | Optional boolean, defaults false; disables the whole principal |

Credentials use P1a's ASCII `[A-Za-z0-9._~-]{32,256}` syntax. Unknown fields,
duplicate JSON keys/principal IDs/versions/tokens, invalid purposes/audiences and
token reuse across any gateway version, legacy internal, chat, managed,
ownership current/previous (including revoked), or Business Conversation fail
startup with fixed error text. Strict mode requires an active version.

The gateway sends exactly one `X-Gateway-Token`, one UUID `X-User-Id`, and at
most one `X-User-Role`. Raw header names are compared case-insensitively;
duplicate values are rejected even if equal. Comma lists, whitespace/control
characters, non-ASCII tokens/identities/roles and competing `Authorization` or
`X-Service-Token` are rejected with fixed `401` before opening the owner DB
dependency or calling a service/provider. Missing or empty role retains its
existing non-admin behavior. Roles are not normalized or given a new enum.

The workload credential establishes who may assert the JWT-derived identity.
It does not replace owner predicates, the existing exact `admin` deletion
override, or Business cookie, Origin and independent CRM membership checks.
The gateway can assert dynamic tenants by design; possession of its AI
credential is therefore privileged and is not granted to KB, CP, channels,
Conversation or browsers.

## Exact cohort

Admission is attached to router inclusions before endpoint dependencies.
It covers 21 operations, with the configured API prefix (normally `/v1`):

| Suffix under `/v1/agents` | Methods |
| --- | --- |
| `/` | POST, GET |
| `/{agent_id}` | GET, PUT, DELETE |
| `/{agent_id}/prompt-config` | GET, PUT |
| `/{agent_id}/prompt-preview` | GET |
| `/{agent_id}/credentials` | GET, PUT |
| `/{agent_id}/credentials/{service_name}` | DELETE |
| `/{agent_id}/tools/{service_name}` | GET, PUT |
| `/{agent_id}/openrouter-key` | PUT, GET, DELETE |
| `/{agent_id}/business-tools/workspaces` | GET |
| `/{agent_id}/business-tools` | GET, PUT, PATCH, DELETE |

Public `/v1/models`, the MCP catalog, health/docs/OpenAPI, chat and all internal
routes retain their existing contracts. This is not a blanket `/v1/*` guard.
The gateway still requires JWT for its MCP catalog endpoint; this receiver PR
does not extend that catalog's backend admission.

## Transition, rollout and rollback

`GATEWAY_OWNER_AUTH_MODE=legacy` is the explicit default. Only absence of the
gateway header preserves previous header-only behavior; this remains a known
SEC-002 residual gap. Duplicate owner/role headers are rejected even in this
transition without a gateway header; missing legacy fields retain endpoint
validation behavior. Empty, invalid, ambiguous, unknown or revoked supplied
credentials never fall back. `required` removes the header-only branch. There
is no outage-triggered fallback, browser JWT secret, or session change.

Do not enable required mode until these separate gates pass:

1. Merge/rebase P1a and this independently reviewed receiver. Provision a
   distinct gateway→AI credential; use current/previous overlap for rotation.
   Do not reuse a CP/KB/chat/managed/Business credential.
2. Deploy the KB P1b caller in explicit `AI_OWNERSHIP_CHECK_MODE=internal` with
   its own ownership principal. Its default `legacy` mode still uses
   `GET /v1/agents/{id}` and would fail after owner enforcement. CP already uses
   the separate ownership path; its dedicated P1a principal rollout remains a
   separate prerequisite for strict internal ownership enforcement.
3. Promote a separately reviewed gateway configuration covering every method
   of all 21 routes, with the same protected JWT/identity semantics. This PR
   supplies no full gateway configuration or GitOps/Secret rollout. A merge of
   this receiver is not proof that production injects `X-Gateway-Token`.
4. Verify staging normal/admin, tenant A/B, Business settings, KB creation,
   chat, managed/Goal5 and subscription lifecycle positives and negatives.
   Then set `required` explicitly and repeat admission tests after restart.

The disposable proof uses the exact production-pinned KrakenD 2.13.3 digest,
raw non-JSON flexible template, validated `env`/`toJson`, request-only Martian
header overwrite, no `FC_OUT`, and synthetic credentials only. It does not
provide the production 21-route template. Native JSON fallback can start after
template errors, and malformed Martian config can run without injection: the
required receiver must still deny those requests. KrakenD's default ERROR
logging can dump malformed modifier configuration including resolved secrets.
The proof's explicit `CRITICAL` logging policy suppresses that dump at the cost
of lower-level diagnostics; validated equivalent redaction is an alternative
future implementation. Safe logging, template correctness and no secret
response/config exports are required gateway rollout gates, not properties
this receiver can enforce.

`OWNER_AI_TOKEN` and `OWNER_CP_TOKEN` are disposable proof names, not deployed
Secret contracts. They, reserved `GATEWAY_AI_OWNER_TOKEN`, and receiver registry
names are excluded case-insensitively from both legacy and managed MCP child
environments, including attempts to reintroduce them through credential maps.
This targeted exclusion does not claim a general legacy environment allowlist.

For rotation keep the same principal ID, install a distinct new current
version and the old authenticated previous version, switch the gateway, then
revoke/remove previous after acceptance. A revoked recognized token never
falls through. Prefer rollback to a compatible receiver/config that retains
authenticated admission. Returning to header-only legacy is an explicit
emergency residual-risk decision and is incompatible with a migrated KB
rollback once the old owner path is closed. Do not revert Business state,
ownership, permits/revisions, UNKNOWN reconciliation or customer identification.
