# Goal3 paired HTTP/process author harness

This opt-in package runs the actual unchanged Conversation service, the candidate
AI managed route/shared core/business ledger and a separate HTTP fake booking and
model provider. Each owns a different disposable PostgreSQL16 database. Provider
receipts, native keys, logical objects and request counters are durable SQL rows;
the provider can restart independently. No production module imports this package.

`acceptance_http.py` is deliberately outside default `test_*.py` discovery. Run
through `run.py`, which installs the exact numeric-loopback guard before importing
pytest, client libraries, runtime settings or private configuration. Direct pytest
is useful for development but does not establish that pre-import guard property.
The runner discards inherited DSNs, service secrets and proxy variables.

## Explicit fresh inputs

The root-owned task container is `chota-goal3-postgres`, bound only at
`127.0.0.1:55438`. Check that exact existing container before provisioning. Do not
use another container or a runtime database. Every database connection verifies
PostgreSQL16. The provisioner generates three new names of the form
`goal3_test_harness_{ai,cs,provider}_<fresh suffix>`; it refuses an existing config.

Use the candidate checkout as the working directory. Supply the test-only database
password through `GOAL3_TEST_POSTGRES_PASSWORD` from the task's private local
configuration; its value is never a command argument or output. The examples below
contain paths and variable names, with no private values:

```sh
AI_GOAL3_PYTHON=/Users/vladislavsologubov/chota/backend/ai-agent-service/.venv/bin/python3.12
CS_GOAL3_PYTHON=/private/tmp/chota-conversation-goal1/venv/bin/python3.11
CS_GOAL3_ROOT=/Users/vladislavsologubov/chota/.goal3/worktrees/conversation
GOAL3_PRIVATE_CONFIG=/private/tmp/goal3-fresh-verifier-private.json
export GOAL3_TEST_POSTGRES_PASSWORD GOAL3_PRIVATE_CONFIG
"$AI_GOAL3_PYTHON" -m tests.goal3_business_cross_service.provision create \
  --config "$GOAL3_PRIVATE_CONFIG" --cs-root "$CS_GOAL3_ROOT" \
  --ai-python "$AI_GOAL3_PYTHON" --cs-python "$CS_GOAL3_PYTHON"
"$AI_GOAL3_PYTHON" -m tests.goal3_business_cross_service.run -x \
  --basetemp=/private/tmp/goal3-fresh-verifier-evidence
"$AI_GOAL3_PYTHON" -m tests.goal3_business_cross_service.provision drop \
  --config "$GOAL3_PRIVATE_CONFIG"
```

Choose new config/evidence paths for every independent run. The test fixture uses
real Alembic upgrades in both candidate runtimes; it never uses `create_all`, SQLite,
a production fallback, or skip-on-missing-input behavior. `GOAL3_PRIVATE_CONFIG`
must be an absolute mode0600 JSON file containing `disposable: "yes"`, exact
`ai_root`, `cs_root`, existing absolute `ai_python`, `cs_python`, and three explicit
`databases` URLs (`postgresql+asyncpg`, numeric127.0.0.1, port55438). The provisioner
creates that file without displaying credentials. Child environments contain only
whitelisted runtime inputs and newly generated synthetic tokens/Fernet material.
The tests refuse candidate `.env` files. Missing/incorrect paired inputs fail.

Listeners are prebound on numeric loopback and passed by descriptor to owned child
processes. Ports are added individually to the guard; there is no wildcard
loopback permission. AI permits only PG and the fake provider; Conversation permits
only PG and that AI listener; provider/migration children permit only PG. All guard
instances reject external connect, connect_ex and DNS in real self-checks.

Docker access and binding local listener sockets require the host's sandbox
escalation. The package itself performs no Docker operation and creates no other
container. Cleanup stops only retained Popen children, closes their listeners and
clients, and releases test barriers, even if evidence writing fails. The provisioner
only drops its validated per-run family; it never terminates DB sessions or uses
DROP FORCE. Stop owned processes before dropping databases.

## Behavior and evidence

