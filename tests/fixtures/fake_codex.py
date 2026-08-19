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


def valid_text_input(value: object) -> bool:
    return (
        type(value) is list
        and len(value) == 1
        and type(value[0]) is dict
        and set(value[0]) == {"text", "type"}
        and value[0]["type"] == "text"
        and isinstance(value[0]["text"], str)
        and bool(value[0]["text"].strip())
    )


def schema_variant(
    method: str,
    params_title: str,
    *,
    properties: dict[str, object] | None = None,
    required: list[str] | None = None,
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": [method]},
            "params": {
                "title": params_title,
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
        "required": ["method", "params"],
    }


def write_schema(output: Path, attempt: int) -> None:
    client_variants = [
        schema_variant(f"fake/account/attempt-{attempt}", "GetAccountParams"),
        schema_variant(
            "fake/config",
            "ConfigReadParams",
            properties={
                "cwd": {"type": ["string", "null"]},
                "includeLayers": {"type": "boolean"},
            },
        ),
        {
            "title": "ConfigRequirements/readRequest",
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["fake/requirements"]},
                "params": {"type": "null"},
            },
            "required": ["method"],
        },
        schema_variant(
            "fake/features",
            "ExperimentalFeatureListParams",
            properties={"cursor": {"type": ["string", "null"]}},
        ),
        schema_variant("fake/voices", "ThreadRealtimeListVoicesParams"),
        schema_variant(
            "fake/interrupt",
            "TurnInterruptParams",
            properties={
                "threadId": {"type": "string"},
                "turnId": {"type": "string"},
            },
            required=["threadId", "turnId"],
        ),
    ]
    server_variants = [
        schema_variant("fake/command-approval", "ExecCommandApprovalParams"),
        schema_variant("fake/file-approval", "ApplyPatchApprovalParams"),
    ]
    (output / "ClientRequest.json").write_text(
        json.dumps({"oneOf": client_variants}),
        encoding="utf-8",
    )
    (output / "ServerRequest.json").write_text(
        json.dumps({"oneOf": server_variants}),
        encoding="utf-8",
    )


arguments = sys.argv[1:]
scenario = "default"
if arguments and arguments[0].startswith("--scenario="):
    scenario = arguments.pop(0).removeprefix("--scenario=")

if arguments == ["--version"]:
    if scenario == "version-failure":
        fail("VERSION_STDERR_SECRET")
    if scenario != "version-empty":
        print("fake-codex 99.1-test")
    raise SystemExit

if len(arguments) in {4, 5} and arguments[:3] == [
    "app-server",
    "generate-json-schema",
    "--out",
]:
    output = Path(arguments[3])
    experimental = arguments[4:] == ["--experimental"]
    if arguments[4:] not in ([], ["--experimental"]):
        fail("unexpected schema arguments")
    marker = output / ".schema-call-count"
    attempt = int(marker.read_text(encoding="utf-8")) + 1 if marker.exists() else 1
    marker.write_text(str(attempt), encoding="utf-8")
    if experimental and scenario in {"schema-stable-only", "schema-both-fail"}:
        print("unexpected argument '--experimental' SCHEMA_STDERR_SECRET", file=sys.stderr)
        raise SystemExit(2)
    if experimental and scenario == "schema-other-failure":
        print("backend unavailable SCHEMA_STDERR_SECRET", file=sys.stderr)
        raise SystemExit(70)
    if experimental and scenario == "schema-non-utf8":
        sys.stderr.buffer.write(b"\xff")
        sys.stderr.buffer.flush()
        raise SystemExit(70)
    if not experimental and scenario == "schema-both-fail":
        print("stable generation failed SCHEMA_STDERR_SECRET", file=sys.stderr)
        raise SystemExit(70)
    write_schema(output, attempt)
    raise SystemExit

if arguments != [
    "app-server",
    "--listen",
    "stdio://",
    "--enable",
    "realtime_conversation",
]:
    fail("unexpected app-server arguments")

