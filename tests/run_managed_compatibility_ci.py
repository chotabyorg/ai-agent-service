"""Exercise one exact AI/Conversation pair using only native disposable peers.

Only sanitized identities, outcomes and counts leave the runner. Private fixture
credentials, synthetic payloads and process logs are never uploaded as evidence.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

AI_ROOT = Path(__file__).resolve().parents[1]
PAIR_CASES = [
    "tests/managed_cross_service/acceptance_http.py::test_A_simple_same_turn_draft_and_terminal_replay",
    "tests/managed_cross_service/acceptance_http.py::test_current_website_v2_profile_crosses_real_cs_ai_http_and_replays",
    "tests/managed_cross_service/acceptance_http.py::test_historical_pre_stream_projection_crosses_real_cs_ai_http_after_restart",
    "tests/managed_cross_service/fingerprint_profiles_http.py::test_current_voice_v2_body_stream_and_terminal_replay_cross_real_http",
]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cs-root", type=Path, required=True)
    parser.add_argument("--expected-cs-revision", required=True)
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cs_root = args.cs_root.resolve()
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    ai_python, cs_python = AI_ROOT / ".venv/bin/python", cs_root / ".venv/bin/python"
    if (git(cs_root, "rev-parse", "HEAD") != args.expected_cs_revision
            or (AI_ROOT / ".env").exists() or (cs_root / ".env").exists()
            or git(AI_ROOT, "status", "--porcelain") or git(cs_root, "status", "--porcelain")):
        raise SystemExit("Exact clean candidates without .env are required")
    record = {
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "ai_revision": git(AI_ROOT, "rev-parse", "HEAD"),
        "ai_tree": git(AI_ROOT, "rev-parse", "HEAD^{tree}"),
        "conversation_revision": args.expected_cs_revision,
        "conversation_tree": git(cs_root, "rev-parse", "HEAD^{tree}"),
        "external_provider_requests": 0, "production_mutations": False,
        "scope": "Exact real HTTP/PG16 services with loopback-guarded synthetic providers",
        "status": "FAIL", "checks": {}, "owned_database_cleanup": False,
    }
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    admin, config = output / "private-admin.json", output / "private-pair.json"
    admin.write_text(json.dumps({"admin_user": "compatibility_ci",
        "admin_password": "isolated_compatibility_fixture", "port": 5432}))
    admin.chmod(0o600)
    provision = AI_ROOT / "tests/goal5_business_bridge/provision.py"

    def run(name: str, command: list[str], *, cwd: Path = AI_ROOT,
            child_env: dict[str, str] | None = None, timeout: int = 600) -> int:
        log = output / f"{name}.log"
        with log.open("xb") as handle:
            result = subprocess.run(command, cwd=cwd, env=child_env or env,
                stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
        log.chmod(0o600)
        return result.returncode

    def summary(name: str, code: int, report: Path) -> None:
        value: dict = {"returncode": code, "status": "FAIL"}
        if report.exists():
            cases = list(ET.parse(report).iter("testcase"))
            failed = [case for case in cases if case.find("failure") is not None
                or case.find("error") is not None]
            skipped = [case for case in cases if case.find("skipped") is not None]
            value.update(tests=len(cases), failed=len(failed), skipped=len(skipped),
                passed=len(cases) - len(failed) - len(skipped),
                junit_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
                failing_cases=[case.get("classname", "") + "::" + case.get("name", "")
                    for case in failed])
            if code == 0 and cases and not failed and not skipped:
                value["status"] = "PASS"
        record["checks"][name] = value
        print(json.dumps({name: value}), flush=True)

    try:
        code = run("provision", [str(ai_python), str(provision), "create",
            "--ai-root", str(AI_ROOT), "--cs-root", str(cs_root),
            "--ai-python", str(ai_python), "--cs-python", str(cs_python),
            "--pg-bin", str(args.pg_bin), "--admin-input", str(admin), "--output", str(config)])
        if code != 0:
            raise RuntimeError("Disposable provisioning failed")
        pair = json.loads(config.read_text())
        bridge_report = output / "business-bridge.xml"
        code = run("business-bridge", [str(ai_python), "tests/goal5_business_bridge/run.py",
            "--junitxml=" + str(bridge_report), "--basetemp=" + str(output / "bridge-tmp")],
            child_env={**env, "GOAL5_PRIVATE_CONFIG": str(config)})
        summary("business-bridge", code, bridge_report)
        pair_report = output / "wire-profiles.xml"
        code = run("wire-profiles", [str(cs_python), "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--junitxml=" + str(pair_report), "--basetemp=" + str(output / "profiles-tmp"), *PAIR_CASES],
            cwd=cs_root, child_env={**env,
                "TEST_DATABASE_URL": pair["databases"]["conversation"]["url"],
                "TEST_DATABASE_DISPOSABLE": "yes",
                "GOAL2_AI_TEST_DATABASE_URL": pair["databases"]["ai"]["url"],
                "GOAL2_AI_TEST_DATABASE_DISPOSABLE": "yes",
                "GOAL2_AI_REPO": str(AI_ROOT), "GOAL2_AI_PYTHON": str(ai_python)})
        summary("wire-profiles", code, pair_report)
        cs_report = output / "conversation-suite.xml"
        code = run("conversation-suite", [str(cs_python), "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--junitxml=" + str(cs_report), "--basetemp=" + str(output / "conversation-tmp")],
            cwd=cs_root, child_env={**env,
                "TEST_DATABASE_URL": pair["databases"]["conversation"]["url"],
                "TEST_DATABASE_DISPOSABLE": "yes"}, timeout=720)
        summary("conversation-suite", code, cs_report)
        record["source_unchanged"] = (
            git(AI_ROOT, "rev-parse", "HEAD") == record["ai_revision"]
            and git(cs_root, "rev-parse", "HEAD") == args.expected_cs_revision
            and not git(AI_ROOT, "status", "--porcelain")
            and not git(cs_root, "status", "--porcelain"))
        if record["source_unchanged"] and all(
                value["status"] == "PASS" for value in record["checks"].values()):
            record["status"] = "PASS"
    except Exception as error:
        record["error_type"] = type(error).__name__
    finally:
        if config.exists():
            try:
                code = run("cleanup", [str(ai_python), str(provision), "drop", "--config", str(config),
                    "--pg-bin", str(args.pg_bin), "--admin-input", str(admin)], timeout=60)
                record["owned_database_cleanup"] = code == 0
            except Exception as error:
                record["cleanup_error_type"] = type(error).__name__
        if not record["owned_database_cleanup"]:
            record["status"] = "FAIL"
        admin.unlink(missing_ok=True)
        (output / "summary.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2))
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
