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
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
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


def _ensure_builder(project_dir: Path) -> dict[str, Any]:
    """Load builder.json; if missing, auto-generate via trae_build_init.detect().

    Used by run_build / flash_run / serial_capture so a fresh repo with no
    builder.json still works without a separate /build-init step.
    """
    try:
        return load_builder(project_dir)
    except FileNotFoundError:
        log(f"[build] no builder.json in {project_dir}; auto-generating...")
        try:
            import trae_build_init as _init
            cfg, detector = _init.detect(project_dir)
            out_path = project_dir / "builder.json"
            out_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            log(f"[build] auto-generated {out_path} (detector: {detector})")
        except Exception as e:  # noqa: BLE001
            raise FileNotFoundError(
                f"No builder.json in {project_dir} and auto-generation failed: {type(e).__name__}: {e}"
            ) from e
        return load_builder(project_dir)


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
    """Execute the build. Returns a result dict (json-serializable).

    If no builder.json exists, auto-generates one (no separate /build-init needed).
    """
    project_dir = project_dir.resolve()
    try:
        cfg = _ensure_builder(project_dir)
    except FileNotFoundError as e:
        return {"status": "error", "error": str(e), "project_dir": str(project_dir)}

    preset_params = resolve_preset(cfg, preset)
    merged = {**preset_params, **(params or {})}

    # Resolve .cproject info (per-config compile options) for the current
    # ProjectPath so callers (CLI / MCP) can show what each build actually
    # passes to the compiler. None if no .cproject is found.
    proj_path_rel = merged.get("ProjectPath") or next(
        (p.get("default", "") for p in cfg["build"].get("parameters", []) if p.get("name") == "ProjectPath"),
        "",
    )
    cproject_info = _build_cproject_info(project_dir, proj_path_rel) if proj_path_rel else None

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
        "cproject": cproject_info,
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
    params_list = cfg["build"].get("parameters", [])
    proj_path_rel = next(
        (p.get("default", "") for p in params_list if p.get("name") == "ProjectPath"),
        "",
    )
    return {
        "project_dir": str(project_dir),
        "config_file": cfg.get("_path"),
        "sdk": cfg.get("sdk", {}),
        "parameters": params_list,
        "presets": cfg["build"].get("presets", []),
        "artifacts_config": cfg["build"].get("artifacts", {}),
        "timeout_seconds": cfg["build"].get("timeout_seconds", 900),
        "script": cfg["build"].get("script", {}),
        "toolchain": cfg["build"].get("toolchain", {}),
        "serial": cfg.get("serial", {}),
        "flash": cfg.get("flash", {}),
        "cproject": _build_cproject_info(project_dir, proj_path_rel) if proj_path_rel else None,
    }


# ---------------------------------------------------------------------------
# .cproject parsing (Eclipse CDT) - extracts compile options per cconfiguration
# ---------------------------------------------------------------------------

# superClass suffix -> field key in the parsed-options dict.
_CPROJECT_OPT_FIELDS = {
    "compiler.option.def": ("defines", "list"),
    "compiler.option.incpath": ("includes", "list_path"),
    "compiler.option.optimize": ("optimization", "optimize"),
    "compiler.option.otherflags": ("other_flags", "str"),
    "compiler.option.optimize.other": ("other_opt_flags", "str"),
    "compiler.option.std": ("language_standard", "std"),
    "compiler.option.optimize.packstruct": ("pack_structs", "bool"),
    "compiler.option.optimize.shortenums": ("short_enums", "bool"),
    "asm.option.flags": ("asm_flags", "str"),
    "asm.option.include.paths": ("asm_includes", "list_path"),
    "linker.option.libpath": ("libpath", "list_path"),
    "linker.option.libs": ("libs", "list"),
}

_OPTIMIZE_MAP = {
    "com.telink.tc32eclipse.compiler.optimize.none": "O0",
    "com.telink.tc32eclipse.compiler.optimize.one": "O1",
    "com.telink.tc32eclipse.compiler.optimize.two": "O2",
    "com.telink.tc32eclipse.compiler.optimize.three": "O3",
    "com.telink.tc32eclipse.compiler.optimize.size": "Os",
}

