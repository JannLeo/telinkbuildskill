#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generic Eclipse CDT headless build script (cross-platform).

Invokes an Eclipse-based IDE (Telink IoT Studio / Eclipse) in headless mode
to clean-build a project configuration, then collects the newest .bin artifact.

Cross-platform replacement for eclipse_headless_build.ps1. Pure stdlib.

CLI args (named, passed by trae_build_runner.py as -Name value):
  -IdePath       IDE directory containing the launcher exe/bin (required)
  -ProjectPath   Eclipse project dir, absolute or relative to SDK root (required)
  -BuildTarget   <ProjectName>/<ConfigName> for -cleanBuild (required)
  -WorkspaceDir  Eclipse -data dir; empty = auto (../woekspace/<sdkname>)
  -OutputDir     where to copy the artifact; empty = <sdkroot>/build_variants
  -OutputName    output base name without extension; empty = auto
  -SdkRoot       SDK root; empty = cwd

On Windows looks for TelinkIoTStudio.exe then eclipse.exe; on Linux/macOS
looks for eclipse / TelinkIoTStudio. The first existing launcher is used.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def log(msg: str) -> None:
    print(msg, flush=True)


def find_launcher(ide_dir: Path) -> Path | None:
    is_win = os.name == "nt"
    names = (
        ["TelinkIoTStudio.exe", "eclipse.exe", "TelinkIoTStudio"]
        if is_win
        else ["eclipse", "TelinkIoTStudio", "TelinkIoTStudio.exe"]
    )
    for n in names:
        p = ide_dir / n
        if p.is_file():
            return p
        # Also allow a direct path (ide_dir itself being the launcher).
    if ide_dir.is_file():
        return ide_dir
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Eclipse headless build (cross-platform)")
    # trae_build_runner passes -Name value (PowerShell-style); accept both -Name and --Name.
    def add(name: str, **kw):
        ap.add_argument(f"-{name}", f"--{name}", dest=name, default=kw.get("default", ""), **{k: v for k, v in kw.items() if k != "default"})
    ap.add_argument("-IdePath", "--IdePath", dest="IdePath", required=True)
    ap.add_argument("-ProjectPath", "--ProjectPath", dest="ProjectPath", required=True)
    ap.add_argument("-BuildTarget", "--BuildTarget", dest="BuildTarget", required=True)
    ap.add_argument("-WorkspaceDir", "--WorkspaceDir", dest="WorkspaceDir", default="")
    ap.add_argument("-OutputDir", "--OutputDir", dest="OutputDir", default="")
    ap.add_argument("-OutputName", "--OutputName", dest="OutputName", default="")
    ap.add_argument("-SdkRoot", "--SdkRoot", dest="SdkRoot", default="")
    args, _unknown = ap.parse_known_args(argv)
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    sdk_root = Path(args.SdkRoot).resolve() if args.SdkRoot else Path(os.getcwd()).resolve()
    ide_dir = Path(args.IdePath).resolve()
    launcher = find_launcher(ide_dir)

    # Resolve project path (absolute or relative to sdk_root).
    proj = Path(args.ProjectPath)
    if not proj.is_absolute():
        proj = (sdk_root / proj).resolve()
    if not proj.exists():
        log(f"ERROR: project path not found: {proj}")
        return 2

    # Workspace dir.
    if args.WorkspaceDir:
        ws = Path(args.WorkspaceDir).resolve()
    else:
        sdk_name = Path(sdk_root).name
        ws = (sdk_root.parent / "woekspace" / sdk_name).resolve()
    ws.mkdir(parents=True, exist_ok=True)

    # Output dir.
    out_dir = Path(args.OutputDir).resolve() if args.OutputDir else (sdk_root / "build_variants")
    out_dir.mkdir(parents=True, exist_ok=True)

    build_target = args.BuildTarget
    build_log = sdk_root / "eclipse_headless_build.log"
    build_start = time.time() - 3  # small backdate for artifact mtime filter

    status = 0
    ran = False
    if launcher:
        ran = True
        cmd = [
            str(launcher),
            "--launcher.suppressErrors",
            "-nosplash",
            "-application", "org.eclipse.cdt.managedbuilder.core.headlessbuild",
            "-data", str(ws),
            "-import", str(proj),
            "-cleanBuild", build_target,
        ]
        log("Running Eclipse headless build:")
        log(f"  launcher: {launcher}")
        log(f"  -data:    {ws}")
        log(f"  -import:  {proj}")
        log(f"  -cleanBuild: {build_target}")
        log(f"  log:      {build_log}")
        try:
            with build_log.open("w", encoding="utf-8", errors="replace") as lf:
                proc = subprocess.run(cmd, cwd=str(sdk_root), stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                lf.write(proc.stdout or "")
                print(proc.stdout or "", flush=True)
            status = proc.returncode
        except FileNotFoundError as e:
            log(f"ERROR launching: {e}")
            return 1
        except subprocess.TimeoutExpired:
            log("ERROR: build timed out")
            return 1
    else:
        log(f"WARNING: no launcher found under {ide_dir}")
        log("Set IdePath to the directory containing TelinkIoTStudio.exe / eclipse.")
        return 1

    # Collect newest .bin under project dir.
    copied = None
    if status == 0:
        bins = [p for p in proj.rglob("*.bin") if p.is_file()]
        # Prefer those newer than build_start.
        fresh = [b for b in bins if b.stat().st_mtime >= build_start]
        candidates = sorted(fresh or bins, key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            src = candidates[0]
            ts = time.strftime("%Y%m%d_%H%M%S")
            proj_name = proj.name
            base = args.OutputName or f"{proj_name}_{build_target.split('/', 1)[-1]}_{ts}"
            base = base.replace("/", "_").replace("\\", "_")
            dst = out_dir / (base + ".bin")
            shutil.copy2(src, dst)
            copied = dst
            rel = str(dst.relative_to(sdk_root)) if str(sdk_root) in str(dst) else str(dst)
            log(f"copied bin: {rel} ({dst.stat().st_size} bytes)")
        else:
            log(f"WARNING: build succeeded, but no .bin found under {proj}")

    log("")
    log("Eclipse headless build summary")
    log(f"  project: {proj}")
    log(f"  target:  {build_target}")
    log(f"  exit:    {status}")
    log(f"  build:   {'success' if status == 0 else ('failed' if ran else 'launcher-not-found')}")
    if status != 0 and ran:
        log(f"  see log: {build_log}")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
