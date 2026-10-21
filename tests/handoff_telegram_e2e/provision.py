"""Provision only a named, private family of loopback PostgreSQL test DBs."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import uuid
from pathlib import Path

AI_ROOT = Path(__file__).resolve().parents[2]


def _private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)


def _admin_input(path: Path) -> dict:
    if path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("PostgreSQL admin input must have mode 0600")
    value = json.loads(path.read_text())
    port = int(value["port"])
    user = value.get("admin_user", value.get("user"))
    password = value.get("admin_password", value.get("password"))
    if not user or not password or not 1 <= port <= 65535:
        raise ValueError("Private PostgreSQL admin input is incomplete")
    return {"admin_user": user, "admin_password": password, "port": port}


def _psql(admin: dict, pg_bin: Path, statement: str) -> str:
    env = {"PATH": f"{pg_bin}:/usr/bin:/bin", "PGPASSWORD": admin["admin_password"],
           "PGSSLMODE": "disable", "PGCONNECT_TIMEOUT": "5"}
    result = subprocess.run(
        [str(pg_bin / "psql"), "-X", "-w", "-v", "ON_ERROR_STOP=1", "-h",
         "127.0.0.1", "-p", str(admin["port"]), "-U", admin["admin_user"],
         "-d", "postgres", "-q", "-t", "-A"],
        input=statement, text=True, capture_output=True, env=env, timeout=20,
    )
    if result.returncode:
        raise RuntimeError("Local disposable PostgreSQL operation failed")
    return result.stdout.strip()


def _validate_paths(value: dict) -> None:
    keys = ("ai_root", "conversation_root", "control_plane_root", "telegram_root",
            "ai_python", "conversation_python", "control_plane_python",
            "telegram_python", "pg_bin")
    for key in keys:
        if not Path(value.get(key, "")).is_absolute() or not Path(value[key]).exists():
            raise ValueError("An explicit candidate path or runtime is missing")
    if Path(value["ai_root"]).resolve() != AI_ROOT.resolve():
        raise ValueError("AI candidate must be this isolated worktree")
    required = (("ai_root", "README.md"), ("conversation_root", "README.md"),
                ("control_plane_root", "README.md"), ("telegram_root", "README.md"))
    for root, file in required:
        candidate = Path(value[root]).resolve()
        if not (candidate / file).is_file() or (candidate / ".env").exists():
            raise ValueError("Candidate checkout is invalid or contains a .env file")
    if not (Path(value["pg_bin"]) / "psql").is_file():
        raise ValueError("Explicit PostgreSQL 16 client directory is invalid")


def create(args) -> None:
    admin = _admin_input(Path(args.admin_input).resolve())
    pg_bin = Path(args.pg_bin).resolve()
    fields = {
        "ai_root": args.ai_root, "conversation_root": args.conversation_root,
        "control_plane_root": args.control_plane_root, "telegram_root": args.telegram_root,
        "ai_python": args.ai_python, "conversation_python": args.conversation_python,
        "control_plane_python": args.control_plane_python, "telegram_python": args.telegram_python,
        "pg_bin": str(pg_bin), "disposable": "yes",
        "suite": "handoff-telegram-e2e", "port": admin["port"],
    }
    _validate_paths(fields)
    suffix = uuid.uuid4().hex[:12]
    resources = {}
    created = []
    try:
        for kind in ("ai", "conversation", "control_plane", "provider"):
            role = f"hte2e_{kind}_{suffix}"
            database = f"handoff_e2e_{kind}_test_{suffix}"
            password = secrets.token_urlsafe(32)
            if not re.fullmatch(r"[a-z0-9_]+", role + database):
                raise AssertionError("Generated PostgreSQL identity is unsafe")
            sql = (f"CREATE ROLE {role} LOGIN PASSWORD '{password}';\n"
                   f"CREATE DATABASE {database} OWNER {role} ENCODING 'UTF8' "
                   "LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;\n")
            _psql(admin, pg_bin, sql)
            created.append((database, role))
            resources[kind] = {
                "role": role, "database": database, "password": password,
                "url": (f"postgresql+asyncpg://{role}:{password}@127.0.0.1:"
                        f"{admin['port']}/{database}"),
            }
        fields.update(databases=resources)
        _private_json(Path(args.output).resolve(), fields)
    except BaseException:
        for database, role in reversed(created):
            try:
                _psql(admin, pg_bin, f"DROP DATABASE IF EXISTS {database};\n"
                      f"DROP ROLE IF EXISTS {role};\n")
            except Exception:
                pass
        raise
    safe = {kind: value["database"] for kind, value in resources.items()}
    print(json.dumps({"private_config": str(Path(args.output).resolve()),
                      "databases": safe}, sort_keys=True))


def drop(args) -> None:
    path = Path(args.config).resolve()
    if path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("Private Handoff E2E config must have mode 0600")
    config = json.loads(path.read_text())
    if config.get("suite") != "handoff-telegram-e2e" or config.get("disposable") != "yes":
        raise ValueError("Refusing to remove databases not owned by this test suite")
    admin = _admin_input(Path(args.admin_input).resolve())
    pg_bin = Path(args.pg_bin).resolve()
    if int(config["port"]) != admin["port"]:
        raise ValueError("PostgreSQL endpoint changed; cleanup refused")
    for kind in ("provider", "control_plane", "conversation", "ai"):
        value = config["databases"][kind]
        if not re.fullmatch(rf"handoff_e2e_{kind}_test_[a-f0-9]{{12}}", value["database"]):
            raise ValueError("Unexpected disposable database identity")
        if not re.fullmatch(rf"hte2e_{kind}_[a-f0-9]{{12}}", value["role"]):
            raise ValueError("Unexpected disposable role identity")
        _psql(admin, pg_bin, f"DROP DATABASE IF EXISTS {value['database']};\n"
              f"DROP ROLE IF EXISTS {value['role']};\n")
    path.unlink()
    print("Removed only this Handoff E2E disposable database family.")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("create")
    for key in ("ai-root", "conversation-root", "control-plane-root", "telegram-root",
                "ai-python", "conversation-python", "control-plane-python",
                "telegram-python", "pg-bin", "admin-input", "output"):
        make.add_argument("--" + key, required=True)
    remove = sub.add_parser("drop")
    remove.add_argument("--config", required=True)
    remove.add_argument("--pg-bin", required=True)
    remove.add_argument("--admin-input", required=True)
    args = parser.parse_args()
    if args.command == "create":
        create(args)
    else:
        drop(args)


if __name__ == "__main__":
    main()