initialized = False
deferred_request: dict[str, object] | None = None
interaction_thread_id = "integration-thread-1"
interaction_active_turn_id: str | None = None
interaction_thread_start_count = 0
interaction_turn_start_count = 0
interaction_steer_count = 0
interaction_interrupt_count = 0

for line in sys.stdin:
    message = json.loads(line)
    method = message["method"]
    params = message.get("params", {})
    request_id = message.get("id")

    if method == "initialize":
        if scenario == "unresponsive-initialize":
            print("initialize request received", file=sys.stderr, flush=True)
            continue
        if scenario == "delayed-initialize":
            time.sleep(0.05)
        if scenario == "reject-initialize" or "reject-initialize" in Path(sys.argv[0]).name:
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
        result: dict[str, object]
        if scenario == "initialize-missing-user-agent":
            result = {
                "platformFamily": "test",
                "platformOs": "test",
                "unknownMetadata": "INITIALIZE_METADATA_SECRET",
            }
        elif scenario == "initialize-invalid-platform":
            result = {
                "userAgent": "fake-codex",
                "platformFamily": {"value": "INITIALIZE_METADATA_SECRET"},
                "platformOs": "test",
            }
        else:
            result = {
                "userAgent": "fake-codex",
                "platformFamily": "test",
                "platformOs": "test",
                "unknownMetadata": {"accepted": True},
            }
        send({"id": request_id, "result": result})
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
    elif scenario == "interaction" and method == "effective/thread-start":
        if params != {
            "cwd": params.get("cwd"),
            "ephemeral": True,
            "sandbox": "read-only",
            "approvalPolicy": "never",
        } or not isinstance(params.get("cwd"), str):
            fail("invalid interaction thread start")
        interaction_thread_start_count += 1
        send({"id": request_id, "result": {"thread": {"id": interaction_thread_id}}})
    elif scenario == "interaction" and method == "effective/turn-start":
        if params.get("threadId") != interaction_thread_id or not valid_text_input(
            params.get("input")
        ):
            fail("invalid interaction turn start")
        interaction_turn_start_count += 1
        interaction_active_turn_id = f"integration-turn-{interaction_turn_start_count}"
        send(
            {
                "id": request_id,
                "result": {"turn": {"id": interaction_active_turn_id}},
            }
        )
    elif scenario == "interaction" and method == "effective/turn-steer":
        if (
            params.get("threadId") != interaction_thread_id
            or params.get("expectedTurnId") != interaction_active_turn_id
            or not valid_text_input(params.get("input"))
        ):
            fail("invalid interaction steer")
        interaction_steer_count += 1
        send({"id": request_id, "result": {"turnId": interaction_active_turn_id}})
    elif scenario == "interaction" and method == "effective/turn-interrupt":
        if params != {
            "threadId": interaction_thread_id,
            "turnId": interaction_active_turn_id,
        }:
            fail("invalid interaction interrupt")
        interaction_interrupt_count += 1
        interaction_active_turn_id = None
        send({"id": request_id, "result": {}})
    elif scenario == "interaction" and method == "test/interaction/snapshot":
        send(
            {
                "id": request_id,
                "result": {
                    "threadId": interaction_thread_id,
                    "activeTurnId": interaction_active_turn_id,
                    "threadStartCount": interaction_thread_start_count,
                    "turnStartCount": interaction_turn_start_count,
                    "steerCount": interaction_steer_count,
                    "interruptCount": interaction_interrupt_count,
                },
            }
        )
    elif scenario == "interaction" and method == "test/interaction/complete":
        if interaction_active_turn_id is None:
            fail("no interaction turn to complete")
        turn_id = interaction_active_turn_id
        for item_type, item_id, private_payload in (
            (
                "commandExecution",
                "private-integration-command-item",
                {
                    "command": "cat /private/integration-command",
                    "cwd": "/private/integration-worktree",
                    "patch": "PRIVATE_PATCH_BODY",
                    "reasoning": "PRIVATE_REASONING",
                },
            ),
            (
                "webSearch",
                "private-integration-web-item",
                {
                    "query": "PRIVATE_WEB_QUERY",
                    "arguments": {"token": "PRIVATE_MCP_ARGUMENT"},
                    "reasoning": "PRIVATE_REASONING",
                },
            ),
        ):
            item = {"id": item_id, "type": item_type} | private_payload
            for progress_method in ("item/started", "item/completed"):
                send(
                    {
                        "method": progress_method,
                        "params": {
                            "threadId": interaction_thread_id,
                            "turnId": turn_id,
                            "item": item,
                        },
                    }
                )
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": interaction_thread_id,
                    "turnId": turn_id,
                    "item": {
                        "id": "integration-item-final",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "integration final",
                    },
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": interaction_thread_id,
                    "turn": {"id": turn_id, "items": [], "status": "completed"},
                },
            }
        )
        interaction_active_turn_id = None
        send({"id": request_id, "result": {}})
    elif scenario == "interaction" and method == "test/interaction/patch-approval":
        approval_turn_id = interaction_active_turn_id or "integration-turn-approval"
        approval_item_id = "integration-item-patch"
        send(
            {
                "method": "item/fileChange/patchUpdated",
                "params": {
                    "threadId": interaction_thread_id,
                    "turnId": approval_turn_id,
                    "itemId": approval_item_id,
                    "changes": [
                        {
                            "path": "/private/integration-target.txt",
                            "kind": {"type": "update"},
                            "diff": "@@ integration patch @@",
                        }
                    ],
                },
            }
        )
        approval_request_id = "integration-approval-1"
        send(
            {
                "id": approval_request_id,
                "method": "alias/fileApproval",
                "params": {
                    "threadId": interaction_thread_id,
                    "turnId": approval_turn_id,
                    "itemId": approval_item_id,
                    "startedAtMs": 1_770_000_000_000,
                },
            }
        )
        approval_line = sys.stdin.readline()
        if not approval_line:
            fail("interaction approval response was not received")
        approval_response = json.loads(approval_line)
        if approval_response.get("id") != approval_request_id or set(approval_response) != {
            "id",
            "result",
        }:
            fail("invalid interaction approval response")
        send(
            {
                "id": request_id,
                "result": {"approvalResponse": approval_response["result"]},
            }
        )
    elif scenario == "interaction" and method == "test/interaction/connection-loss":
        os._exit(23)
    elif method == "trigger/server-request":
        server_request_id: int | str = "fake-request-7" if params.get("idKind") == "string" else 7
        send(
            {
                "id": server_request_id,
                "method": "fake/ask",
                "params": {"allowed": True},
            },
        )
        response_line = sys.stdin.readline()
        if not response_line:
            fail("client response was not received")
        response = json.loads(response_line)
        if set(response) != {"id", "result"}:
            fail("invalid client response shape")
        if response["id"] != server_request_id or type(response["id"]) is not type(
            server_request_id,
        ):
            fail("client response id changed")
        barrier_id = "fake-barrier-8"
        send(
            {
                "id": barrier_id,
                "method": "fake/barrier",
                "params": {"originalId": server_request_id},
            },
        )
        barrier_line = sys.stdin.readline()
        if not barrier_line:
            fail("barrier response was not received")
        barrier_response = json.loads(barrier_line)
        if barrier_response.get("id") == server_request_id:
            fail("duplicate client response")
        if barrier_response != {"id": barrier_id, "result": {"reached": True}}:
            fail("invalid barrier response")
        send(
            {
                "id": request_id,
                "result": {
                    "clientResponse": response["result"],
                    "responseIdType": type(response["id"]).__name__,
                    "responseCount": 1,
                },
            },
        )
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
    elif method == "stdout-eof-delayed-exit":
        stdout_fd = sys.stdout.fileno()
        sys.stdout.flush()
        with Path(os.devnull).open("w", encoding="utf-8") as stdout_sink:
            os.dup2(stdout_sink.fileno(), stdout_fd)
            time.sleep(0.75)
            os._exit(23)
    elif method == "exit":
        os._exit(23)
    else:
        send(
            {
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            },
        )
