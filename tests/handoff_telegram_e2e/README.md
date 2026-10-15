# Handoff Telegram cross-service acceptance

This opt-in suite starts the candidate AI, Conversation, Control Plane, and
Telegram services as real local HTTP processes. It uses four fresh PostgreSQL
16 databases and local deterministic model, Telegram, and User-service peers.
No service database, `.env`, hosted endpoint, real Telegram credential, or
external provider is used.

Run from `/Users/vladislavsologubov/chota/.handoff-telegram/ai`. This creates
four random roles and databases on the task-owned PostgreSQL 16 server; the
admin input and generated configuration stay private with mode `0600`:

```sh
"/Users/vladislavsologubov/chota/backend/ai-agent-service/.venv/bin/python" \
  tests/handoff_telegram_e2e/provision.py create \
  --ai-root "$PWD" \
  --conversation-root /Users/vladislavsologubov/chota/.handoff-telegram/conversation \
  --control-plane-root /Users/vladislavsologubov/chota/.handoff-telegram/control-plane \
  --telegram-root /Users/vladislavsologubov/chota/.handoff-telegram/telegram \
  --ai-python /Users/vladislavsologubov/chota/backend/ai-agent-service/.venv/bin/python \
  --conversation-python /Users/vladislavsologubov/chota/backend/conversation-service/.venv/bin/python \
  --control-plane-python /Users/vladislavsologubov/chota/backend/control-plane-service/.venv/bin/python \
  --telegram-python /Users/vladislavsologubov/chota/backend/telegram-gateway-service/.venv/bin/python \
  --pg-bin /opt/homebrew/Cellar/libpq/17.0_1/bin \
  --admin-input /private/tmp/chota-handoff-docker/admin.json \
  --output /private/tmp/handoff-e2e-final-private.json
```

Run from this AI candidate:

```sh
HANDOFF_E2E_PRIVATE_CONFIG=/private/tmp/handoff-e2e-final-private.json \
  /Users/vladislavsologubov/chota/backend/ai-agent-service/.venv/bin/python \
  tests/handoff_telegram_e2e/run.py
```

The runner validates every numeric-loopback database URL and candidate path,
installs a loopback-only guard before importing application code, applies only
the four disposable service migrations, and writes process logs with private
permissions. The suite is outside normal pytest discovery and never clears a
database unless its generated name and private config prove it belongs to this
run.

After review, remove only this suite's generated database roles and databases:

```sh
"/Users/vladislavsologubov/chota/backend/ai-agent-service/.venv/bin/python" \
  tests/handoff_telegram_e2e/provision.py drop \
  --config /private/tmp/handoff-e2e-final-private.json \
  --pg-bin /opt/homebrew/Cellar/libpq/17.0_1/bin \
  --admin-input /private/tmp/chota-handoff-docker/admin.json
```

The evidence file records which process-backed scenarios ran. It distinguishes
the local Telegram Bot API peer from Telegram's hosted service and records any
assertions that could not pass. It does not claim production readiness.
