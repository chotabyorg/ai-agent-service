"""Provision/drop only fresh Goal3 task-owned PostgreSQL16 test databases."""

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from tests.goal3_business_cross_service.network import install_guard, self_check
from tests.goal3_business_cross_service.state import private_json

install_guard([("127.0.0.1", 55438)])
self_check()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "drop"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--cs-root", type=Path)
    parser.add_argument("--ai-python", type=Path)
    parser.add_argument("--cs-python", type=Path)
    args = parser.parse_args()
    if not args.config.is_absolute():
        parser.error("Absolute private configuration path required")
    # Import database/client settings only after the exact numeric loopback guard.
    import asyncpg
    from sqlalchemy.engine import URL, make_url

    if args.action == "create":
        if args.config.exists():
            parser.error("Refusing to replace an existing per-run private configuration")
        if any(path is None or not path.is_absolute() or not path.exists()
               for path in (args.cs_root, args.ai_python, args.cs_python)):
            parser.error("Explicit existing absolute paired candidate and runtimes required")
        password = os.environ.get("GOAL3_TEST_POSTGRES_PASSWORD")
        if not password:
            parser.error("Explicit test-only PostgreSQL password environment input required")
        suffix = uuid4().hex[:12]
        names = {key: "goal3_test_harness_" + key + "_" + suffix for key in ("ai", "cs", "provider")}
        config = {"disposable": "yes", "ai_root": str(Path(__file__).resolve().parents[2]),
                  "cs_root": str(args.cs_root), "ai_python": str(args.ai_python), "cs_python": str(args.cs_python),
                  "databases": {key: URL.create("postgresql+asyncpg", username="postgres", password=password,
                      host="127.0.0.1", port=55438, database=name).render_as_string(hide_password=False)
                      for key, name in names.items()}}
    else:
        config = json.loads(args.config.read_text())
        if config.get("disposable") != "yes":
            parser.error("Explicit disposable acknowledgement required")
        names, passwords, suffixes = {}, set(), set()
        for key in ("ai", "cs", "provider"):
            url = make_url(config.get("databases", {}).get(key, ""))
            match = re.fullmatch("goal3_test_harness_" + key + "_([0-9a-f]{12})", url.database or "")
            if (not match or url.drivername != "postgresql+asyncpg" or url.host != "127.0.0.1"
                    or url.port != 55438 or url.username != "postgres" or not url.password):
                parser.error("Refusing unknown/nonowned endpoint or database")
            names[key] = url.database
            passwords.add(url.password)
            suffixes.add(match[1])
        if len(passwords) != 1 or len(suffixes) != 1:
            parser.error("One private owned per-run database family required")
        password = passwords.pop()
    db = await asyncpg.connect(host="127.0.0.1", port=55438, user="postgres", password=password,
                               database="postgres", timeout=5)
    created = []
    try:
        if not 160000 <= int(await db.fetchval("SHOW server_version_num")) < 170000:
            raise RuntimeError("Owned Goal3 PostgreSQL16 required")
        for name in names.values():
            # Names were generated here or completely validated above. Never
            # terminate database sessions or use DROP FORCE against other owners.
            await db.execute(("CREATE" if args.action == "create" else "DROP") + ' DATABASE "' + name + '"')
            created.append(name)
        if args.action == "create":
            args.config.parent.mkdir(parents=True, exist_ok=True)
            private_json(args.config, config)
        else:
            args.config.unlink()
        print("Created three new owned test databases and private configuration" if args.action == "create"
              else "Dropped the three owned test databases and private configuration")
    except BaseException:
        if args.action == "create":
            for name in created:
                await db.execute('DROP DATABASE "' + name + '"')
        raise
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
