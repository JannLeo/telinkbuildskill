#!/usr/bin/env python3
"""Trae Builder Initializer - generate a builder.json for any repo.

Scans a project root for known build patterns (Telink Eclipse headless,
generic make, batch scripts, single script) and emits a starter builder.json
that trae_build_runner.py / trae_build_mcp.py can consume.

Usage:
  python trae_build_init.py <project-root> [--ide <IDE path>] [--out builder.json]
  python trae_build_init.py <project-root>            # auto-detect, write builder.json
  python trae_build_init.py <project-root> --dry-run   # print, don't write

Detected patterns (in order):
  1. Telink "release_sdk_tool" (A-type): tools/release_sdk_tool/compile.bat + ReleaseSDK.bat
  2. Telink "telink_ble post-build" (B-type): rom_lib.bat / flash_on_rom_lib.bat + .cproject
  3. Existing robin_builder.json: normalize/rename to builder.json
  4. Generic makefile / Makefile
  5. Any *.bat / *.ps1 / *.sh build script at root
  6. Fallback: minimal template prompting the user to fill in

The generated builder.json is a starting point; adjust script path / parameters
/ presets to match the actual build. Zero third-party deps; stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


def _read(path: Path, limit: int = 4096) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _find(root: Path, patterns: list[str], max_depth: int = 4) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if len(rel.parts) > max_depth:
            continue
        name = p.name.lower()
        if name in patterns:
            out.append(p)
    return out


def _detect_telink_a_type(root: Path) -> dict[str, Any] | None:
    """A-type: tools/release_sdk_tool/compile.bat (+ ReleaseSDK.bat)."""
    candidates = _find(root, ["releasesdk.bat", "rleasesdk.bat"], max_depth=4)
    compile_bats = _find(root, ["compile.bat"], max_depth=4)
    # Filter compile.bat to those under a release_sdk_tool dir.
    compile_bats = [c for c in compile_bats if "release_sdk_tool" in str(c).lower().replace("\\", "/")]
    if not compile_bats:
        return None
    compile_bat = compile_bats[0]
    tool_dir = compile_bat.parent
    # IDE path hint from ReleaseSDK.bat / compile.bat
    ide_hint = "C:\\TelinkIoTStudio"
    rel_bat = next((p for p in candidates if p.parent == tool_dir), None)
    txt = _read(rel_bat or compile_bat)
    m = re.search(r'(?:TOOL_PATH_R?LEASE_SDK|IDE_PATH)\s*=\s*([^\r\n]+)', txt, re.IGNORECASE)
    if m:
        ide_hint = m.group(1).strip().strip('"')
    # Eclipse launcher name
    launcher = "TelinkIoTStudio.exe"
    if "TelinkSDK" in ide_hint or "eclipse.exe" in txt:
        launcher = "eclipse.exe"
    # Discover chip dirs under project/tlsr_tc32/*
    chip_dirs: list[str] = []
    for c in ("B80", "B80B", "B85", "B87", "B89", "TC321X", "TC1211", "tc122x"):
        d = root / "project" / "tlsr_tc32" / c
        if d.is_dir():
            chip_dirs.append(c)
    # Also search nested (some repos have project/ two levels down)
    if not chip_dirs:
        for pd in _find(root, [".cproject"], max_depth=6):
            parts = pd.relative_to(root).parts
            if "project" in parts and "tlsr_tc32" in parts:
                idx = parts.index("tlsr_tc32")
                if idx + 1 < len(parts):
                    chip_dirs.append(parts[idx + 1])
        chip_dirs = sorted(set(chip_dirs))
    default_chip = chip_dirs[0] if chip_dirs else "B80"
    params: list[dict[str, Any]] = [
        {"name": "IdePath", "type": "path", "default": ide_hint, "description": "Telink IoT Studio / Eclipse IDE path"},
        {"name": "ProjectPath", "type": "path", "default": f"project/tlsr_tc32/{default_chip}", "description": "Eclipse project dir relative to SDK root"},
        {"name": "BuildTarget", "type": "string", "default": f"{default_chip}/Release", "description": "Eclipse cleanBuild target: <Project>/<ConfigName>"},
        {"name": "WorkspaceDir", "type": "path", "default": "", "description": "Eclipse workspace dir; empty = script default"},
        {"name": "OutputDir", "type": "path", "default": "build_variants", "description": "Where collected artifacts go"},
    ]
    presets: list[dict[str, Any]] = []
    for chip in chip_dirs[:4]:
        presets.append({
            "name": f"{chip.lower()}_release",
            "description": f"Build {chip} release firmware",
            "params": {"ProjectPath": f"project/tlsr_tc32/{chip}", "BuildTarget": f"{chip}/Release"},
        })
    script_rel = compile_bat.relative_to(root).as_posix()
    return {
        "sdk": {"name": root.name, "type": "eclipse", "description": f"Telink SDK (A-type, release_sdk_tool) - {root.name}"},
        "build": {
            "script": {"path": script_rel, "interpreter": "cmd.exe", "execution_policy": "Bypass"},
            "toolchain": {"studio_exe": ide_hint, "eclipse_launcher": launcher, "default_build_target": f"{default_chip}/Release"},
            "parameters": params,
            "presets": presets,
            "artifacts": {"scan_dirs": ["build_variants"], "name_pattern": "*.bin", "max_age_hours": 168},
            "timeout_seconds": 1200,
        },
    }


def _detect_telink_b_type(root: Path) -> dict[str, Any] | None:
    """B-type: telink_ble post-build bats (rom_lib.bat / flash_on_rom_lib.bat) + .cproject, no release_sdk_tool."""
    bats = _find(root, ["rom_lib.bat", "flash_on_rom_lib.bat", "flash_wo_rom_lib.bat"], max_depth=4)
    cprojects = _find(root, [".cproject"], max_depth=5)
    if not bats or not cprojects:
        return None
    # Pick the most likely "build" bat: flash_on_rom_lib.bat preferred, else rom_lib.bat
    build_bat = next((b for b in bats if b.name.lower() == "flash_on_rom_lib.bat"), bats[0])
    # Extract a cconfiguration id from .cproject to use as build target
    build_target = ""
    cp = cprojects[0]
    cp_txt = _read(cp, 8192)
    m = re.search(r'com\.telink\.tc32eclipse\.configuration\.app\.(\w+)\.', cp_txt)
    if m:
        build_target = m.group(1)  # e.g. "debug" or "release"
    chip = ""
    parts = cp.relative_to(root).parts
    if "tlsr_tc32" in parts:
        idx = parts.index("tlsr_tc32")
        if idx + 1 < len(parts):
            chip = parts[idx + 1]
    if not chip:
        chip = "B80"
    project_rel = cp.parent.relative_to(root).as_posix()
    params = [
        {"name": "IdePath", "type": "path", "default": "C:\\TelinkIoTStudio", "description": "Telink IoT Studio / Eclipse IDE path"},
        {"name": "ProjectPath", "type": "path", "default": project_rel, "description": "Eclipse project dir relative to SDK root"},
        {"name": "BuildTarget", "type": "string", "default": f"{chip}/{build_target or 'Release'}", "description": "Eclipse cleanBuild target: <Project>/<ConfigName>"},
        {"name": "WorkspaceDir", "type": "path", "default": "", "description": "Eclipse workspace dir; empty = script default"},
        {"name": "OutputDir", "type": "path", "default": "build_variants", "description": "Where collected artifacts go"},
    ]
    script_rel = build_bat.relative_to(root).as_posix()
    return {
        "sdk": {"name": root.name, "type": "eclipse", "description": f"Telink SDK (B-type, post-build bats) - {root.name}"},
        "build": {
            "script": {"path": script_rel, "interpreter": "cmd.exe", "execution_policy": "Bypass"},
            "toolchain": {"studio_exe": "C:\\TelinkIoTStudio", "eclipse_launcher": "TelinkIoTStudio.exe", "default_build_target": f"{chip}/{build_target or 'Release'}"},
            "parameters": params,
            "presets": [
                {"name": "flash_rom", "description": "Build flash-on-rom-lib firmware", "params": {}},
            ],
            "artifacts": {"scan_dirs": ["release_bin", "build_variants"], "name_pattern": "*.bin", "max_age_hours": 168},
            "timeout_seconds": 1200,
        },
    }


def _detect_existing_robin(root: Path) -> dict[str, Any] | None:
    """Existing robin_builder.json: load and normalize."""
    p = root / "robin_builder.json"
    if not p.is_file():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    cfg["sdk"] = cfg.get("sdk", {"name": root.name, "type": "eclipse"})
    cfg.setdefault("schema_version", SCHEMA_VERSION)
    return cfg


def _detect_telink_eclipse_only(root: Path) -> dict[str, Any] | None:
    """C-type: .cproject present but no release_sdk_tool and no post-build bats.
    Pure Eclipse projects: use the generic eclipse_headless_build.ps1 script."""
    cprojects = _find(root, [".cproject"], max_depth=6)
    if not cprojects:
        return None
    # Collect distinct chip/project dirs.
    projects: list[tuple[str, str]] = []  # (chip, project_rel)
    for cp in cprojects:
        parts = cp.relative_to(root).parts
        chip = ""
        project_rel = cp.parent.relative_to(root).as_posix()
        if "tlsr_tc32" in parts:
            idx = parts.index("tlsr_tc32")
            if idx + 1 < len(parts):
                chip = parts[idx + 1]
        if not chip:
            chip = cp.parent.name
        projects.append((chip, project_rel))
    # Dedupe by project_rel.
    seen = set()
    uniq = []
    for chip, prel in projects:
        if prel not in seen:
            seen.add(prel)
            uniq.append((chip, prel))
    projects = uniq
    if not projects:
        return None
    default_chip, default_proj = projects[0]
    # Extract build config name from first .cproject.
    cp = cprojects[0]
    cp_txt = _read(cp, 8192)
    cfg_name = "Release"
    m = re.search(r'com\.telink\.tc32eclipse\.configuration\.app\.(\w+)\.', cp_txt)
    if m:
        cfg_name = m.group(1).capitalize()
    # Generic headless script path (shipped with trae_builder).
    headless_script = "D:/work/workspace/trae_builder/scripts/eclipse_headless_build.ps1"
    params = [
        {"name": "IdePath", "type": "path", "default": "C:\\TelinkIoTStudio", "description": "Telink IoT Studio / Eclipse IDE directory"},
        {"name": "ProjectPath", "type": "path", "default": default_proj, "description": "Eclipse project dir relative to SDK root"},
        {"name": "BuildTarget", "type": "string", "default": f"{default_chip}/{cfg_name}", "description": "Eclipse cleanBuild target: <Project>/<ConfigName>"},
        {"name": "WorkspaceDir", "type": "path", "default": "", "description": "Eclipse workspace dir; empty = auto (../woekspace/<sdkname>)"},
        {"name": "OutputDir", "type": "path", "default": "build_variants", "description": "Where collected artifacts go"},
    ]
    presets: list[dict[str, Any]] = []
    for chip, prel in projects[:6]:
        pname = f"{chip.lower()}_{cfg_name.lower()}"
        presets.append({
            "name": pname,
            "description": f"Build {chip} {cfg_name} ({prel})",
            "params": {"ProjectPath": prel, "BuildTarget": f"{chip}/{cfg_name}"},
        })
    return {
        "sdk": {"name": root.name, "type": "eclipse", "description": f"Telink SDK (Eclipse projects, no release_sdk_tool) - {root.name}"},
        "build": {
            "script": {"path": headless_script, "interpreter": "powershell.exe", "execution_policy": "Bypass"},
            "toolchain": {"studio_exe": "C:\\TelinkIoTStudio", "eclipse_launcher": "TelinkIoTStudio.exe", "default_build_target": f"{default_chip}/{cfg_name}"},
            "parameters": params,
            "presets": presets,
            "artifacts": {"scan_dirs": ["build_variants"], "name_pattern": "*.bin", "max_age_hours": 168},
            "timeout_seconds": 1200,
        },
    }


def _detect_make(root: Path) -> dict[str, Any] | None:
    mf = root / "Makefile"
    if not mf.is_file():
        mf = root / "makefile"
    if not mf.is_file():
        return None
    txt = _read(mf, 4096)
    targets = sorted(set(re.findall(r'^([a-zA-Z0-9_-]+):', txt, re.MULTILINE)))
    targets = [t for t in targets if t not in (".PHONY", "all", "clean")]
    all_target = "all" if "all" in re.findall(r'^([a-zA-Z0-9_-]+):', txt, re.MULTILINE) else (targets[0] if targets else "")
    params = [
        {"name": "Target", "type": "string", "default": all_target, "description": "Make target"},
        {"name": "MakeArgs", "type": "string", "default": "", "description": "Extra make args (e.g. V=1 -j4)"},
        {"name": "OutputDir", "type": "path", "default": "build", "description": "Artifacts dir"},
    ]
    presets = [{"name": t, "description": f"make {t}", "params": {"Target": t}} for t in targets[:8]]
    return {
        "sdk": {"name": root.name, "type": "make"},
        "build": {
            "script": {"path": "Makefile", "interpreter": "make"},
            "parameters": params,
            "presets": presets,
            "artifacts": {"scan_dirs": ["build"], "name_pattern": "*.bin", "max_age_hours": 168},
            "timeout_seconds": 600,
        },
    }


def _detect_generic_script(root: Path) -> dict[str, Any] | None:
    """Any single build-ish script at root."""
    for name in ("build.ps1", "build.bat", "build.sh", "compile.bat", "compile.sh"):
        p = root / name
        if p.is_file():
            interp = "powershell.exe" if p.suffix.lower() == ".ps1" else ("bash" if p.suffix.lower() == ".sh" else "cmd.exe")
            return {
                "sdk": {"name": root.name, "type": "custom"},
                "build": {
                    "script": {"path": name, "interpreter": interp},
                    "parameters": [
                        {"name": "OutputDir", "type": "path", "default": "build", "description": "Artifacts dir"},
                    ],
                    "presets": [{"name": "default", "description": "Run default build", "params": {}}],
                    "artifacts": {"scan_dirs": ["build", "release_bin"], "name_pattern": "*.bin", "max_age_hours": 168},
                    "timeout_seconds": 900,
                },
            }
    return None


def _fallback(root: Path) -> dict[str, Any]:
    return {
        "sdk": {"name": root.name, "type": "custom", "description": "Auto-generated template; please fill in build.script.path"},
        "build": {
            "script": {"path": "REPLACE_WITH_BUILD_SCRIPT", "interpreter": "powershell.exe"},
            "parameters": [
                {"name": "OutputDir", "type": "path", "default": "build", "description": "Artifacts dir"},
            ],
            "presets": [{"name": "default", "description": "Default build", "params": {}}],
            "artifacts": {"scan_dirs": ["build"], "name_pattern": "*.bin", "max_age_hours": 168},
            "timeout_seconds": 900,
        },
    }


def detect(root: Path, ide_override: str | None = None) -> tuple[dict[str, Any], str]:
    """Return (config, detector_name)."""
    for fn, name in (
        (_detect_existing_robin, "existing robin_builder.json"),
        (_detect_telink_a_type, "telink release_sdk_tool (A-type)"),
        (_detect_telink_b_type, "telink_ble post-build (B-type)"),
        (_detect_telink_eclipse_only, "telink eclipse-only (C-type)"),
        (_detect_make, "makefile"),
        (_detect_generic_script, "generic build script"),
    ):
        cfg = fn(root)
        if cfg:
            if ide_override and cfg.get("build", {}).get("parameters"):
                for p in cfg["build"]["parameters"]:
                    if p["name"] == "IdePath":
                        p["default"] = ide_override
                if "toolchain" in cfg["build"]:
                    cfg["build"]["toolchain"]["studio_exe"] = ide_override
            cfg["schema_version"] = SCHEMA_VERSION
            return cfg, name
    cfg = _fallback(root)
    return cfg, "fallback template"


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate builder.json for a project")
    ap.add_argument("project", help="Project root to scan")
    ap.add_argument("--ide", help="Override IDE path (e.g. C:\\TelinkIoTStudio)")
    ap.add_argument("--out", default="builder.json", help="Output filename (default: builder.json)")
    ap.add_argument("--dry-run", action="store_true", help="Print config, don't write")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2

    cfg, detector = detect(root, args.ide)
    text = json.dumps(cfg, indent=2, ensure_ascii=False)

    print(f"[trae_build_init] detector: {detector}")
    print(f"[trae_build_init] project : {root}")
    if args.dry_run:
        print(text)
        return 0

    out_path = root / args.out
    out_path.write_text(text + "\n", encoding="utf-8")
    print(f"[trae_build_init] wrote {out_path}")
    print("[trae_build_init] review/adjust script.path, parameters and presets to match the actual build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
