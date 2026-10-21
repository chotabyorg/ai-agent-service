# Handoff Telegram cross-service acceptance

This opt-in suite starts the candidate AI, Conversation, Control Plane, and
Telegram services as real local HTTP processes. It uses four fresh PostgreSQL
16 databases and local deterministic model, Telegram, and User-service peers.
No service database, `.env`, hosted endpoint, real Telegram credential, or
external provider is used.

Run from the isolated AI candidate. Set `AI_VENV`, `CONVERSATION_VENV`,
`CONTROL_PLANE_VENV`, and `TELEGRAM_VENV` to their separate native venv folders;
set `CONVERSATION_ROOT`, `CONTROL_PLANE_ROOT`, and `TELEGRAM_ROOT` to the clean
candidate checkouts. `PG_BIN` must point to PostgreSQL 16's bin directory.
`ADMIN_INPUT` is the existing private admin JSON and `PRIVATE_CONFIG` is a new
file inside a task-owned private directory. This creates
four random roles and databases on the task-owned PostgreSQL 16 server; the
admin input and generated configuration stay private with mode `0600`:

```sh
"$AI_VENV/bin/python" \
  tests/handoff_telegram_e2e/provision.py create \
  --ai-root "$PWD" \
  --conversation-root "$CONVERSATION_ROOT" \
  --control-plane-root "$CONTROL_PLANE_ROOT" \
  --telegram-root "$TELEGRAM_ROOT" \
  --ai-python "$AI_VENV/bin/python" \
  --conversation-python "$CONVERSATION_VENV/bin/python" \
  --control-plane-python "$CONTROL_PLANE_VENV/bin/python" \
  --telegram-python "$TELEGRAM_VENV/bin/python" \
  --pg-bin "$PG_BIN" \
  --admin-input "$ADMIN_INPUT" \
  --output "$PRIVATE_CONFIG"
```

Run from this AI candidate:

```sh
HANDOFF_E2E_PRIVATE_CONFIG="$PRIVATE_CONFIG" \
  "$AI_VENV/bin/python" \
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
"$AI_VENV/bin/python" \
  tests/handoff_telegram_e2e/provision.py drop \
  --config "$PRIVATE_CONFIG" \
  --pg-bin "$PG_BIN" \
  --admin-input "$ADMIN_INPUT"
```

The evidence file records which process-backed scenarios ran. It distinguishes
the local Telegram Bot API peer from Telegram's hosted service and records any
assertions that could not pass. It does not claim production readiness.
