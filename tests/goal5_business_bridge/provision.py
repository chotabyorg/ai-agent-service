"""Create three fresh UTF-8 databases on the task-owned native PostgreSQL16."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import uuid
from pathlib import Path

AI_ROOT = Path(__file__).resolve().parents[2]


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))
    path.chmod(0o600)


def psql(admin: dict, sql: str, *, pg_bin: Path, database: str = "postgres", quiet: bool = True):
    env = {
        "PATH": f"{pg_bin}:/usr/bin:/bin",
        "PGPASSWORD": admin["admin_password"],
        "PGSSLMODE": "disable",
        "PGCONNECT_TIMEOUT": "5",
    }
    args = [str(pg_bin / "psql"), "-X", "-w", "-v", "ON_ERROR_STOP=1", "-h",
            "127.0.0.1", "-p", str(int(admin["port"])), "-U", admin["admin_user"],
            "-d", database]
    if quiet:
        args.extend(["-q", "-t", "-A"])
    result = subprocess.run(args, input=sql, text=True, capture_output=True, env=env, timeout=20)
    if result.returncode:
        raise RuntimeError("Native PostgreSQL provisioning failed: " + result.stderr.splitlines()[-1])
    return result.stdout.strip()


def validate_inputs(value):
    if value.get("disposable") != "yes":
        raise ValueError("Private config must acknowledge disposable local databases")
    for key in ("ai_root", "cs_root", "ai_python", "cs_python", "pg_bin"):
        if not Path(value.get(key, "")).is_absolute() or not Path(value[key]).exists():
            raise ValueError("Explicit absolute service candidate/runtime is required")
    if Path(value["ai_root"]).resolve() != AI_ROOT.resolve():
        raise ValueError("AI root must be this isolated candidate")
    if not (Path(value["cs_root"]) / "README.md").is_file():
        raise ValueError("Conversation root must be an existing service checkout")
    if not (Path(value["pg_bin"]) / "psql").is_file():
        raise ValueError("Explicit native PostgreSQL bin directory is invalid")
    if (Path(value["ai_root"]) / ".env").exists() or (Path(value["cs_root"]) / ".env").exists():
        raise ValueError("Candidate .env files are forbidden")
    return value


def create(args):
    admin_path = Path(args.admin_input).resolve()
    if admin_path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("Native PostgreSQL admin input must have mode 0600")
    admin = json.loads(admin_path.read_text())
    pg_bin = Path(args.pg_bin).resolve()
    suffix = uuid.uuid4().hex[:12]
    resources = {}
    for kind in ("ai", "conversation", "provider"):
        role = f"g5bridge_{kind}_{suffix}"
        database = f"goal5_bridge_{kind}_test_{suffix}"
        password = secrets.token_urlsafe(32)
        if not re.fullmatch(r"[a-z0-9_]+", role + database):
            raise AssertionError("Generated identifier was not safe")
        sql = (
            f"CREATE ROLE {role} LOGIN PASSWORD '{password}';\n"
            f"CREATE DATABASE {database} OWNER {role} ENCODING 'UTF8' "
            "LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;\n"
        )
        psql(admin, sql, pg_bin=pg_bin)
        resources[kind] = {
            "role": role,
            "database": database,
            "password": password,
            "url": f"postgresql+asyncpg://{role}:{password}@127.0.0.1:{admin['port']}/{database}",
        }
    config = {
        "disposable": "yes",
        "suite": "goal5-business-bridge",
        "ai_root": str(Path(args.ai_root).resolve()),
        "cs_root": str(Path(args.cs_root).resolve()),
        # Preserve venv launcher paths; resolving a symlink loses its venv prefix.
        "ai_python": str(Path(args.ai_python).absolute()),
        "cs_python": str(Path(args.cs_python).absolute()),
        "pg_bin": str(pg_bin),
        "port": int(admin["port"]),
        "databases": resources,
    }
    validate_inputs(config)
    private_json(Path(args.output).resolve(), config)
    safe = {kind: {"role": value["role"], "database": value["database"]}
            for kind, value in resources.items()}
    print(json.dumps({"private_config": str(Path(args.output).resolve()),
                      "databases": safe}, sort_keys=True))


def drop(args):
    config_path = Path(args.config).resolve()
    if config_path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("Private paired config must have mode 0600")
    config = json.loads(config_path.read_text())
    if config.get("suite") != "goal5-business-bridge" or config.get("disposable") != "yes":
        raise ValueError("Refusing to drop databases not owned by this harness")
    admin_path = Path(args.admin_input).resolve()
    if admin_path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("Native PostgreSQL admin input must have mode 0600")
    admin = json.loads(admin_path.read_text())
    pg_bin = Path(args.pg_bin).resolve()
    if int(config["port"]) != int(admin["port"]):
        raise ValueError("PostgreSQL endpoint changed; refusing cleanup")
    for kind in ("provider", "conversation", "ai"):
        value = config["databases"][kind]
        if not re.fullmatch(rf"goal5_bridge_{kind}_test_[a-f0-9]{{12}}", value["database"]):
            raise ValueError("Unexpected database identity; refusing cleanup")
        if not re.fullmatch(rf"g5bridge_{kind}_[a-f0-9]{{12}}", value["role"]):
            raise ValueError("Unexpected role identity; refusing cleanup")
        psql(admin, f"DROP DATABASE {value['database']};\nDROP ROLE {value['role']};\n", pg_bin=pg_bin)
    config_path.unlink()
    print("Dropped only this Goal5 disposable database family and removed its private config.")


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    make = subparsers.add_parser("create")
    make.add_argument("--ai-root", required=True)
    make.add_argument("--cs-root", required=True)
    make.add_argument("--ai-python", required=True)
    make.add_argument("--cs-python", required=True)
    make.add_argument("--pg-bin", required=True)
    make.add_argument("--admin-input", required=True)
    make.add_argument("--output", required=True)
    remove = subparsers.add_parser("drop")
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