| Group | Actual proof |
| --- | --- |
| A/B/B2/C/D/E | Real reservation/permission/finalization methods return their own COMMIT before test bootstrap signals. SIGKILL/restart retains original UUID/Turn, receipts and provider counts. A/B safely first-dispatch; B2 remains UNKNOWN with zero POST; C/D resolve by READ; E observes committed receipt without redispatch. |
| F | Fresh PG observer sees CS finalizer blocked before terminal COMMIT; owned CS is killed/restarted and same Turn reclaimed at epoch2. Protected scoped AI evidence is passed to existing CS fail with winning proof before marker-only execute recovery; stale proof rejects and exact replay succeeds. |
| Provider modes | LOOKUP positive and definitive no-effect GET evidence; missing lookup retains UNKNOWN; NATIVE reuses the original UUID/key/payload/issued time with two POST attempts and one object; BLIND persists UNKNOWN across repeated independent process restarts. Bounded native horizon refuses unsafe replay. |
| Effect/result separation | Actual CS→AI→provider success survives model failure, invalid draft and error after real native cleanup. Draft is never promoted to Message. Later deliberate T2 gets its own action identity while T1 evidence survives. Actual CS receives mixed SUCCEEDED/UNKNOWN/null refs and no success draft. |
| Identity/bounds/auth | Exact frozen replay reuses IDs; changed payload/order/count conflicts;32refs allowed and33actions rejected. Evidence authenticates full scope, rejects legacy token/extra fields/foreign IDs, returns no-store, and produces no model/provider writes. Exact later selected customer confirmation dispatches the original proposal; replay keeps one effect and real TTL expiry denies a stale proposal. |
| §46 overlap | First reservation, same-operation exact replay, competing permission, original receipt finalization plus exact receipt replay, reconciliation, canonical confirmation and authoritative generation commit all have observed independent PG waiters. Queued lock dependency paths are traced to the owned blocker. |
| AR1: pre-reserved live/expired/UNKNOWN | Two real CS Turns in one scope are pre-reserved before any permission; consent is selected from two further real CS inputs. Actual queued transactions block after fresh scope lock; second plan/consent/null refs/attempt0 remain unchanged. Real DB-clock lease expiration is observed before normalization. |
| AR1: R/D and normalization | Both controlled R-first and D-first real PG waits; fresh reservation after normalization sees UNKNOWN after COMMIT. Reverse first-permission winners prove no age priority. |
| AR1: backstop/release | Direct second attempted-state SQL transition violates the real partial unique index. Positive and negative authoritative terminal COMMIT release the slot while a queued contender waits; the same reserved UUID then competes under current authority. |
| AR1: sequential/residual/native | RESERVED/live/UNKNOWN predecessors stop the successor. Another Turn wins between actions and blocks the original residual until authoritative resolution. Native exception belongs only to the original UNKNOWN row, never its live replay or another RESERVED row. |
| AR1: isolation/current permission | NOWAIT probes acquire agent/binding/scope/plan/operation locks while provider HTTP remains gated; independent scope progresses. Changing current mutation permission does not rewrite blocked consent; after original resolution the stale plan is denied with genuine local no-dispatch evidence. |

Concurrency assertions use independent real connections and fresh observer PIDs,
`pg_blocking_pids`, transaction start and Lock wait metadata. `gather` is used only
to join killed request tasks, never as proof of overlap. Test-only authenticated
worker routes call the actual runtime/repository/adapter under trusted fixture
grants and explicit ManagedRuntime owners. They are control seams for reservation,
permission, recovery and commit barriers; no ledger, receipt or result is mocked.

Each case's private pytest directory retains `case-evidence.json` with request
counts, logical-object count, safe identity summaries, actual process events, phase
signals, PG wait proofs and provider peer EOF observations. Runtime configs/logs and
synthetic inputs remain mode0600; do not publish those files. Child logs are checked
for the synthetic token, password, model key, Fernet and provider-error sentinels.
There are no Conversation business tables, Message promotion, Session/Redis store
calls, MCP acquisition or DeliveryAttempt/Reconciliation mutations in this matrix.

The existing immutable Conversation API cannot enrich an already-terminal result
whose refs are empty. A separate actual marker-only recovery test asserts that409
and retained authoritative AI evidence. F's same-T1 attachment is deliberately
performed before that terminalization. The harness claims no automatic recovery
coordinator, scheduler, real provider compatibility, deployment or final PASS.

## Source-fix R3 matrix

The public repository admission seam takes a closed zero-argument async facts
resolver. Test-only workers use that same public P → released sessions → FRESH →
decisive NOWAIT bridge. Passing a previously resolved `MaterialFacts` value is a
negative test. Scope-lock traces distinguish wait-capable P from decisive locks;
actual PostgreSQL observer records never label NOWAIT contention as a wait.

The fake provider has independently durable mutable `fake_facts`, original
`fake_objects`/`fake_receipts`, persistent key authority rows, and an honest
expiring `fake_keys` cache. Acceptance acquires the facts and key authorities,
reloads current facts, then samples `clock_timestamp()`. It compares complete
expected values/revisions and the original absolute deadline before cache eviction
or a new object. Counts distinguish POST bytes received, ACCEPT decisions,
MUTATION object creation, REJECT, LOOKUP and distinct durable object IDs.

Guarded BOUNDED profiles reject every unenveloped path and carry the unchanged
deadline on FIRST, native and displaced attempts. Local expiration avoidance is
only an optimization; the transport/provider-wait cases pass that local check and
still require the authoritative late rejection. FENCED profiles receive no native
replay token. An explicit legacy test profile retains an unenveloped old FIRST
path and the exact conservative original catalog mapping; it is FENCED precisely
because that path cannot gain retroactive acceptance protection. Receipt reads
can establish its original effect later without another command.

Original definitive no-effect evidence is a permanent fence under its complete
receipt binding, including old unenveloped attempts after key eviction. A later
negative attempt cannot disprove an existing durable B1 receipt. Separate legacy
proofs also demonstrate that a positive original does not silently become durable
success dedupe: an explicitly unsafe unenveloped send after honest cache expiry
can create a distinct second object, so runtime replay remains FENCED.

Additional cases exercise no-tool UNKNOWN/FAILED/pending/incomplete/mixed and
32-ref plans, all-success positives, current-no-plan/prior-U, current success with
other-Turn U, early inactive/key failures, actual final-read lock waits and final
read loss, including cleanup success/failure with both no selected plan and
retained successful receipts. Every original ref remains correlated to its selected plan. Stale
runtime snapshots consume committed success at every ordinal of a three-action
plan and all-terminal replay; residuals independently reconsider U, facts,
generation, genuine FAILED predecessors and real original expiry. The ordinary
suite separately exercises all32 ordinals. These are authored cases, not an
independent review or a final-verifier result.
