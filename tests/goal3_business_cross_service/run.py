"""Install the parent guard before pytest/app/config imports, then collect explicitly."""

import os
import sys

from tests.goal3_business_cross_service import network

network.PREIMPORT_GUARD = network.install_guard([("127.0.0.1", 55438)])
network.self_check()
# No inherited runtime DSNs, service credentials or proxy configuration enter
# pytest or application settings. The explicit private fixture is the authority.
environment = {key: os.environ[key] for key in ("PATH", "GOAL3_PRIVATE_CONFIG") if key in os.environ}
environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
os.environ.clear()
os.environ.update(environment)
import pytest

raise SystemExit(pytest.main(["-q", "-p", "no:cacheprovider",
    "tests/goal3_business_cross_service/acceptance_http.py", *sys.argv[1:]]))
