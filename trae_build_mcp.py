#!/usr/bin/env python3
"""Trae Build MCP Server (stdio).

A minimal, dependency-free MCP server that exposes generic build tooling to
Trae CLI / VSCode Trae plugin. It auto-discovers `builder.json` (or legacy
`robin_builder.json`) in the current working directory and delegates to
trae_build_runner.py.

Protocol: JSON-RPC 2.0 over newline-delimited stdio (MCP 2024-11-05 stdio).
Tools exposed:
  - build_info        : show builder config (params, presets, artifacts)
  - build_run         : run a build (by preset and/or param overrides)
  - build_list       : list collected build artifacts
  - build_presets    : list available presets

Usage in trae_cli.yaml:
  mcp:
    servers:
      trae_builder:
        type: stdio
        command: python
        args:
          - D:\\work\\workspace\\trae_builder\\trae_build_mcp.py

The server runs in the project root it was launched from; the MCP client
(Trae) typically sets cwd to the open workspace, so builder.json is found
automatically. The project root can also be passed explicitly via the env
var TRAE_BUILDER_PROJECT or the first positional arg.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

# Make the sibling runner importable regardless of cwd.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import trae_build_runner as runner  # noqa: E402

SERVER_NAME = "trae-builder"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"

# Lock so tool calls (which may spawn long builds) don't interleave on stdout.
_WRITE_LOCK = threading.Lock()


def _project_dir() -> Path:
    env = os.environ.get("TRAE_BUILDER_PROJECT")
    if env:
        return Path(env).resolve()
    if len(sys.argv) > 1 and sys.argv[1]:
        return Path(sys.argv[1]).resolve()
    return Path(os.getcwd()).resolve()


# ---- JSON-RPC framing -------------------------------------------------------

def _send(msg: dict[str, Any]) -> None:
    data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    with _WRITE_LOCK:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _send_log(level: str, msg: str) -> None:
    """Best-effort log notification (MCP notifications/notifications/message)."""
    _send({"jsonrpc": "2.0", "method": "notifications/message",
           "params": {"level": level, "data": msg}})


# ---- Tool implementations ---------------------------------------------------

def _tool_build_info(args: dict[str, Any]) -> dict[str, Any]:
    project = args.get("project") or str(_project_dir())
    try:
        info = runner.list_info(Path(project))
        return {"content": [{"type": "text", "text": json.dumps(info, indent=2, ensure_ascii=False)}]}
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True}
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"ERROR: {type(e).__name__}: {e}"}], "isError": True}


def _tool_build_presets(args: dict[str, Any]) -> dict[str, Any]:
    project = args.get("project") or str(_project_dir())
    try:
        info = runner.list_info(Path(project))
        presets = info.get("presets", [])
        lines = [f"Available presets in {info.get('project_dir')}:"]
        if not presets:
            lines.append("  (none defined)")
        for p in presets:
            desc = p.get("description", "")
            lines.append(f"  - {p['name']}: {desc}")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"ERROR: {type(e).__name__}: {e}"}], "isError": True}


def _tool_build_run(args: dict[str, Any]) -> dict[str, Any]:
    project = args.get("project") or str(_project_dir())
    preset = args.get("preset")
    params = args.get("params") or {}
    timeout = args.get("timeout")
    dry_run = bool(args.get("dry_run", False))

    if not isinstance(params, dict):
        return {"content": [{"type": "text", "text": "ERROR: 'params' must be an object"}], "isError": True}

    try:
        res = runner.run_build(
            Path(project),
            params=params,
            preset=preset,
            timeout=timeout,
            dry_run=dry_run,
        )
        return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True}
    except ValueError as e:
        return {"content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True}
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"ERROR: {type(e).__name__}: {e}"}], "isError": True}


def _tool_build_list(args: dict[str, Any]) -> dict[str, Any]:
    project = args.get("project") or str(_project_dir())
    try:
        cfg = runner.load_builder(Path(project))
        arts = runner.scan_artifacts(cfg, Path(project))
        if not arts:
            return {"content": [{"type": "text", "text": "No build artifacts found (check scan_dirs / max_age_hours in builder.json)."}]}
        return {"content": [{"type": "text", "text": json.dumps(arts, indent=2, ensure_ascii=False)}]}
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True}
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"ERROR: {type(e).__name__}: {e}"}], "isError": True}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "build_info",
        "description": "Show the builder config for the current project (parameters, presets, artifacts config, toolchain). No build is performed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project root containing builder.json. Defaults to the server's working directory or TRAE_BUILDER_PROJECT env var."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "build_presets",
        "description": "List the named build presets defined in this project's builder.json.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "build_run",
        "description": "Run a build using the project's builder.json. Pass either a preset name and/or explicit parameter overrides (e.g. {\"Target\":\"tx\"}). Output artifacts are scanned afterwards.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project root. Defaults to cwd."},
                "preset": {"type": "string", "description": "Preset name from builder.json (e.g. 'rx_default')."},
                "params": {"type": "object", "description": "Parameter overrides as key/value. Keys must match the 'parameters' list in builder.json.", "additionalProperties": True},
                "timeout": {"type": "integer", "description": "Override build timeout in seconds."},
                "dry_run": {"type": "boolean", "description": "If true, print the command without executing the build.", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "build_list",
        "description": "List collected build artifacts (from the directories configured in builder.json 'artifacts.scan_dirs').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
]

TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "build_info": _tool_build_info,
    "build_presets": _tool_build_presets,
    "build_run": _tool_build_run,
    "build_list": _tool_build_list,
}


# ---- JSON-RPC dispatch ------------------------------------------------------

def _handle_initialize(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {
                "tools": {},
                "logging": {},
            },
        },
    }


def _handle_tools_list(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}


def _handle_tools_call(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Unknown tool: {name}"},
        }
    try:
        result = handler(args)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    except Exception as e:  # noqa: BLE001
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": f"ERROR: {type(e).__name__}: {e}"}], "isError": True},
        }


def _handle_ping(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": {}}


def _dispatch(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _handle_initialize(req_id, params)
    if method == "initialized":
        # notification, no response
        return None
    if method == "ping":
        return _handle_ping(req_id, params)
    if method == "tools/list":
        return _handle_tools_list(req_id, params)
    if method == "tools/call":
        return _handle_tools_call(req_id, params)
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}
    if method in ("notifications/initialized",):
        return None
    if method == "shutdown":
        return {"jsonrpc": "2.0", "id": req_id, "result": None}

    if req_id is not None:
        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def _read_messages() -> Any:
    """Yield parsed JSON messages from stdin (newline-delimited)."""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError as e:
            _send({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"Parse error: {e}"},
            })


def main() -> int:
    # Lightweight hello log (to stderr so it never corrupts the JSON-RPC stream).
    print(f"[{SERVER_NAME}] stdio MCP server starting (cwd={os.getcwd()})", file=sys.stderr)
    for msg in _read_messages():
        resp = _dispatch(msg)
        if resp is not None:
            _send(resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