_STD_MAP = {
    "com.telink.tc32eclipse.compiler.option.std.gnu99": "gnu99",
    "com.telink.tc32eclipse.compiler.option.std.gnu11": "gnu11",
    "com.telink.tc32eclipse.compiler.option.std.c99": "c99",
    "com.telink.tc32eclipse.compiler.option.std.c11": "c11",
}


def _subst_eclipse_path(raw: str, project_name: str, config_name: str) -> str:
    """Convert an Eclipse workspace_loc / ProjName / ConfigName string to a
    project-relative path (or a substituted raw value if it doesn't match the
    common workspace_loc pattern)."""
    s = raw.strip()
    # Strip surrounding quotes that CDT often emits in listOptionValue values.
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    # ${workspace_loc:/${ProjName}/<rest>}
    m = re.match(r"^\$\{workspace_loc:/\$\{ProjName\}/(.*)\}$", s)
    if m:
        return m.group(1).rstrip("/")
    # ${workspace_loc:/${ProjName}} (project root itself)
    if s == "${workspace_loc:/${ProjName}}":
        return ""
    # Fall back: substitute variables inline.
    s = s.replace("${ProjName}", project_name).replace("${ConfigName}", config_name)
    return s


def _map_optimize(value: str) -> str:
    return _OPTIMIZE_MAP.get(value, (value.rsplit(".", 1)[-1] if value else ""))


def _map_std(value: str) -> str:
    return _STD_MAP.get(value, (value.rsplit(".", 1)[-1] if value else ""))


def parse_cproject_options(cp_path: Path, project_name: str) -> dict[str, dict[str, Any]]:
    """Parse a Telink Eclipse .cproject file and return per-config compile options.

    Returns {config_name: {optimization, language_standard, defines, includes,
    other_flags, other_opt_flags, pack_structs, short_enums, asm_flags,
    asm_includes, linker_command, libpath, libs, postbuild, prebuild,
    builder_command, builder_arguments, build_artifact_type}}.

    Returns {} on read/parse failure (never raises).
    """
    try:
        tree = ET.parse(cp_path)
        root = tree.getroot()
    except Exception:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for cc in root.findall(".//cconfiguration"):
        try:
            settings = cc.find(".//storageModule[@moduleId='org.eclipse.cdt.core.settings']")
            if settings is None:
                continue
            config_name = settings.get("name")
            if not config_name:
                continue
            cfg = cc.find(".//storageModule[@moduleId='cdtBuildSystem']/configuration")
            if cfg is None:
                continue

            entry: dict[str, Any] = {
                "optimization": "",
                "language_standard": "",
                "defines": [],
                "includes": [],
                "other_flags": "",
                "other_opt_flags": "",
                "pack_structs": False,
                "short_enums": False,
                "asm_flags": "",
                "asm_includes": [],
                "linker_command": "",
                "libpath": [],
                "libs": [],
                "postbuild": "",
                "prebuild": "",
                "builder_command": "",
                "builder_arguments": "",
                "build_artifact_type": "",
            }

            # buildArtefactType: e.g. 'com.telink.tc32eclipse.buildArtefactType.app'
            art = cfg.get("buildArtefactType", "")
            if art:
                entry["build_artifact_type"] = art.rsplit(".", 1)[-1]
            entry["postbuild"] = cfg.get("postbuildStep", "") or ""
            entry["prebuild"] = cfg.get("prebuildStep", "") or ""

            builder = cfg.find(".//builder")
            if builder is not None:
                entry["builder_command"] = builder.get("command", "") or ""
                entry["builder_arguments"] = builder.get("arguments", "") or ""

            # Tool commands (linker.command lives here, not in <option>).
            for tool in cfg.findall(".//tool"):
                sc = tool.get("superClass", "") or ""
                cmd = tool.get("command", "") or ""
                if "tool.linker" in sc:
                    entry["linker_command"] = cmd

            # Options (defines/includes/optimization/flags/...).
            for opt in cfg.findall(".//option"):
                sc = opt.get("superClass", "") or ""
                # Strip any trailing .<id> instance suffix; match on the last
                # dotted segment(s) we care about.
                field = None
                for suffix, (fname, ftype) in _CPROJECT_OPT_FIELDS.items():
                    if sc.endswith(suffix):
                        field = (fname, ftype)
                        break
                if field is None:
                    continue
                fname, ftype = field
                if ftype == "list":
                    values = []
                    if opt.get("IS_VALUE_EMPTY", "false") != "true":
                        for lv in opt.findall("listOptionValue"):
                            v = lv.get("value", "")
                            if v is not None:
                                values.append(v)
                    entry[fname] = values
                elif ftype == "list_path":
                    values = []
                    if opt.get("IS_VALUE_EMPTY", "false") != "true":
                        for lv in opt.findall("listOptionValue"):
                            v = lv.get("value", "")
                            if v is not None:
                                values.append(_subst_eclipse_path(v, project_name, config_name))
                    entry[fname] = values
                elif ftype == "str":
                    entry[fname] = opt.get("value", "") or ""
                elif ftype == "bool":
                    entry[fname] = (opt.get("value", "false") or "false").lower() == "true"
                elif ftype == "optimize":
                    entry[fname] = _map_optimize(opt.get("value", "") or "")
                elif ftype == "std":
                    entry[fname] = _map_std(opt.get("value", "") or "")

            out[config_name] = entry
        except Exception:
            # One broken cconfiguration shouldn't sink the others.
            continue
    return out


