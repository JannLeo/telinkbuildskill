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


def _parse_cproject_configs(cp_path: Path) -> list[str]:
    """Extract all cconfiguration names from a Telink Eclipse .cproject file.

    Each cconfiguration has a child storageModule like:
        <storageModule ... moduleId="org.eclipse.cdt.core.settings" name="CONFIG_NAME">
    The CONFIG_NAME is what Eclipse headlessbuild -cleanBuild expects after the
    project name prefix (e.g. 'B80_dongle_flash').

    Returns the configs in document order (matches Eclipse UI order).
    Empty list on read/parse failure.
    """
    try:
        text = cp_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Match only the settings storageModule that carries the user-visible config
    # name; other storageModules (cdtBuildSystem, etc.) also have name= attrs but
    # with values like "TC32 Cross Target Application" that are not config names.
    pattern = re.compile(
        r'<storageModule\b[^>]*\bmoduleId="org\.eclipse\.cdt\.core\.settings"[^>]*\bname="([^"]+)"'
    )
    names = pattern.findall(text)
    # Dedupe while preserving order (defensive; normally each config appears once).
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _parse_cproject_configs_with_type(cp_path: Path) -> list[tuple[str, bool]]:
    """Extract (config_name, is_static_lib) for each cconfiguration in a Telink .cproject.

    Pairs each config name from _parse_cproject_configs with a flag indicating
    whether its buildArtefactType is 'staticLib' (vs the default 'app').

    Returns list of (name, is_static_lib) in document order; empty list on failure.
    """
    try:
        text = cp_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Split by <cconfiguration ...> opening tags; each block carries both the
    # settings storageModule (with name=) and the cdtBuildSystem <configuration>
    # element (with buildArtefactType=). Walk the blocks in order.
    blocks = re.split(r"<cconfiguration\b", text)[1:]
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for block in blocks:
        # Take up to the matching </cconfiguration>. Each block ends with it.
        end = block.find("</cconfiguration>")
        block = block if end < 0 else block[:end]
        m_name = re.search(
            r'<storageModule\b[^>]*\bmoduleId="org\.eclipse\.cdt\.core\.settings"[^>]*\bname="([^"]+)"',
            block,
        )
        if not m_name:
            continue
        name = m_name.group(1)
        if not name or name in seen:
            continue
        seen.add(name)
        m_art = re.search(r'buildArtefactType="([^"]+)"', block)
        is_static = bool(m_art and "staticLib" in m_art.group(1))
        out.append((name, is_static))
    return out


