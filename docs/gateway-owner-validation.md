# P1c AI receiver validation

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
