#!/usr/bin/env python3
"""Trae Build Runner - generic, SDK-agnostic build orchestrator.

Discovers a `builder.json` (or legacy `robin_builder.json`) in a project root,
then invokes the configured build script with the given parameters / preset.

Designed to be called either directly from the CLI or from the Trae MCP server
(trae_build_mcp.py). Zero third-party dependencies; stdlib only.

builder.json schema (see trae_builder_schema.json):
{
  "schema_version": "1.0",
  "sdk":  { "name": "...", "type": "eclipse|make|custom", "description": "..." },
  "build": {
    "script":   { "path": "scripts/robin_build_variant.ps1", "interpreter": "powershell.exe", "execution_policy": "Bypass" },
    "toolchain":{ "studio_exe": "...", "default_build_target": "..." },
    "parameters": [ { "name": "Target", "type": "enum|string|path|bool|int",
                      "enum": [...], "default": "...", "description": "..." }, ... ],
    "presets": [ { "name": "...", "description": "...", "params": { ... } }, ... ],
    "artifacts": { "scan_dirs": ["build_variants"], "name_pattern": "*.bin", "max_age_hours": 168 },
    "timeout_seconds": 900,
    "env": { "KEY": "value" }     // optional extra env vars
  }
}
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BUILDER_FILES = ("builder.json", "robin_builder.json")


def log(msg: str) -> None:
    print(msg, flush=True)


def find_builder_config(project_dir: Path) -> Path:
    for name in BUILDER_FILES:
        p = project_dir / name
        if p.is_file():
            return p
    raise FileNotFoundError(
        f"No builder config found in {project_dir}. Expected one of: {', '.join(BUILDER_FILES)}"
    )


def load_builder(project_dir: Path) -> dict[str, Any]:
    cfg_path = find_builder_config(project_dir)
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_path"] = str(cfg_path)
    return cfg


def resolve_script_path(cfg: dict[str, Any], project_dir: Path) -> Path:
    rel = cfg["build"]["script"]["path"]
    p = (project_dir / rel).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Build script not found: {p}")
    return p


def param_value_for_cli(param: dict[str, Any], value: Any) -> str:
    """Convert a python value to the string the build script expects on CLI."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return ""
    return str(value)


def build_arg_list(cfg: dict[str, Any], params: dict[str, Any], project_dir: Path) -> list[str]:
    spec_params = {p["name"]: p for p in cfg["build"].get("parameters", [])}
    args: list[str] = []
    for name, spec in spec_params.items():
        if name in params and params[name] not in (None, ""):
            val = params[name]
        else:
            val = spec.get("default", "")
        sval = param_value_for_cli(spec, val)
        # PowerShell param style: -Name value. For non-powershell, pass as --name value.
        args.append(f"-{name}")
        args.append(sval)
    return args


def resolve_preset(cfg: dict[str, Any], preset_name: str | None) -> dict[str, Any]:
    if not preset_name:
        return {}
    presets = cfg["build"].get("presets", [])
    for p in presets:
        if p["name"] == preset_name:
            return dict(p.get("params", {}))
    available = ", ".join(p["name"] for p in presets) or "(none)"
    raise ValueError(f"Unknown preset '{preset_name}'. Available: {available}")


