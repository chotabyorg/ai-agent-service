"""Run pinned P0a AI assertions with synthetic P1a receiver provisioning only."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

P0A_SHA = "f0c8a94c035696260c2fb0ae1959d95aa36b2041"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    metrics = args.metrics_root.resolve()
    if git(metrics, "rev-parse", "HEAD") != P0A_SHA or git(metrics, "status", "--porcelain"):
        raise RuntimeError("P0a requires its clean pinned metrics checkout")
    if git(root, "status", "--porcelain"):
        raise RuntimeError("Commit the AI candidate before recording P0a SHA evidence")
    sha = git(root, "rev-parse", "HEAD")
    sys.path.insert(0, str(metrics))
    from security.worker import synthetic_environment

    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    env = synthetic_environment("ai-agent-service", root, sha, "asgi")
    env["PYTHONPATH"] = str(root) + os.pathsep + str(metrics)
    # Install the same P0a network/process/.env and source-origin guards before
    # importing pytest or the application. No parent credentials are inherited.
    child = """
import sys
from pathlib import Path
from security.worker import install_runtime_guard, install_service_origin_guard
install_runtime_guard()
install_service_origin_guard(Path(sys.argv[1]))
import pytest
raise SystemExit(pytest.main(sys.argv[2:]))
"""
    invocation = [sys.executable, "-c", child, str(root), "-q", "--noconftest",
                  "-c", str(metrics / "security/pytest.ini"), "-p", "no:cacheprovider",
                  "-p", "pytest_asyncio.plugin", "-p", "security.pytest_plugin",
                  "--security-result-file", str(report),
                  str(root / "tests/p0a_receiver_contract.py")]
    with tempfile.TemporaryDirectory(prefix="chota-p1a-p0a-") as scratch:
        result = subprocess.run(invocation, cwd=scratch, env=env)
    if not report.is_file():
        raise RuntimeError("P0a worker produced no evidence")
    data = json.loads(report.read_text())
    if not data.get("cases"):
        raise RuntimeError("P0a collected no assertions")
    data.update(source_sha=sha, metrics_sha=P0A_SHA, seam="asgi",
                provisioning="strict-p1a-ownership-principal")
    report.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    counts = Counter(row["status"] for row in data["cases"])
    print(json.dumps(dict(source_sha=sha, metrics_sha=P0A_SHA, statuses=dict(counts))))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
