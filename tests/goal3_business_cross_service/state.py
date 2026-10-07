"""Private synthetic observations and test-only phase signals."""

import asyncio
import fcntl
import json
import os
import time
import tempfile
from pathlib import Path


def private_json(path, value):
    target = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, prefix=target.name + ".", delete=False) as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str))
        temporary = Path(handle.name)
    temporary.replace(target)


class State:
    def __init__(self, directory):
        self.directory = Path(directory)

    def append(self, kind, value):
        path = self.directory / "trace.jsonl"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(json.dumps({"kind": kind, "value": value}, ensure_ascii=False) + "\n")
            handle.flush()
            fcntl.flock(handle, fcntl.LOCK_UN)
        path.chmod(0o600)

    async def phase(self, name):
        faults = json.loads((self.directory / "faults.json").read_text())
        if faults.get("phase") != name:
            return
        private_json(self.directory / ("entered-" + name + ".json"), {"phase": name, "pid": os.getpid()})
        deadline = time.monotonic() + 40
        while not (self.directory / ("release-" + name)).exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Owned Goal3 phase deadline")
            await asyncio.sleep(.01)
