"""Offline synthetic MCP child used only by managed resource lifetime tests."""
import json
import os
import signal
import sys
import time

mode = os.environ.get("GOAL2_CHILD_MODE", "success")
with open(os.environ["GOAL2_CHILD_PIDS"], "a") as output:
    output.write(str(os.getpid()) + "\n")
    output.flush()
print("managed-secret-sentinel-child-stderr", file=sys.stderr, flush=True)
if mode == "ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def stall():
    while True:
        time.sleep(0.1)


for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if (method == "initialize" and mode in {"stall_initialize", "ignore_term"}
            or method == "tools/list" and mode == "stall_list"
            or method == "resources/read" and mode == "stall_metadata"):
        stall()
    if method == "initialize":
        result = dict(protocolVersion=request["params"]["protocolVersion"],
                      capabilities=dict(tools={}, resources={}), serverInfo=dict(name="synthetic", version="1"))
    elif method == "tools/list":
        if mode == "fail_list":
            print(json.dumps(dict(jsonrpc="2.0", id=request["id"], error=dict(code=-32603, message="managed-secret-sentinel"))), flush=True)
            continue
        result = dict(tools=[dict(name="read_fake", description="Offline read", inputSchema=dict(type="object", properties={}))])
    elif method == "resources/read":
        if mode == "malformed_protocol":
            print('{"malformed":"managed-secret-sentinel"}', flush=True)
            stall()
        result = dict(contents=[dict(uri="tools://metadata", mimeType="application/json",
            text="managed-secret-sentinel-invalid-json" if mode == "fail_metadata" else '{"read_fake":{"tier":"core"}}')])
    elif method == "tools/call":
        result = dict(content=[dict(type="text", text='{"ok":true}')], isError=False)
    else:
        result = {}
    print(json.dumps(dict(jsonrpc="2.0", id=request["id"], result=result)), flush=True)
