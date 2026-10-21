# P1c validation — CI stabilization, 21 October 2026

Application snapshots: updated parent `b7800468d85c80a85ef46b717dbc37991aa7432d`,
P1c `46d15d3e45e694010d102fd343c522054fa8947f`, sequential current-main candidate
`8f2ec507cfe25b27307b6fbf78032dbcd511665b`. The last two have identical trees.
Current main `f570d1e3c6e23075b899379e0e23895df512947f` is integrated, including
all Voice/completion profiles, Business changes and SQL observers. Of its 26
changed files since original main, 24 are byte-identical; only `core/config.py`
and `core/managed_security.py` merge the existing auth additions. No new
production auth/Business/Voice logic or dependency/workflow changes fix this CI.

## Inventory root cause and strict regression seam

Original CI checked out `7dfb92d` (tree-identical to P1c `6a3e71c`). With FastAPI
0.156.2, direct `app.routes`/`APIRoute` iteration sees none of the included
routers. Since FastAPI 0.137, inclusion retains a route tree. This is test
introspection failure; the real request admission matrix still executes guards.

The fix uses the public `iter_route_contexts()` to inspect effective paths and
inherited dependencies. The prior direct route traversal remains for old 0.135.1.
The exact inventory equality and **21 guarded operations** assertion are retained;
hidden routes and duplicate registrations remain visible. The separate-auth
inventory now must equal six internal/chat operations, preventing a vacuous
negative check. No production dependency override, guard bypass or downgrade was
introduced. Public helper availability is tested; 0.137.0–0.137.1 predate it and
are not claimed compatible by this fallback.

