# P1a: ownership-check receiver authorization

This is a receiver-first, application-only change for SEC-002. It provisions no
credentials, enables no production enforcement and changes no caller, gateway,
Business transport, schema, ledger, grants or deployment.

## Admission and compatibility

`POST /internal/v1/agents/ownership-check` keeps its JSON body
`{"agent_id":"<uuid>","owner_id":"<uuid>"}` and empty 204 success. A nonexistent
agent and a foreign owner still produce the same 404 `Agent not found`.

Send exactly one `X-Service-Token` header. Identity, role, caller and purpose
headers never establish a principal or expand its grants. The receiver identifies
the principal from its configured credential; there is no client-selected version
or principal header. `Authorization` is not an alternative credential on these
routes. Duplicate headers, combined credentials and ambiguous authorization fail.
Authentication failure returns 401 `Invalid service token`, without a credential
or a principal identifier. A valid principal outside its exact pair grants gets
the same 404 as an absent agent, before database access. The database owner
predicate is still checked for every granted request.

| Credential | Ownership, default transition mode | Ownership, strict mode | Count / deactivate-all |
| --- | --- | --- | --- |
| Active ownership principal current / previous version | Allowed within configured scope | Allowed within configured scope | 401 |
| Existing nonempty `INTERNAL_API_TOKEN` | Existing authenticated behavior | 401 | Existing authenticated behavior |
| Missing, invalid, revoked, chat, managed or Business credential | 401 | 401 | 401 |

`INTERNAL_OWNERSHIP_AUTH_REQUIRED=false` is the default. It retains only the
existing authenticated legacy path, never anonymous access or an unknown-token
fallback. During this transition the old shared credential can still perform
ownership checks **and management**. Merging this change alone does not close
that deployed shared-credential risk. New ownership credentials are isolated
immediately when configured, including while transition mode remains active.

`INTERNAL_OWNERSHIP_AUTH_REQUIRED=true` removes the legacy credential from
ownership admission. Strict configuration without any active ownership principal
fails startup. Invalid supplied registry configuration fails startup in either
mode; disabling enforcement never makes a malformed registry acceptable.

Strict ownership removes the legacy token from ownership-check only. Any caller
that still possesses `INTERNAL_API_TOKEN` retains management access, even after
its ownership client switches to a narrow credential. Removing that broad
distribution and rotating the management credential require a separate approved
change; neither is performed by this PR.

## Receiver registry

`INTERNAL_AI_AUTH_PRINCIPALS` is a secret JSON list, empty by default, with at most
32 principals. It extends the existing internal/managed authorization conventions:
strict value models, receiver-owned grants, `SecretStr`, bounded token syntax,
constant-time credential comparisons and safe startup errors. The managed
principal contract and complete managed grant tuples stay unchanged.

Illustrative shape only; placeholders below are not usable credentials:

```json
[
  {
    "principal_id": "kb-ai-ownership",
    "caller": "knowledge-base-service",
    "purpose": "agent-ownership-check",
    "current": {"version": "v2", "token": "<dedicated-current-secret>", "revoked": false},
    "previous": {"version": "v1", "token": "<dedicated-previous-secret>", "revoked": false},
    "revoked": false,
    "all_tenants": false,
    "grants": [
      {"owner_id": "11111111-1111-1111-1111-111111111111", "agent_id": "22222222-2222-2222-2222-222222222222"}
    ]
  }
]
```

- `caller` is exactly `control-plane-service` or `knowledge-base-service`.
- `purpose` is exactly `agent-ownership-check`. This registry grants no agent
  management, managed execution or Business effect capability.
- Principal and version identifiers use bounded identifiers. Tokens use the
  existing managed syntax: 32–256 ASCII letters, digits, `.`, `_`, `~` or `-`.
- `current` is required, `previous` is optional; there are never more than two
  versions per stable principal. Version identifiers must differ. Each version
  and the whole principal can be revoked. Boolean flags require actual booleans.
- Use either 1–64 exact owner/agent pair grants or `all_tenants=true` with no pair
  grants. Separate owner and agent allowlists are not supported. A multi-tenant
  CP/KB workload may legitimately need explicit `all_tenants=true`; this grants
  only an ownership predicate across tenants, not tenant impersonation rights.
  Its credential cannot constrain a compromised caller to tenants absent from
  its configured scope. Prefer exact grants where provisioning supports them.
- Unknown fields, malformed values, duplicate JSON keys, duplicate IDs, duplicate
  grants and credential reuse across principals/versions are rejected, including
  revoked versions. Tokens must also differ from legacy internal, chat, managed
  execution and Business Conversation authority credentials. Startup errors
  contain fixed text and never render the registry or rejected input.

The registry is excluded from inherited MCP child environments, including
case variants and explicit credential mappings. This exclusion does not provide
an OS sandbox against a malicious same-UID child. Existing broader legacy MCP
environment inheritance remains outside P1a.

## Rotation and revocation

Keep one principal and its scope stable. First configure the receiver to accept
the new current and bounded previous version. Only after all receiver replicas
have that configuration may callers switch. Confirm every caller replica and
the in-flight overlap window, then revoke/remove the previous version and verify
that it gets 401. Revoking the principal disables both versions. Revocation is
configuration-driven; existing processes cache settings and do not hot-reload
environment updates. Every receiver replica must reload before revocation is
effective everywhere. This PR makes no claim of immediate distributed revoke.

Do not roll back to an older image while callers depend on this registry.
Retain a compatible receiver image/configuration. Do not restore broad legacy
admission as an automatic error or rollback response.

## Following P1b prerequisites (not implemented here)

1. Deploy a compatible P1a receiver to every AI replica with enforcement still
   disabled; verify the image/SHA and exact internal contract on synthetic data.
2. Provision a distinct KB→AI ownership credential and separate CP→AI ownership
   credential through a separately approved change. Keep management, browser JWT,
   chat, managed execution, Business RSA/mTLS and provider credentials separate.
   Inventory all consumers, including databases, init containers and jobs; do not
   expose the new values through omnibus Secrets or MCP environments. KB must
   not receive gateway or user-assertion authority.
3. Configure receiver-owned exact pairs or explicitly approve the multi-tenant
   lookup scope. Do not infer these grants from caller `X-User-Id` / role headers.
4. In a separate KB PR replace the generic AI owner GET with the POST above;
   preserve the owner/agent pair and map 204/404 precisely. Treat 401, malformed
   responses and transport failures as verification failure, never ownership
   success. Do not send the management token as a fallback.
5. Migrate CP separately before enabling strict ownership enforcement: its
   existing client still uses `INTERNAL_API_TOKEN`. Verify all ownership callers,
   tenant A/B positives/negatives, wrong-purpose denial, rotation/revoke and
   KB create/index/retrieval behavior. Then explicitly approve strict enforcement.

Generic owner `/v1/*` workload admission remains the separate P1c task. Such
routes still trust their existing identity headers; an ownership credential does
not fix that pre-existing route-level gap. Do not claim that P1a alone makes
generic owner CRUD deny a KB workload. That acceptance criterion requires P1c.
Infrastructure isolation and multi-replica rollout checks remain separate gates.
Goal5 permits, revisions, operation ownership and UNKNOWN reconciliation remain
authoritative and are not replaced by an ownership principal.
