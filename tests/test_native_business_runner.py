"""Failure-path evidence for the disposable native acceptance controller."""
from pathlib import Path
import json
import os
import subprocess
import sys
import venv
from unittest.mock import AsyncMock, Mock

import pytest

from tests.business_v2_e2e.native import (
    close_resources,
    git,
    python_executable,
    require_server_major,
    validate_pg18_binaries,
)


async def test_cleanup_attempts_other_children_and_handles_after_multiple_failures():
    client = Mock(aclose=AsyncMock(side_effect=RuntimeError("private credential")))
    slow = Mock()
    slow.poll.return_value = None
    slow.wait.side_effect = [subprocess.TimeoutExpired("owned", 10), 0]
    bad = Mock()
    bad.poll.return_value = None
    bad.terminate.side_effect = OSError("private path")
    bad.kill.side_effect = OSError("private path")
    later = Mock()
    later.poll.return_value = None
    handles = [Mock(), Mock()]
    handles[0].close.side_effect = OSError("private handle")
    errors = []
    await close_resources(client, {"bad": bad, "slow": slow, "later": later}, handles, errors)
    slow.kill.assert_called_once()
    assert slow.wait.call_count == 2
    later.terminate.assert_called_once()
    handles[1].close.assert_called_once()
    assert errors == [
        {"resource": "http_client", "error_type": "RuntimeError"},
        {"resource": "process_bad", "error_type": "OSError"},
        {"resource": "log_0", "error_type": "OSError"},
    ]


async def test_server_major_is_verified_before_caller_can_mutate():
    connection = Mock(fetchval=AsyncMock(return_value="180006"))
    with pytest.raises(RuntimeError):
        await require_server_major(connection, 16)
    await require_server_major(connection, 18)
    connection.execute.assert_not_called()


@pytest.mark.parametrize("wrong", ["initdb", "postgres", "pg_ctl"])
def test_all_pg18_tools_are_verified(monkeypatch, wrong):
    called = []

    def run(command, **kwargs):
        name = Path(command[0]).stem
        called.append(name)
        return Mock(stdout=f"{name} (PostgreSQL) {16 if name == wrong else 18}.6")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError):
        validate_pg18_binaries(Path("synthetic"))
    assert wrong in called


def test_source_check_strips_git_repository_overrides(monkeypatch, tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong"))
    assert Path(git(tmp_path, "rev-parse", "--show-toplevel")).resolve() == tmp_path.resolve()


def test_optimized_controller_is_rejected_before_setup():
    result = subprocess.run([sys.executable, "-O", "-m", "tests.business_v2_e2e.native"],
        capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "without python -O" in result.stderr


def test_python_launcher_keeps_real_venv_packages(tmp_path, monkeypatch):
    environment = tmp_path / "isolated-venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    launcher = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = subprocess.run([str(launcher), "-I", "-c",
        "import json,sys,sysconfig; print(json.dumps([sys.prefix,sysconfig.get_path('purelib')]))"],
        check=True, capture_output=True, text=True, timeout=20)
    prefix, packages = json.loads(probe.stdout)
    Path(packages, "native_venv_probe.py").write_text("VALUE = 'venv-only'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    executable = python_executable(launcher.relative_to(tmp_path))
    result = subprocess.run([str(executable), "-I", "-c",
        "import json,sys,native_venv_probe; print(json.dumps([sys.prefix,native_venv_probe.VALUE]))"],
        check=True, capture_output=True, text=True, timeout=20)
    assert executable.is_absolute()
    assert json.loads(result.stdout) == [prefix, "venv-only"]