def run_build(
    project_dir: Path,
    params: dict[str, Any] | None = None,
    preset: str | None = None,
    timeout: int | None = None,
    extra_env: dict[str, str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute the build. Returns a result dict (json-serializable)."""
    project_dir = project_dir.resolve()
    cfg = load_builder(project_dir)

    preset_params = resolve_preset(cfg, preset)
    merged = {**preset_params, **(params or {})}

    script_path = resolve_script_path(cfg, project_dir)
    script_cfg = cfg["build"]["script"]
    interpreter = script_cfg.get("interpreter", "")
    exec_policy = script_cfg.get("execution_policy", "")

    cli_args = build_arg_list(cfg, merged, project_dir)

    cmd: list[str] = []
    suffix = script_path.suffix.lower()
    interp_low = (interpreter or "").lower()
    is_powershell = "powershell" in interp_low or "pwsh" in interp_low or suffix == ".ps1"
    if is_powershell:
        pwsh = interpreter or ("powershell.exe" if os.name == "nt" else "pwsh")
        cmd = [pwsh, "-NoProfile", "-ExecutionPolicy", exec_policy or "Bypass", "-File", str(script_path)]
        cmd.extend(cli_args)
    elif interpreter:
        cmd = [interpreter, str(script_path)]
        cmd.extend(cli_args)
    elif suffix == ".py":
        py = sys.executable or ("python" if os.name == "nt" else "python3")
        cmd = [py, str(script_path)]
        cmd.extend(cli_args)
    elif suffix == ".sh":
        cmd = ["bash", str(script_path)]
        cmd.extend(cli_args)
    elif suffix == ".bat" and os.name != "nt":
        # .bat on non-Windows: best-effort via cmd.exe if available, else wine
        cmd = ["cmd.exe", "/c", str(script_path)]
        cmd.extend(cli_args)
    else:
        cmd = [str(script_path)]
        cmd.extend(cli_args)

    effective_timeout = timeout or cfg["build"].get("timeout_seconds", 900)

    env = os.environ.copy()
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    if cfg["build"].get("env"):
        env.update({k: str(v) for k, v in cfg["build"]["env"].items()})

    result = {
        "project_dir": str(project_dir),
        "config_file": cfg.get("_path"),
        "preset": preset,
        "params": merged,
        "command": cmd,
        "timeout_seconds": effective_timeout,
        "dry_run": dry_run,
    }

    if dry_run:
        result["status"] = "dry-run"
        return result

    start = time.time()
    log("[trae_build] Running build:")
    log("  cwd : " + str(project_dir))
    log("  cmd : " + " ".join(shlex.quote(c) for c in cmd))
    log("  preset: " + (preset or "(none)"))
    log("  params: " + json.dumps(merged, ensure_ascii=False))
    log("  timeout: %ss" % effective_timeout)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(project_dir),
            env=env,
            capture_output=False,
            timeout=effective_timeout,
        )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["elapsed_seconds"] = round(time.time() - start, 2)
        log(f"[trae_build] TIMEOUT after {effective_timeout}s")
        return result
    except FileNotFoundError as e:
        result["status"] = "error"
        result["error"] = str(e)
        result["elapsed_seconds"] = round(time.time() - start, 2)
        log(f"[trae_build] ERROR: {e}")
        return result

    elapsed = round(time.time() - start, 2)
    result["exit_code"] = rc
    result["elapsed_seconds"] = elapsed
    result["status"] = "success" if rc == 0 else "failed"
    log(f"[trae_build] Build {result['status']} (exit={rc}, {elapsed}s)")

    artifacts = scan_artifacts(cfg, project_dir)
    result["artifacts"] = artifacts
    if artifacts:
        log(f"[trae_build] Found {len(artifacts)} artifact(s):")
        for a in artifacts[:10]:
            log(f"  - {a['path']} ({a['size']} bytes)")
    return result


def scan_artifacts(cfg: dict[str, Any], project_dir: Path) -> list[dict[str, Any]]:
    art_cfg = cfg["build"].get("artifacts", {})
    scan_dirs = art_cfg.get("scan_dirs", [])
    pattern = art_cfg.get("name_pattern", "*.bin")
    max_age_hours = art_cfg.get("max_age_hours", 168)
    cutoff = time.time() - max_age_hours * 3600 if max_age_hours else 0

    found: list[dict[str, Any]] = []
    for d in scan_dirs:
        scan_path = (project_dir / d).resolve()
        if not scan_path.is_dir():
            continue
        for f in scan_path.glob(pattern):
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            found.append({
                "path": str(f.relative_to(project_dir)),
                "size": st.st_size,
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            })
    found.sort(key=lambda x: x["mtime"], reverse=True)
    return found


def list_info(project_dir: Path) -> dict[str, Any]:
    cfg = load_builder(project_dir)
    return {
        "project_dir": str(project_dir),
        "config_file": cfg.get("_path"),
        "sdk": cfg.get("sdk", {}),
        "parameters": cfg["build"].get("parameters", []),
        "presets": cfg["build"].get("presets", []),
        "artifacts_config": cfg["build"].get("artifacts", {}),
        "timeout_seconds": cfg["build"].get("timeout_seconds", 900),
        "script": cfg["build"].get("script", {}),
        "toolchain": cfg["build"].get("toolchain", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Trae generic build runner")
    parser.add_argument("--project", default=os.getcwd(), help="Project root containing builder.json")
    sub = parser.add_subparsers(dest="command")

    p_info = sub.add_parser("info", help="Show builder config for this project")
    p_build = sub.add_parser("build", help="Run a build")
    p_build.add_argument("--preset", help="Preset name from builder.json")
    p_build.add_argument("--timeout", type=int, help="Override timeout (seconds)")
    p_build.add_argument("--dry-run", action="store_true", help="Print command without running")
    p_build.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                         help="Override a parameter, e.g. --param Target=tx")
    p_list = sub.add_parser("list", help="List build artifacts")

    args = parser.parse_args()
    project_dir = Path(args.project).resolve()

    if args.command == "info" or args.command is None:
        try:
            info = list_info(project_dir)
            print(json.dumps(info, indent=2, ensure_ascii=False))
            return 0
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    if args.command == "build":
        params: dict[str, Any] = {}
        for kv in args.param:
            if "=" not in kv:
                print(f"ERROR: --param expects NAME=VALUE, got: {kv}", file=sys.stderr)
                return 2
            k, v = kv.split("=", 1)
            params[k] = v
        try:
            res = run_build(project_dir, params=params, preset=args.preset,
                            timeout=args.timeout, dry_run=args.dry_run)
        except (FileNotFoundError, ValueError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") in ("success", "dry-run") else 1

    if args.command == "list":
        try:
            cfg = load_builder(project_dir)
            arts = scan_artifacts(cfg, project_dir)
            print(json.dumps(arts, indent=2, ensure_ascii=False))
            return 0
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