def _resolve_project_name(dot_project_path: Path) -> str:
    """Read the Eclipse project name from a .project file. Falls back to the
    parent dir name on failure."""
    try:
        import trae_build_init as _init
        name = _init._parse_project_name(dot_project_path)
        return name or dot_project_path.parent.name
    except Exception:
        return dot_project_path.parent.name


def _build_cproject_info(project_dir: Path, project_path_rel: str) -> dict[str, Any] | None:
    """Locate .cproject/.project under project_path_rel and assemble the
    `cproject` field attached to `info`/`build` outputs. Returns None if no
    .cproject is found at the given path."""
    if not project_path_rel:
        return None
    cp_dir = (project_dir / project_path_rel).resolve()
    cp_path = cp_dir / ".cproject"
    if not cp_path.is_file():
        return None
    dot_proj = cp_dir / ".project"
    proj_name = _resolve_project_name(dot_proj) if dot_proj.is_file() else cp_dir.name
    configs = parse_cproject_options(cp_path, proj_name)
    available_targets = [f"{proj_name}/{c}" for c in configs]
    return {
        "path": str(cp_path),
        "project_name": proj_name,
        "configs": configs,
        "available_targets": available_targets,
    }


def _format_build_options_summary(target: str, opts: dict[str, Any] | None) -> str:
    """Format a single config's parsed options as a human-readable block."""
    if not opts:
        return f"[build options for {target}]\n  (config not found in .cproject)"
    lines = [f"[build options for {target}]"]
    lines.append(f"  optimization   : {opts.get('optimization', '') or '(none)'}")
    lines.append(f"  language       : {opts.get('language_standard', '') or '(none)'}")
    lines.append(f"  defines        : {', '.join(opts.get('defines', [])) or '(none)'}")
    lines.append(f"  includes       : {', '.join(opts.get('includes', [])) or '(none)'}")
    lines.append(f"  other_flags    : {opts.get('other_flags', '') or '(none)'}")
    lines.append(f"  other_opt_flags: {opts.get('other_opt_flags', '') or '(none)'}")
    lines.append(f"  pack_structs   : {str(opts.get('pack_structs', False)).lower()}")
    lines.append(f"  short_enums    : {str(opts.get('short_enums', False)).lower()}")
    lines.append(f"  asm_flags      : {opts.get('asm_flags', '') or '(none)'}")
    lines.append(f"  asm_includes   : {', '.join(opts.get('asm_includes', [])) or '(none)'}")
    lines.append(f"  linker         : {opts.get('linker_command', '') or '(none)'}")
    lines.append(f"  libpath        : {', '.join(opts.get('libpath', [])) or '(none)'}")
    lines.append(f"  libs           : {', '.join(opts.get('libs', [])) or '(none)'}")
    lines.append(f"  postbuild      : {opts.get('postbuild', '') or '(none)'}")
    lines.append(f"  build_artifact : {opts.get('build_artifact_type', '') or '(none)'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Serial port capture (optional; requires pyserial)
# ---------------------------------------------------------------------------

def _import_serial():
    """Lazy-import pyserial with a friendly error if missing."""
    try:
        import serial  # noqa: F401
        from serial.tools import list_ports  # noqa: F401
        return serial, list_ports
    except ImportError as e:
        raise RuntimeError(
            "pyserial is required for serial capture. Install it with: pip install pyserial"
        ) from e


def serial_list_ports() -> list[dict[str, Any]]:
    """List available serial ports (cross-platform). Returns [{port, description, hwid}]."""
    _, list_ports = _import_serial()
    out: list[dict[str, Any]] = []
    for p in list_ports.comports():
        out.append({"port": p.device, "description": p.description, "hwid": p.hwid})
    # Sort: COM ports numerically, others lexically.
    import re as _re
    def keyfn(x):
        m = _re.match(r"COM(\d+)", x["port"])
        return (0, int(m.group(1))) if m else (1, x["port"])
    out.sort(key=keyfn)
    return out


def serial_capture(
    project_dir: Path,
    port: str = "",
    baud: int | None = None,
    duration_seconds: float = 5.0,
    max_lines: int = 200,
    timeout_seconds: float | None = None,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> dict[str, Any]:
    """Open a serial port and capture output for a bounded time/line count.

    Reads `cfg["serial"]` from builder.json for defaults (baud, port, timeout).
    Caller args override config. Returns the captured text plus metadata.
    """
    project_dir = project_dir.resolve()
    cfg = _ensure_builder(project_dir)
    s_cfg = cfg.get("serial", {})
    port = port or s_cfg.get("default_port", "")
    baud = baud if baud is not None else int(s_cfg.get("baud", 115200))
    if timeout_seconds is None:
        timeout_seconds = float(s_cfg.get("timeout_seconds", 60))
    if not port:
        # Auto-pick first available port if none configured/specified.
        ports = serial_list_ports()
        if not ports:
            return {"status": "error", "error": "No serial port specified and none detected. Set serial.default_port in builder.json or pass port."}
        port = ports[0]["port"]

    serial_mod, _ = _import_serial()
    result: dict[str, Any] = {
        "project_dir": str(project_dir),
        "port": port,
        "baud": baud,
        "duration_seconds": duration_seconds,
        "max_lines": max_lines,
    }
    lines: list[str] = []
    started = time.time()
    try:
        with serial_mod.Serial(port=port, baudrate=baud, timeout=0.1) as ser:
            log(f"[serial] capturing {port} @ {baud} for {duration_seconds}s (max {max_lines} lines)")
            buf = bytearray()
            while True:
                if time.time() - started > duration_seconds:
                    break
                if len(lines) >= max_lines:
                    break
                chunk = ser.read(256)
                if chunk:
                    buf.extend(chunk)
                    # Split on any newline; keep incomplete tail in buf.
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        line = bytes(buf[:nl])
                        del buf[: nl + 1]
                        lines.append(line.decode(encoding, errors=errors).rstrip("\r"))
                    # Non-blocking-ish: small sleep to yield CPU when idle.
                else:
                    time.sleep(0.01)
            # Flush any trailing bytes without a newline.
            if buf:
                lines.append(bytes(buf).decode(encoding, errors=errors).rstrip("\r"))
    except serial_mod.SerialException as e:
        result["status"] = "error"
        result["error"] = f"SerialException: {e}"
        result["lines"] = lines
        return result
    except Exception as e:  # noqa: BLE001
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    text = "\n".join(lines)
    result["line_count"] = len(lines)
    result["byte_count"] = len(text.encode(encoding, errors=errors))
    result["elapsed_seconds"] = round(time.time() - started, 2)
    result["capture"] = text
    result["status"] = "captured" if lines else "no-data"
    return result


# ---------------------------------------------------------------------------
# Firmware flashing via Telink bdt.exe (optional)
# ---------------------------------------------------------------------------

# Map common repo chip-dir names to bdt.exe chip prefixes (bdt expects UPPER).
_DEFAULT_CHIP_MAP: dict[str, str] = {
    "B80": "B80", "B80B": "B80B", "B85": "B85", "B87": "B87", "B89": "B89",
    "B91": "B91", "B92": "B92",
    "TC1211": "TC1211", "TC321X": "TC321X", "tc122x": "TC122X", "tc123x": "TC123X",
    "TL321X": "TL321X", "TL721X": "TL721X", "TL751X": "TL751X", "TL322X": "TL322X",
}

_DEFAULT_BDT_PATH = r"E:\TelinkIoTStudio\tools\libusbBDT\bin\bdt.exe"


def _latest_artifact_bin(project_dir: Path, cfg: dict[str, Any]) -> str | None:
    """Return the newest .bin under artifacts.scan_dirs, or None."""
    art = cfg.get("build", {}).get("artifacts", {})
    scan_dirs = art.get("scan_dirs", ["build_variants"])
    pattern = art.get("name_pattern", "*.bin")
    candidates: list[Path] = []
    for d in scan_dirs:
        p = (project_dir / d).resolve()
        if p.is_dir():
            candidates.extend(p.glob(pattern))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0])


