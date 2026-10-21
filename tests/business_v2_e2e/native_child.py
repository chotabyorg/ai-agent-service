"""Pre-import network fence and Windows event policy for native test children."""
import asyncio
import json
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
config = json.loads(Path(os.environ["BUSINESS_E2E_CONFIG"]).read_text())
from tests.goal5_business_bridge.network import install_guard, self_check  # noqa: E402
install_guard(config["allowlist"], allow_bound_loopback=True)
self_check()
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
if sys.argv[1] == "migrate":
    from alembic.config import CommandLine
    sys.path.insert(0, config["roots"][sys.argv[2]])
    CommandLine().main(argv=["upgrade", "head"])
elif sys.argv[1] == "worker":
    sys.path.insert(0, config["roots"]["cs"])
    from core.config import load_settings
    from db.database import Database
    from services.managed_client import ManagedAIClient
    from services.telegram_worker import TelegramWorker
    from services.website_worker import WebsiteWorker

    async def run_worker():
        settings = load_settings()
        database = Database(settings.database_url.get_secret_value())
        kind = TelegramWorker if config["channel"] == "TELEGRAM" else WebsiteWorker
        try:
            progressed = await kind(database, settings, ManagedAIClient(settings)).run_once()
            assert progressed, "NATIVE_WORKER_NO_PROGRESS"
        finally:
            await database.dispose()

    asyncio.run(run_worker())
else:
    runpy.run_path(str(ROOT / "tests/business_v2_e2e/bootstrap.py"), run_name="__main__")
