#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import NoReturn


def send(message: object) -> None:
    print(json.dumps(message, separators=(",", ":")), flush=True)


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(64)


if sys.argv[1:] != [
    "app-server",
    "--listen",
    "stdio://",
    "--enable",
    "realtime_conversation",
]:
    fail(f"unexpected arguments: {sys.argv[1:]!r}")

initialized = False
deferred_request: dict[str, object] | None = None

for line in sys.stdin:
    message = json.loads(line)
    method = message["method"]
    params = message.get("params", {})
    request_id = message.get("id")

    if method == "initialize":
        if "reject-initialize" in Path(sys.argv[0]).name:
            send(
                {
                    "id": request_id,
                    "error": {"code": -32002, "message": "initialize rejected"},
                },
            )
            continue
        expected = {
            "clientInfo": {"name": "moco", "title": "moco", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }
        if params != expected:
            fail("unexpected initialize parameters")
        send({"method": "fake/ready", "params": {"phase": "initializing"}})
        send({"id": request_id, "result": {"userAgent": "fake-codex"}})
    elif method == "initialized":
        initialized = True
    elif not initialized:
        send(
            {
                "id": request_id,
                "error": {"code": -32002, "message": "Not initialized"},
            },
        )
    elif method == "ping":
        send({"method": "fake/interleaved", "params": {"value": params["value"]}})
        send({"id": request_id, "result": {"value": params["value"]}})
    elif method == "concurrent/first":
        deferred_request = message
    elif method == "concurrent/second":
        send({"id": request_id, "result": {"order": "second"}})
        if deferred_request is None:
            fail("concurrent requests arrived in the wrong order")
        send({"id": deferred_request["id"], "result": {"order": "first"}})
        deferred_request = None
    elif method == "server/error":
        send(
            {
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": "realtime unavailable",
                    "data": {"retryable": False},
                },
            },
        )
    elif method == "never":
        continue
    elif method == "malformed":
        print("{not-json", flush=True)
    elif method == "non-object":
        send(["not", "an", "object"])
    elif method == "invalid-constant":
        print('{"id":NaN,"result":{}}', flush=True)
    elif method == "invalid-id":
        send({"id": True, "result": {}})
    elif method == "missing-result":
        send({"id": request_id})
    elif method == "invalid-error":
        send({"id": request_id, "error": "not-an-object"})
    elif method == "invalid-error-message":
        send({"id": request_id, "error": {"code": -32000, "message": 42}})
    elif method == "invalid-error-code":
        send({"id": request_id, "error": {"code": True, "message": "bad code"}})
    elif method == "unknown-response":
        send({"id": 99999, "result": {"ignored": True}})
        send({"id": request_id, "result": {"accepted": True}})
    elif method == "notification-default-params":
        send({"method": "fake/default-params"})
        send({"id": request_id, "result": {}})
    elif method == "notification-without-method":
        send({"params": {}})
    elif method == "notification-invalid-params":
        send({"method": "fake/invalid", "params": []})
    elif method == "client/status":
        if request_id is not None or params != {"ready": True}:
            fail("invalid client status")
    elif method == "stderr":
        sensitive_value = "RPC_" + "SENSITIVE"
        print(
            f"diagnostic output Authorization: Bearer {sensitive_value}",
            file=sys.stderr,
            flush=True,
        )
        send({"id": request_id, "result": {}})
    elif method == "hang-on-close":
        send({"id": request_id, "result": {}})
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)
    elif method == "exit":
        os._exit(23)
    else:
        send(
            {
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            },
        )