def flash_run(
    project_dir: Path,
    chip: str = "",
    command: str = "wf",
    address: int | str = 0,
    input_file: str = "",
    output_file: str = "",
    size: str = "",
    erase: bool = False,
    extra_flags: list[str] | None = None,
    timeout: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Invoke Telink BDT Cmd_download_tool.exe to flash/read/reset the chip.

    Reads `cfg["flash"]` from builder.json for defaults (bdt_path, default_chip,
    device_id, flash_mode, reset_after_flash, timeout). Caller args override config.

    BDT command-line format (verified with BDT v5.8.5 / TC122X):
        Cmd_download_tool.exe <device_id> <chip> <command> <addr> [flags...]
    where device_id=1 (single device), and flags include:
        -i <file>   input file
        -o <file>   output file
        -s <size>   size (e.g. 64k)
        -e          erase before write
        -u          USB mode (omit for EVK mode)
        -f          flash reset (for rst command)
        -c          core reset (for rst command)

    For EVK mode (default), the tool requires:
        1. ac        — activate MCU (must succeed before wf/rf)
        2. lf 0 0    — unlock flash (required before wf -e on protected flash)
        3. wf ...    — write flash
        4. rst -f    — reset MCU

    For USB mode (add -u flag), ac/lf steps may be skipped.
    """
    project_dir = project_dir.resolve()
    cfg = _ensure_builder(project_dir)
    f_cfg = cfg.get("flash", {})
    bdt_path = f_cfg.get("bdt_path") or _DEFAULT_BDT_PATH

    # Resolve chip: arg > config default_chip > map from any preset's chip.
    chip = chip or f_cfg.get("default_chip", "")
    if not chip:
        for p in cfg.get("build", {}).get("presets", []):
            bt = p.get("params", {}).get("BuildTarget", "")
            if "/" in bt:
                head = bt.split("/", 1)[0]
                chip = _DEFAULT_CHIP_MAP.get(head, head)
                break
    if not chip:
        return {"status": "error",
                "error": "No chip specified. Set flash.default_chip in builder.json or pass chip=."}

    # Device ID (first positional arg to Cmd_download_tool.exe, 1=single device)
    device_id = str(f_cfg.get("device_id", 1))

    # Flash mode: "evk" (default, no -u flag) or "usb" (add -u flag)
    flash_mode = f_cfg.get("flash_mode", "evk")
    mode_flag = ["-u"] if flash_mode == "usb" else []

    # Auto-pick latest artifact as input_file for wf when none given.
    if not input_file and command in ("wf", "wc", "wo"):
        auto = f_cfg.get("default_input_file", "")
        if auto:
            input_file = auto
        else:
            latest = _latest_artifact_bin(project_dir, cfg)
            if latest:
                input_file = latest
                log(f"[flash] auto-selected latest artifact: {latest}")

    if timeout is None:
        timeout = int(f_cfg.get("timeout_seconds", 120))

    def _run_bdt(bdt_cmd: list[str], t: int) -> tuple[int, str]:
        """Execute a single BDT command, return (exit_code, output)."""
        log(f"[flash] running: {' '.join(shlex.quote(c) for c in bdt_cmd)}")
        try:
            proc = subprocess.run(
                bdt_cmd, cwd=str(project_dir),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                timeout=t,
            )
            return proc.returncode, (proc.stdout or "")[-4000:]
        except subprocess.TimeoutExpired:
            return -1, "(timed out)"
        except FileNotFoundError as e:
            return -1, str(e)

    steps = f_cfg.get("steps", [])
    is_multi_step = bool(steps) and command == "wf"

    # Build the main command
    def _build_cmd(cmd_command: str, cmd_addr: str = "", cmd_file: str = "",
                   cmd_flags: list[str] | None = None) -> list[str]:
        c: list[str] = [bdt_path, device_id, chip, cmd_command]
        if cmd_addr:
            c.append(cmd_addr)
        if cmd_file:
            c += ["-i", cmd_file]
        if cmd_flags:
            c += cmd_flags
        c += mode_flag  # -u for USB, empty for EVK
        return c

    result: dict[str, Any] = {
        "project_dir": str(project_dir),
        "bdt_path": bdt_path,
        "chip": chip,
        "device_id": device_id,
        "flash_mode": flash_mode,
        "command": command,
        "address": address,
        "input_file": input_file,
        "output_file": output_file,
        "size": size,
        "erase": erase or f_cfg.get("default_erase", False),
        "timeout_seconds": timeout,
        "dry_run": dry_run,
    }

    if dry_run:
        # Show what would be run
        cmds_preview = []
        if is_multi_step:
            for step in steps:
                if step == "ac":
                    cmds_preview.append([bdt_path, device_id, chip, "ac"] + mode_flag)
                elif step == "lf_unlock":
                    cmds_preview.append([bdt_path, device_id, chip, "lf", "0", "0"] + mode_flag)
                elif step == "wf_bootloader":
                    boot_bin = f_cfg.get("bootloader_bin", "")
                    cmds_preview.append([bdt_path, device_id, chip, "wf", "0", "-i", boot_bin, "-e"] + mode_flag)
                elif step == "wf_app":
                    app_bin = f_cfg.get("app_bin", "")
                    app_addr = str(f_cfg.get("app_addr", 0))
                    cmds_preview.append([bdt_path, device_id, chip, "wf", app_addr, "-i", app_bin] + mode_flag)
                elif step == "wf":
                    flags = ["-e"] if (erase or f_cfg.get("default_erase", False)) else []
                    if input_file:
                        flags += ["-i", input_file]
                    cmds_preview.append([bdt_path, device_id, chip, "wf", str(address)] + flags + mode_flag)
                elif step == "rst":
                    cmds_preview.append([bdt_path, device_id, chip, "rst", "-f"] + mode_flag)
        else:
            main_cmd = [bdt_path, device_id, chip, command, str(address)]
            if input_file:
                main_cmd += ["-i", input_file]
            if output_file:
                main_cmd += ["-o", output_file]
            if size:
                main_cmd += ["-s", size]
            if erase or f_cfg.get("default_erase", False):
                main_cmd += ["-e"]
            main_cmd += mode_flag
            cmds_preview.append(main_cmd)
        result["commands"] = [" ".join(shlex.quote(c) for c in cmd) for cmd in cmds_preview]
        result["status"] = "dry-run"
        return result

    if not Path(bdt_path).is_file():
        result["status"] = "error"
        result["error"] = f"BDT tool not found at {bdt_path}. Set flash.bdt_path in builder.json."
        return result

    started = time.time()
    all_outputs: list[str] = []

    if is_multi_step:
        # Multi-step flash: ac -> lf_unlock -> wf_bootloader -> wf_app -> rst
        step_results = []
        for step in steps:
            step_cmd: list[str] = []
            step_name = step
            step_timeout = timeout
            if step == "ac":
                step_cmd = [bdt_path, device_id, chip, "ac"] + mode_flag
                step_timeout = min(timeout, 10)
            elif step == "lf_unlock":
                step_cmd = [bdt_path, device_id, chip, "lf", "0", "0"] + mode_flag
                step_timeout = min(timeout, 10)
            elif step == "wf_bootloader":
                boot_bin = f_cfg.get("bootloader_bin", "")
                if not boot_bin:
                    continue
                boot_path = str(project_dir / "build_variants" / boot_bin) if not os.path.isabs(boot_bin) else boot_bin
                step_cmd = [bdt_path, device_id, chip, "wf", "0", "-i", boot_path, "-e"] + mode_flag
            elif step == "wf_app":
                app_bin = f_cfg.get("app_bin", "")
                if not app_bin:
                    continue
                app_path = str(project_dir / "build_variants" / app_bin) if not os.path.isabs(app_bin) else app_bin
                app_addr = str(f_cfg.get("app_addr", 0))
                step_cmd = [bdt_path, device_id, chip, "wf", app_addr, "-i", app_path] + mode_flag
            elif step == "wf":
                flags = ["-e"] if (erase or f_cfg.get("default_erase", False)) else []
                if input_file:
                    flags += ["-i", input_file]
                step_cmd = [bdt_path, device_id, chip, "wf", str(address)] + flags + mode_flag
            elif step == "rst":
                step_cmd = [bdt_path, device_id, chip, "rst", "-f"] + mode_flag
                step_timeout = 10
            else:
                continue

            rc, out = _run_bdt(step_cmd, step_timeout)
            step_results.append({"step": step_name, "exit_code": rc, "output": out[-1000:]})
            all_outputs.append(f"--- {step_name} (rc={rc}) ---\n{out}")
            if rc != 0:
                result["status"] = "failed"
                result["exit_code"] = rc
                result["output"] = "\n".join(all_outputs)[-4000:]
                result["elapsed_seconds"] = round(time.time() - started, 2)
                result["steps"] = step_results
                log(f"[flash] FAILED at step '{step_name}' (exit={rc})")
                return result

        result["status"] = "success"
        result["exit_code"] = 0
        result["output"] = "\n".join(all_outputs)[-4000:]
        result["elapsed_seconds"] = round(time.time() - started, 2)
        result["steps"] = step_results
        log(f"[flash] SUCCESS (all {len(step_results)} steps, {result['elapsed_seconds']}s)")
        return result

    # Single-command mode (rf, wc, rc, ac, rst, etc.)
    main_cmd = [bdt_path, device_id, chip, command, str(address)]
    if input_file:
        main_cmd += ["-i", input_file]
    if output_file:
        main_cmd += ["-o", output_file]
    if size:
        main_cmd += ["-s", size]
    if erase or f_cfg.get("default_erase", False):
        main_cmd += ["-e"]
    main_cmd += mode_flag
    if extra_flags:
        main_cmd += list(extra_flags)

    result["command_line"] = " ".join(shlex.quote(c) for c in main_cmd)

    rc, out = _run_bdt(main_cmd, timeout)
    result["exit_code"] = rc
    result["output"] = out[-4000:]
    result["elapsed_seconds"] = round(time.time() - started, 2)
    result["status"] = "success" if rc == 0 else "failed"
    log(f"[flash] {result['status']} (exit={rc}, {result['elapsed_seconds']}s)")

    # Optional auto-reset after write flash (single-command mode only).
    if (rc == 0 and command == "wf"
            and f_cfg.get("reset_after_flash", True)):
        rst_cmd = [bdt_path, device_id, chip, "rst", "-f"] + mode_flag
        rc2, rst_out = _run_bdt(rst_cmd, 30)
        result["reset_exit_code"] = rc2
        result["reset_output"] = rst_out[-2000:]

    return result


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

    p_serial = sub.add_parser("serial", help="Serial port capture (requires pyserial)")
    p_serial_sub = p_serial.add_subparsers(dest="serial_command")
    p_serial_sub.add_parser("list", help="List available serial ports")
    p_scap = p_serial_sub.add_parser("capture", help="Capture serial output")
    p_scap.add_argument("--port", default="", help="Serial port (e.g. COM3 / /dev/ttyUSB0); empty = config default or auto-detect")
    p_scap.add_argument("--baud", type=int, default=None, help="Baud rate; empty = config default (115200)")
    p_scap.add_argument("--duration", type=float, default=5.0, help="Capture duration in seconds")
    p_scap.add_argument("--max-lines", type=int, default=200, help="Max lines to capture")

    p_flash = sub.add_parser("flash", help="Flash firmware via Telink bdt.exe")
    p_flash.add_argument("--chip", default="", help="bdt chip prefix (e.g. TL721X, B80); empty = config default or infer from presets")
    p_flash.add_argument("--command", dest="command_arg", default="wf", help="bdt command: wf/rf/wc/rc/wa/ra/wo/ro/lf/rst/pc/ac (default wf)")
    p_flash.add_argument("--address", default="0", help="Address (default 0)")
    p_flash.add_argument("--input", default="", help="Input file for wf/wc/wo (-i); empty = latest artifact under build_variants")
    p_flash.add_argument("--output", default="", help="Output file for rf/rc/ro (-o)")
    p_flash.add_argument("--size", default="", help="Size, e.g. 512k (-s)")
    p_flash.add_argument("--erase", action="store_true", help="Erase before write (-e)")
    p_flash.add_argument("--timeout", type=int, default=None, help="Override timeout seconds")
    p_flash.add_argument("--dry-run", action="store_true", help="Print bdt command without executing")

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
        # Append a human-readable summary of the compile options for the
        # current BuildTarget (split on '/' or '@' so both formats work).
        cp_info = res.get("cproject")
        if cp_info:
            target = res.get("params", {}).get("BuildTarget", "") or ""
            cfg_name = ""
            for sep in ("/", "@"):
                if sep in target:
                    _, cfg_name = target.split(sep, 1)
                    break
            if not cfg_name:
                cfg_name = target
            opts = cp_info.get("configs", {}).get(cfg_name) if cfg_name else None
            if opts is not None:
                print()
                print(_format_build_options_summary(target, opts))
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

    if args.command == "serial":
        try:
            if args.serial_command == "list":
                ports = serial_list_ports()
                print(json.dumps(ports, indent=2, ensure_ascii=False))
                return 0
            if args.serial_command == "capture":
                res = serial_capture(project_dir, port=args.port, baud=args.baud,
                                      duration_seconds=args.duration, max_lines=args.max_lines)
                print(json.dumps(res, indent=2, ensure_ascii=False))
                return 0 if res.get("status") in ("captured", "no-data") else 1
            print("Usage: serial <list|capture> ...", file=sys.stderr)
            return 2
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    if args.command == "flash":
        try:
            res = flash_run(project_dir, chip=args.chip, command=args.command_arg,
                            address=args.address, input_file=args.input,
                            output_file=args.output, size=args.size,
                            erase=args.erase, timeout=args.timeout, dry_run=args.dry_run)
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0 if res.get("status") in ("success", "dry-run") else 1
        except (FileNotFoundError, RuntimeError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