def _parse_project_name(project_path: Path) -> str:
    """Extract the Eclipse project name from a .project file.

    The .project file starts with <projectDescription><name>PROJECT_NAME</name>...
    Other <name> tags exist for linked resources / natures, so we anchor on
    <projectDescription> and take the immediately following <name>.

    Returns the project name (e.g. 'B80_Driver_Demo') or '' on failure.
    """
    text = _read(project_path, 1024)  # project name always near the top
    m = re.search(r'<projectDescription>\s*<name>([^<]+)</name>', text)
    if m:
        return m.group(1).strip()
    # Last-resort fallback: first <name> tag in the file head.
    m = re.search(r'<name>([^<]+)</name>', text)
    return m.group(1).strip() if m else ""


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
    # Discover chip dirs under project/tlsr_tc32/* (or nested <sdk>/.../project/tlsr_tc32/*).
    # Each entry is (chip_name, project_rel) where project_rel is the Eclipse
    # project dir relative to SDK root (e.g. 'project/tlsr_tc32/tc122x' or
    # 'telink_b85m_platform_src/project/tlsr_tc32/tc122x').
    chip_entries_raw: list[tuple[str, str]] = []
    for c in ("B80", "B80B", "B85", "B87", "B89", "TC321X", "TC1211", "tc122x"):
        d = root / "project" / "tlsr_tc32" / c
        if d.is_dir():
            chip_entries_raw.append((c, d.relative_to(root).as_posix()))
    # Also search nested (some repos have project/ two levels down)
    if not chip_entries_raw:
        for pd in _find(root, [".cproject"], max_depth=6):
            parts = pd.relative_to(root).parts
            if "project" in parts and "tlsr_tc32" in parts:
                idx = parts.index("tlsr_tc32")
                if idx + 1 < len(parts):
                    chip = parts[idx + 1]
                    proj_dir = pd.parent
                    proj_rel = proj_dir.relative_to(root).as_posix()
                    chip_entries_raw.append((chip, proj_rel))
        # Dedupe by (chip, proj_rel) preserving order
        seen: set[tuple[str, str]] = set()
        deduped: list[tuple[str, str]] = []
        for e in chip_entries_raw:
            if e not in seen:
                seen.add(e)
                deduped.append(e)
        chip_entries_raw = deduped
    default_chip, default_proj_rel = chip_entries_raw[0] if chip_entries_raw else ("B80", f"project/tlsr_tc32/B80")
    # For each chip dir, resolve the real Eclipse project name (from .project)
    # and the real cconfiguration names (from .cproject). Falls back to the
    # legacy hard-coded "<chip>/Release" target when parsing fails.
    chip_entries: list[tuple[str, str, str, list[tuple[str, bool]]]] = []
    for chip, proj_rel in chip_entries_raw:
        cp_dir = root / proj_rel
        cp_path = cp_dir / ".cproject"
        dot_proj = cp_dir / ".project"
        configs_with_type: list[tuple[str, bool]] = []
        if cp_path.is_file():
            configs_with_type = _parse_cproject_configs_with_type(cp_path)
        if not configs_with_type:
            configs_with_type = [("Release", False)]
        proj_name = _parse_project_name(dot_proj) if dot_proj.is_file() else chip
        if not proj_name:
            proj_name = chip
        chip_entries.append((chip, proj_rel, proj_name, configs_with_type))
    default_proj_name, default_configs = (
        (chip_entries[0][2], chip_entries[0][3]) if chip_entries else (default_chip, [("Release", False)])
    )
    default_build_target = f"{default_proj_name}/{default_configs[0][0]}"
    params: list[dict[str, Any]] = [
        {"name": "IdePath", "type": "path", "default": ide_hint, "description": "Telink IoT Studio / Eclipse IDE path"},
        {"name": "ProjectPath", "type": "path", "default": default_proj_rel, "description": "Eclipse project dir relative to SDK root"},
        {"name": "BuildTarget", "type": "string", "default": default_build_target, "description": "Eclipse cleanBuild target: <Project>/<ConfigName>"},
        {"name": "WorkspaceDir", "type": "path", "default": "", "description": "Eclipse workspace dir; empty = script default"},
        {"name": "OutputDir", "type": "path", "default": "build_variants", "description": "Where collected artifacts go"},
    ]
    # Generate one preset per app config (skip staticLib). Preset name is the
    # config name lowercased with non-alphanumerics replaced by '_'.
    presets: list[dict[str, Any]] = []
    for chip, proj_rel, proj_name, configs_with_type in chip_entries:
        for cfg_name, is_static in configs_with_type:
            if is_static:
                continue
            pname = re.sub(r"[^a-z0-9]+", "_", cfg_name.lower()).strip("_")
            presets.append({
                "name": pname,
                "description": f"Build {proj_name} / {cfg_name}",
                "params": {"ProjectPath": proj_rel, "BuildTarget": f"{proj_name}/{cfg_name}"},
            })
    script_rel = compile_bat.relative_to(root).as_posix()
    return {
        "sdk": {"name": root.name, "type": "eclipse", "description": f"Telink SDK (A-type, release_sdk_tool) - {root.name}"},
        "build": {
            "script": {"path": script_rel, "interpreter": "cmd.exe", "execution_policy": "Bypass"},
            "toolchain": {"studio_exe": ide_hint, "eclipse_launcher": launcher, "default_build_target": default_build_target},
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
    Pure Eclipse projects: use the generic cross-platform eclipse_headless_build.py."""
    cprojects = _find(root, [".cproject"], max_depth=6)
    if not cprojects:
        return None
    # For each .cproject: collect (project_name, project_rel, configs[]).
    # project_name comes from the sibling .project file; configs from .cproject.
    entries: list[tuple[str, str, list[str]]] = []
    for cp in cprojects:
        project_rel = cp.parent.relative_to(root).as_posix()
        dot_project = cp.parent / ".project"
        proj_name = _parse_project_name(dot_project) if dot_project.is_file() else cp.parent.name
        configs = _parse_cproject_configs(cp)
        if not configs:
            # Fallback: guess from cconfiguration id (debug/release).
            cp_txt = _read(cp, 8192)
            m = re.search(r'com\.telink\.tc32eclipse\.configuration\.app\.(\w+)\.', cp_txt)
            configs = [m.group(1).capitalize()] if m else ["Release"]
        entries.append((proj_name, project_rel, configs))
    # Dedupe by project_rel (keep first).
    seen: set[str] = set()
    uniq: list[tuple[str, str, list[str]]] = []
    for pn, prel, cfgs in entries:
        if prel not in seen:
            seen.add(prel)
            uniq.append((pn, prel, cfgs))
    entries = uniq
    if not entries:
        return None
    default_proj_name, default_proj_rel, default_configs = entries[0]
    default_cfg = default_configs[0]
    # Generic headless script path (shipped with trae_builder).
    # Resolve relative to this generator file so it works wherever the plugin is installed.
    _here = Path(__file__).resolve().parent
    headless_script = (_here / "scripts" / "eclipse_headless_build.py").as_posix()
    params = [
        {"name": "IdePath", "type": "path", "default": "C:\\TelinkIoTStudio", "description": "Telink IoT Studio / Eclipse IDE directory"},
        {"name": "ProjectPath", "type": "path", "default": default_proj_rel, "description": "Eclipse project dir relative to SDK root"},
        {"name": "BuildTarget", "type": "string", "default": f"{default_proj_name}/{default_cfg}", "description": "Eclipse cleanBuild target: <ProjectName>/<ConfigName>"},
        {"name": "WorkspaceDir", "type": "path", "default": "", "description": "Eclipse workspace dir; empty = auto (../woekspace/<sdkname>)"},
        {"name": "OutputDir", "type": "path", "default": "build_variants", "description": "Where collected artifacts go"},
    ]
    # Generate one preset per (project, config) so all real Eclipse configurations
    # are available. No global cap: a repo with N projects × M configs needs all
    # N*M presets to be usable; truncating hides valid build targets.
    # Only cap per-project configs as a safety net against pathological .cproject
    # files with dozens of configs. Telink config names already encode chip prefix
    # ('B80_dongle_flash', 'B80b_dongle_flash'), so preset name = cfg.lower() is
    # unique across projects; defensive dedup falls back to proj_name prefix.
    MAX_CONFIGS_PER_PROJECT = 20
    presets: list[dict[str, Any]] = []
    used_preset_names: set[str] = set()
    for proj_name, prel, cfgs in entries:
        for cfg in cfgs[:MAX_CONFIGS_PER_PROJECT]:
            # Preset name: lowercased config name (already encodes chip prefix in
            # Telink SDKs: 'B80_dongle_flash', 'B80b_dongle_flash', etc.).
            pname = cfg.lower().replace(" ", "_").replace("/", "_")
            # Defensive dedup: if collision across projects, prefix with proj_name.
            if pname in used_preset_names:
                pname = f"{proj_name.lower()}_{pname}".replace(" ", "_").replace("/", "_")
            used_preset_names.add(pname)
            presets.append({
                "name": pname,
                "description": f"Build {proj_name} / {cfg} ({prel})",
                "params": {"ProjectPath": prel, "BuildTarget": f"{proj_name}/{cfg}"},
            })
    return {
        "sdk": {"name": root.name, "type": "eclipse", "description": f"Telink SDK (Eclipse projects, no release_sdk_tool) - {root.name}"},
        "build": {
            "script": {"path": headless_script, "interpreter": "", "execution_policy": "Bypass"},
            "toolchain": {"studio_exe": "C:\\TelinkIoTStudio", "eclipse_launcher": "TelinkIoTStudio.exe", "default_build_target": f"{default_proj_name}/{default_cfg}"},
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
            "script": {"path": "REPLACE_WITH_BUILD_SCRIPT", "interpreter": ""},
            "parameters": [
                {"name": "OutputDir", "type": "path", "default": "build", "description": "Artifacts dir"},
            ],
            "presets": [{"name": "default", "description": "Default build", "params": {}}],
            "artifacts": {"scan_dirs": ["build"], "name_pattern": "*.bin", "max_age_hours": 168},
            "timeout_seconds": 900,
        },
    }


def _detect_serial(root: Path) -> dict[str, Any] | None:
    """Detect serial-port usage hints in a repo and return a `serial` config fragment.

    Heuristics (read-only):
    - Telink SDKs (any type already detected) flash/printf over UART; default
      baud 115200 for logs, no fixed port (auto-detect at capture time).
    - If a repo contains Python files importing pyserial/serial.Serial, treat as
      serial-using and use 115200 default.
    - Repos with tools/flash*.py or RF_Tools/tlsr_tool references also count.
    - Otherwise return None (no serial section emitted; the schema section is optional).
    """
    is_telink = _is_telink_repo(root)
    has_serial_code = _has_serial_usage(root)
    if not (is_telink or has_serial_code):
        return None
    cfg: dict[str, Any] = {
        "default_port": "",
        "baud": 115200,
        "timeout_seconds": 60,
        "encoding": "utf-8",
    }
    return cfg


# Default bdt.exe path on Windows (Telink IoT Studio install).
_DEFAULT_BDT_PATH = r"E:\TelinkIoTStudio\tools\libusbBDT\bin\bdt.exe"

# Repo chip-dir name -> bdt chip prefix (bdt expects UPPER).
_DEFAULT_CHIP_MAP: dict[str, str] = {
    "B80": "B80", "B80B": "B80B", "B85": "B85", "B87": "B87", "B89": "B89",
    "B91": "B91", "B92": "B92",
    "TC1211": "TC1211", "TC321X": "TC321X", "tc122x": "TC122X", "tc123x": "TC123X",
    "TL321X": "TL321X", "TL721X": "TL721X", "TL751X": "TL751X", "TL322X": "TL322X",
}


def _detect_flash(root: Path) -> dict[str, Any] | None:
    """Detect Telink bdt.exe flashing defaults for a repo.

    Only emits a flash section for Telink repos (those with telink_ble/ or
    tlsr_tc32/ dirs, or a release_sdk_tool marker). Sets bdt_path only if the
    conventional Windows path exists on this host; otherwise leaves it empty
    (runner has its own default). Infers default_chip from the repo's chip dirs
    or presets when possible.
    """
    if not _is_telink_repo(root):
        return None
    bdt = _DEFAULT_BDT_PATH if Path(_DEFAULT_BDT_PATH).is_file() else ""
    # Try to infer a default chip from project/tlsr_tc32/<CHIP>/ dirs.
    chip = ""
    for d in root.rglob("tlsr_tc32"):
        if not d.is_dir():
            continue
        rel = d.relative_to(root)
        if len(rel.parts) > 4:
            continue
        for child in d.iterdir():
            if child.is_dir() and child.name in _DEFAULT_CHIP_MAP:
                chip = _DEFAULT_CHIP_MAP[child.name]
                break
        if chip:
            break
    cfg: dict[str, Any] = {
        "default_chip": chip,
        "default_command": "wf",
        "default_address": 0,
        "timeout_seconds": 120,
        "reset_after_flash": True,
    }
    if bdt:
        cfg["bdt_path"] = bdt
    return cfg


def _is_telink_repo(root: Path) -> bool:
    """True if the repo looks like a Telink SDK (has telink_ble/ or tlsr_tc32/ dirs or release_sdk_tool)."""
    # Directories (telink_ble, tlsr_tc32 are dirs, not files) - check existence at depth<=4.
    for dirpath in root.rglob("*"):
        if not dirpath.is_dir():
            continue
        rel = dirpath.relative_to(root)
        if len(rel.parts) > 4:
            continue
        name = dirpath.name.lower()
        if name in ("telink_ble", "tlsr_tc32"):
            return True
    # release_sdk_tool marker files (A-type).
    if _find(root, ["releasesdk.bat", "rleasesdk.bat"], max_depth=4):
        return True
    return False


def _has_serial_usage(root: Path) -> bool:
    """True if any Python file in the repo imports pyserial / opens a serial port."""
    pys = list(root.rglob("*.py"))
    for p in pys[:200]:  # cap scan for speed
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "import serial" in txt or "serial.Serial" in txt or "pyserial" in txt:
            return True
    return False


def _platform_finalize(cfg: dict[str, Any], detector_name: str) -> None:
    """Adjust generated config for the current OS.

    On non-Windows: Telink A/B-type build scripts are .bat (Windows-only); fall back
    to the generic cross-platform eclipse_headless_build.py. Make-type and generic
    scripts that are already cross-platform (.py/.sh/makefile) are left as-is.
    Windows configs are returned unchanged (Telink .bat + IDE .exe work natively).
    """
    if os.name == "nt":
        return  # Windows: keep Telink .bat / IDE .exe as detected.
    build = cfg.get("build", {})
    script = build.get("script", {})
    path = script.get("path", "")
    is_windows_only = path.lower().endswith(".bat")
    sdk_type = cfg.get("sdk", {}).get("type", "")
    # For eclipse-type repos with Windows-only scripts, switch to the generic headless python script.
    if is_windows_only and sdk_type == "eclipse":
        _here = Path(__file__).resolve().parent
        headless = (_here / "scripts" / "eclipse_headless_build.py").as_posix()
        script["path"] = headless
        script["interpreter"] = ""  # runner auto-selects python
        # IDE launcher name without .exe on Linux/macOS.
        tc = build.get("toolchain", {})
        if tc.get("eclipse_launcher") and tc["eclipse_launcher"].lower().endswith(".exe"):
            tc["eclipse_launcher"] = tc["eclipse_launcher"][:-4]
        # IdePath default: keep user's hint but it must point to a Linux Eclipse install; leave as-is (user edits).
    # For makefile-type: interpreter 'make' is fine on Linux.
    # For .bat generic scripts (non-eclipse) on Linux: leave but warn via description.
    elif is_windows_only:
        cfg.setdefault("sdk", {})["description"] = (cfg.get("sdk", {}).get("description", "") +
            " [WARNING: detected .bat script is Windows-only; on Linux, point build.script.path to a .sh/.py script]")


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
            _platform_finalize(cfg, name)
            cfg["schema_version"] = SCHEMA_VERSION
            serial_cfg = _detect_serial(root)
            if serial_cfg:
                cfg["serial"] = serial_cfg
            flash_cfg = _detect_flash(root)
            if flash_cfg:
                cfg["flash"] = flash_cfg
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