Primary sources: [route tree change](https://fastapi.tiangolo.com/release-notes/#01370),
[public iterator](https://fastapi.tiangolo.com/release-notes/#01372),
[effective dependency composition](https://github.com/fastapi/fastapi/blob/0.156.2/fastapi/routing.py#L1491).

## Results; broad failures remain failures

| Check | Result |
| --- | --- |
| Original #141 native shard 0, 804 cases | 803 PASS / 1 inventory FAIL |
| Updated candidate, identical original shard 0 module cohort | 806 PASS / 1 inherited Goal5 FAIL; inventory PASS |
| Original #141 native shard 3, 612 cases | 610 PASS / 2 FAIL: Voice UNKNOWN and managed sentinel |
| Updated candidate, identical original shard 3 module cohort | 612 PASS / 1 same Voice FAIL; managed sentinel PASS |
| Updated P1a full suite | 2276 PASS / 0 FAIL / 0 SKIP |
| Current-main integrated full suite, 2575 cases | **2574 PASS / 1 FAIL / 0 SKIP** |
| Focused real PostgreSQL gate | **439 PASS / 0 FAIL / 0 SKIP** |
| Quiet serial complete observed-failure union, original/current main | 9 PASS each; owner inventory unavailable and recorded |
| Same union, updated P1a / integration | 10 PASS / 11 PASS; managed cache parameter adds one case |
| Full Goal5 recovery module, all four sources serially | 10 PASS on each source |
| Old local and original CI FastAPI dependency sets | Exact inventory and real owner reject/admit matrix PASS |
| Newer observed current-main CI set, FastAPI 0.156.4 | 383 PASS / 7 integration deselected across auth/startup/chat/bounds |
| Pinned P0a AI adapter on exact integration `8f2ec50` | 22 PASS / 1 EXPECTED SECURITY GAP (legacy owner mode) |
| Exact workflow lint/compilation | PASS |

The integrated full failure is
`test_current_booking_reschedule_context::test_create_move_cancel_uses_fresh_resource_and_one_completion_per_turn[VOICE]`.
It passes in the quiet serial union on all four sources. The broad Voice held
cycle failure observes UNKNOWN instead of DISPATCHING and also passes in these
serial runs; its prior/broad failures remain recorded. No timeout, expected
status, retry policy or Business assertion was changed. The precise trigger of
these broad Voice manifestations is not established by serial PASS.

The shard-0 Goal5 failure is `UnboundLocalError: plan` in `_send`: an own two-second
shield can cancel `mark_unknown` before assignment, bypass `except Exception`,
then suppress cancellation before the uninitialized variable is read. An
independent actual-method reproduction confirms it; source SHA-256 is identical
on `05d229c`, `076e723`, `6a3e71c`, `f570d1e` and `8f2ec50`. It occurs during test
setup before recovery read-only assertions. It masks typed UNKNOWN handling and
may leave a committed DISPATCHING operation awaiting recovery; it does not add a
provider dispatch/success fallback. Managed outer processing remains UNKNOWN.
The four serial Goal5 modules pass, which does not repair this inherited defect.
No Business change is made here.

[Full receipt](ci-stabilization/p1c-receipts.json) records every broad failure,
exact source/selector, unavailable baseline test, serial result and cleanup.
The 2575 collection is current main 2223 + existing P1a 52 + extra cache case 1 +
P1c 299. No cases were dropped. Overlapping focused/independent runs must not be
summed. Independent review reran auth on old/current dependency sets and approved
spec/correctness plus supplemental security within the scoped delta; it is not
whole-platform or deployment acceptance.

## Real MCP child environment

A self-contained synthetic FastMCP server was launched through production
`MCPManager.initialize()/call_tool()/close()` and real MCP 1.26 stdio transport on
Python 3.11.14 and 3.12.12. Cold valid managed/ownership/gateway registries parse;
the actual child excludes all their case variants and registry/token values,
reserved gateway/proof tokens and Business Conversation credentials. B24 mapping
survives. Real PID termination, stop marker and manager cleanup all pass. The
launcher/child have no source PYTHONPATH; the receipt checks exact clean source
SHA and source hashes before/after and removes temporary driver/server copies.
See [3.12 receipt](ci-stabilization/mcp-child-env-ci312.json) and
[3.11 receipt](ci-stabilization/mcp-child-env-ci311.json).

**Legacy `CHAT_S2S_TOKEN` and `INTERNAL_API_TOKEN` still reach the child.** This is
observed existing broader env inheritance; a compromised legacy MCP child can
retain those broad capabilities. This PR does not implement a general env
allowlist or claim all service secrets are excluded. `acquire_owned()` shares
the parameter builder by source inspection; the probe is not a standalone full
managed-turn lifecycle or OS sandbox proof.

## CI, merge order and rollout boundary

[Original CI versions](ci-stabilization/p1c-original-ci-constraints.txt) and
[newer current-main CI versions](ci-stabilization/current-main-ci-constraints.txt)
are reproduction inventories only. Requirements remain unchanged. Local
Python/OS patch versions differ from GitHub; new-head remote checks must be read
after push, and no local PASS is remote CI evidence.

Merge #138 first; reconcile/retarget #141 with the actual resulting main and
repeat affected checks if SHA/tree changes. Original heads/history are preserved
through ordinary merges/fast-forward feature updates. Default legacy and strict
off remain, so production SEC002 enforcement is not accepted. Actual gateway
route integration, scoped credentials on every replica, caller migration,
rotation/revocation/rollback acceptance and the inherited limits remain separate
gates. No merge, deployment, P2 or infrastructure change is performed here.
The current contract points to the later Lua proof instead of treating the
superseded Martian/CRITICAL candidate as rollout approval.

## Historical implementation validation — 20–21 October

Validation completed across 20–21 October 2026 (Europe/Minsk). This is a dormant
receiver change; no environment enforcement, gateway configuration promotion,
GitOps/Secret/network change, deployment or production probe was performed.

## Sources and scope

- Exact unchanged P1a base: `076e723af7b4771f1633bd9029bc833b89072d96`.
- P1c branch: `security/p1c-ai-owner-gateway`, stacked PR base
  `security/p1a-internal-ownership-purpose`.
- Main compatibility source: `e44a3b0977523ae7ab73fef9a6e57c80c975a7d2` merged
  with exact P1a using `--no-commit`; no merge conflicts. The P1c delta was then
  applied in that separate disposable checkout. Original main/P1a were not edited.
- Existing AI Python 3.12 environment and a fresh loopback-only, uniquely named
  disposable PostgreSQL 16 container per run/shard. Synthetic credentials,
  tmpfs storage, disabled dotenv loading, explicit `TEST_DATABASE_DISPOSABLE=yes`
  and guarded `test_ai_*` database names were used. No production DSN was used.

The exact 21-route cohort and rollout prerequisites are in
[gateway-owner-auth.md](gateway-owner-auth.md).

## Results

| Check | Result |
| --- | --- |
| New gateway admission module, including real PostgreSQL owner/admin CRUD | **299 PASS** |
| Fresh independent security review, including new tests and real DB owner/admin behavior | **APPROVE; 368 PASS** |
| Full exact P1a baseline | **2214 PASS / 5 FAIL**, 2219 tests |
| Full P1c candidate | **2512 PASS / 5 FAIL / 1 ERROR**, 2518 tests |
| Merged-main focused compatibility: new admission, P1a, main context changes, Voice and Goal5 | **461 PASS / 1 FAIL**; failure was a local disposable-PG connection timeout |
| Serial union of 11 observed failures on exact P1a | **10 PASS / 1 FAIL** |
| Same serial union on P1c | **10 PASS / 1 same FAIL** |
| Same serial union on merged main + P1a + P1c | **10 PASS / 1 same FAIL** |
| CI-equivalent changed-production and changed-test Ruff 0.13.2 checks | **PASS** |
| `git diff --check` | **PASS** |

The full suites are **not green**. The one serial failure common to all three
sources is:

`tests/test_five_booking_cycles.py::test_five_scoped_booking_cancel_cycles_keep_four_progressing_while_one_is_held[VOICE]`

It expects eight `SUCCEEDED` plus one `DISPATCHING` retained operation, but
observes eight `SUCCEEDED` plus one `UNKNOWN`. The assertion and Business code
were not modified or weakened. This is an inherited test/scenario failure,
not an expected security gap or a passing test. The Website variant passes in
all three serial runs.

The other ten failures/errors observed under the broad concurrent runs pass
in all three serial runs, including the merged-main lost-acknowledgment
`PERMISSION` case. They include disposable localhost-PG connection timeouts and
existing Voice/customer-reuse/context assertions. These observations do not
establish a P1c regression; they remain explicit limits of the broad-run result.

An additional focused baseline/candidate ordering reproduces the pre-existing
`test_managed_bounds.py::test_new_managed_secret_env_denylist_and_mapped_credentials`
failure: it installs a malformed managed-registry sentinel before constructing
`MCPManager` when the settings cache has been cleared. The independent reviewer
also reproduced it on P1a. It is not changed in this receiver PR.

Initial candidate shard collection recorded 2515 cases before the final three
duplicate-identity regressions were added. The actual run executes 2518 cases:
shard 1 has 632 versus its earlier 629, and the other shards retain their
counts. The final total is exactly 2219 unchanged baseline tests + 299 new
tests. Raw collection metadata is retained; no omitted cases or passing
full-suite result are inferred from the runner process exit code.

## Reproduction and receipts

The existing isolated runner `/private/tmp/chota-p1a-tests.py` creates and
removes its own random disposable PostgreSQL container. Run it with the
existing service Python interpreter, source checkout, a unique result label,
and pytest arguments. For the new suite the arguments are:

```text
-q tests/test_gateway_owner_auth.py
```

For the independent real DB case alone:

```text
-q tests/test_gateway_owner_auth.py::test_authenticated_owner_crud_keeps_tenant_and_admin_rules
```

Local receipts:

- `/private/tmp/chota-p1a-p1c-new-final.log`: final 299-case run.
- `/private/tmp/chota-p1a-p1c-main-compat.log`: merged-main focused run.
- `/private/tmp/chota-p1c-full-shards/run-summary.json`: all eight full shards;
  sibling logs, JUnit and result JSON preserve the failures and cleanup status.
- `/private/tmp/chota-p1c-full-shards/serial-nodeids.json`: exact eleven-node
  union; `serial-*.xml` and `serial-recheck-results.json` record all three
  consecutive rechecks.
- `/private/tmp/chota-p1c-full-shards/validation-summary.json`: count
  reconciliation and consolidated results.
- `/private/tmp/chota-p1c-source-receipt.json`: source identity checks against
  the combined checkout.

All full-shard and serial-run owned containers were cleaned. Synthetic
credential-input files were removed by the runners. The independent reviewer
used a separate disposable DB and did not share a shard database.

Ruff 0.13.2 was already installed for P1a at
`/private/tmp/chota-p1a-lint/bin/ruff`; no dependency was added. Commands match
the current `quality.yaml` selectors:

```text
/private/tmp/chota-p1a-lint/bin/ruff check --select E4,E7,E9,F api/gateway_owner.py core/config.py core/managed_security.py main.py
/private/tmp/chota-p1a-lint/bin/ruff check --select E9,F63,F7,F82 tests/test_gateway_owner_auth.py
```

Because P1c newly changes `main.py`, the current workflow now lints that file
and finds two inherited late-import `E402` warnings. Exact P1a reproduces both.
Narrow `noqa: E402` comments preserve the existing import/initialization order.
The comments are the only production-file delta after the main independent
review; AST equivalence with the tested merged-main copy was verified. This
validation note and documentation links are the remaining documentation-only
delta. A final incremental independent review covers those additions before
commit/push.

## Rollout remains separate

The receiver defaults to explicit legacy compatibility. Strict activation
still requires KB internal ownership mode, separate receiver credentials,
all 21 gateway method/route injections, a reviewed full runtime configuration,
safe logging for KrakenD's malformed-modifier path, staged positives/negatives,
rotation/rollback and final main integration. The disposable pinned-runtime
proof and this local receiver validation do not deploy or authorize those steps.
