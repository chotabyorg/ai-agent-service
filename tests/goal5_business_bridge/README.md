# Goal5 paired HTTP/PostgreSQL acceptance tests

This opt-in suite runs the real Conversation and AI HTTP services against three
separate disposable PostgreSQL 16 databases and a local fake provider. It does
not use Docker, `.env` files, hosted services, or production credentials.
Ordinary `pytest tests` collection omits `acceptance_http.py` unless the paired
runner configuration is explicitly selected; use the runner below to include
those process cases.

The operator supplies absolute paths to the two candidate checkouts, their
existing virtual-environment Python launchers, a native PostgreSQL 16 `bin`
directory, and a private JSON file containing the credentials for an already
running PostgreSQL 16 server bound to `127.0.0.1`. The admin input must have
mode `0600` and contain `admin_user`, `admin_password`, and `port`. The suite
creates its own random roles and UTF8 databases; no existing role or database
is changed. Keep both private input files outside version control.

From the AI service checkout, provision a private suite config:

```sh
python tests/goal5_business_bridge/provision.py create \
  --ai-root /absolute/path/to/ai-agent-service-checkout \
  --cs-root /absolute/path/to/conversation-service-checkout \
  --ai-python /absolute/path/to/ai-venv/bin/python \
  --cs-python /absolute/path/to/conversation-venv/bin/python \
  --pg-bin /absolute/path/to/postgresql-16/bin \
  --admin-input /absolute/path/to/private-postgres-admin.json \
  --output /absolute/path/to/private-goal5-config.json
```

Run the paired acceptance suite with the AI virtual environment Python:

```sh
GOAL5_PRIVATE_CONFIG=/absolute/path/to/private-goal5-config.json \
  /absolute/path/to/ai-venv/bin/python \
  tests/goal5_business_bridge/run.py
```

To emit one sanitized stage trace for the synthetic `booking.create` flow, set
`GOAL5_LATENCY_EVIDENCE` to a new absolute JSON path in an existing directory
when invoking the same runner. The trace contains monotonic stage timestamps,
durations, endpoint counts, and the fake provider's durable acceptance time; it
contains no request or response bodies. Model timing is from the deterministic
local fake and does not represent a real model. The readiness point is the real
Conversation turn-select and claim boundary; this probe does not include a
configured settling quiet window. The paired stack does not start
Conversation's outbound Telegram delivery worker, so channel-send authorization
and Telegram acceptance are reported as not exercised.

The runner validates that the three database URLs are distinct numeric
loopback endpoints, verifies PostgreSQL 16 and UTF8, clears inherited
environment variables, and installs a loopback-only network guard before
loading service code. Migration subprocesses and service processes receive
only explicit paths, disposable database settings, and local test configuration.
The harness itself cannot select a non-loopback destination.

The separate ordinary-AI native test wrapper allows an additional loopback
destination only when that same test process first bound the exact loopback
address and port. This supports tests that create ephemeral TLS listeners.
The paired runner does not enable that exception: its outbound network access
remains limited to its explicitly configured PostgreSQL, service, and fake
provider endpoints.

After reviewing the test result, remove only this suite's owned roles and
databases:

```sh
python tests/goal5_business_bridge/provision.py drop \
  --config /absolute/path/to/private-goal5-config.json \
  --pg-bin /absolute/path/to/postgresql-16/bin \
  --admin-input /absolute/path/to/private-postgres-admin.json
```

Provisioning and cleanup validate the suite marker, database/role naming
patterns, port, and private-file permissions before acting. Do not point this
suite at shared or production database credentials.
