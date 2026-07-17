# Telink SDK Builder

English | [简体中文](README.md)

A generic, SDK-agnostic build orchestrator that lets AI coding assistants (Trae / Claude Desktop / Cursor / Cline) compile any SDK repository's build script via natural language.

Packaged as a **TraeCLI plugin** (skill + slash commands + MCP server). The **MCP server uses the standard protocol**, so any MCP-capable client can connect. Also usable as a plain CLI, with no AI tool required.

![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue)
![python](https://img.shields.io/badge/python-3.8%2B-green)
![license](https://img.shields.io/badge/license-MIT-lightgrey)
![mcp](https://img.shields.io/badge/MCP-stdio-orange)

## Install

**Trae CLI:**
```bash
trae-cli plugin marketplace add git@github.com:JannLeo/telinksdk-builder-mcp.git
trae-cli plugin install telinksdk-builder
```

**Other MCP clients (Claude Desktop / Cursor / Cline):** see [Using with non-Trae tools](#using-with-non-trae-tools) below.

## Demo

Open any Telink SDK repo in Trae and just say:

```
> /build-init            # scan the repo, auto-generate builder.json
> /build b80_dongle_flash  # build the B80 dongle flash firmware
```

**`/build-init` output** (auto-detect, generate config):
```text
Detected: Telink eclipse-only (C-type)
Build script: scripts/eclipse_headless_build.py
IDE path: C:\TelinkIoTStudio
Presets (25, one per real Eclipse config):
  - 8366_dongle -> 8366_dongle_for_8373_km/8366_dongle
  - b80_dongle_flash -> B80_Driver_Demo/B80_dongle_flash
  - b80_dongle_otp -> B80_Driver_Demo/B80_dongle_otp
  - b80b_mouse_flash_sram -> B80B_Driver_Demo/B80b_mouse_flash_sram
  - lark_dongle_demo -> TC_PLATFORM_SDK_1211/Lark_Dongle_Demo
  ... (full coverage, no truncation)
```

**`/build b80_dongle_flash` output** (real build, artifact returned):
```text
✓ build: success (exit=0, 42s)
  copied bin: build_variants/B80_Driver_Demo_b80_dongle_flash_20260717.bin (17400 bytes)
```

## Components

| File | Purpose |
|------|---------|
| `trae_build_runner.py` | Generic build runner. Reads `builder.json` (or legacy `robin_builder.json`) from the project root, invokes the configured build script with presets/params, collects artifacts. Usable standalone from CLI. |
| `trae_build_mcp.py` | MCP stdio server (zero deps, pure Python stdlib). Exposes 6 tools for any MCP client. Delegates to the runner. |
| `trae_build_init.py` | Generator that auto-detects the build pattern of any repo and emits a `builder.json`. |
| `trae_builder_schema.json` | JSON Schema for `builder.json` (validation / IDE completion). |

## How it works

1. Place a `builder.json` in the SDK repo root declaring: build script path, parameters, presets, artifacts directory.
2. `trae_build_mcp.py` is spawned via stdio by the MCP client and auto-discovers the `builder.json` in the current workspace.
3. In the AI conversation, say "build rx_default preset"; the agent calls the `build_run` tool, the runner generates and executes the command, and scanned artifacts are returned.

Switching repos needs no tool changes — the same toolchain adapts to any repo that has a `builder.json`.

## MCP tools exposed

| Tool | Description |
|------|-------------|
| `build_info` | Show the project's build config (params, presets, artifacts, toolchain). No build performed. |
| `build_presets` | List named presets defined in `builder.json`. |
| `build_run` | Run a build by preset and/or param overrides; `dry_run` prints the command without executing. |
| `build_list` | List collected build artifacts (filtered by `artifacts.scan_dirs` and `max_age_hours`). |
| `serial_list` | List available serial ports on this machine (COMx on Windows, /dev/tty* on Linux/macOS). Requires pyserial. |
| `serial_capture` | Open a serial port and capture output (default 5s / 200 lines) to verify firmware behavior after flashing. Port/baud default to the `builder.json` `serial` section; auto-picks the first port if none given. The raw log is returned for the agent to judge whether the firmware behaves correctly (e.g. `boot ok` / version string present). Requires pyserial. |

## Generating builder.json for any repo: `/build-init`

For repos without a `builder.json`, run `/build-init` to auto-scan and generate one:

```
/build-init                       # scan current repo, generate builder.json
/build-init --ide C:\TelinkIoTStudio   # override IDE path
```

Detected patterns:

| Pattern | Detection | Generated |
|---------|-----------|-----------|
| **Telink A-type** (release_sdk_tool) | finds `tools/release_sdk_tool/compile.bat` | IDE path, chip list (B80/B80B/tc122x etc.), one preset per chip |
| **Telink B-type** (telink_ble post-build) | finds `rom_lib.bat`/`flash_on_rom_lib.bat` + `.cproject` | extracts build config name and chip from `.cproject` |
| **Telink C-type** (pure Eclipse projects) | `.cproject` present but none of the above | extracts real project names from `.project`, all configs from `.cproject`, one preset per project×config, uses cross-platform `eclipse_headless_build.py` |
| **Makefile** | finds `Makefile`/`makefile` | parses targets, one preset per target |
| **Generic script** | finds `build.ps1`/`build.bat`/`build.sh` at root | basic config |
| **fallback template** | none of the above | prompts user to fill `build.script.path` |

## Cross-platform (Windows / Linux / macOS)

- **runner / MCP server / generator**: pure Python stdlib, works on all three.
- **C-type build script**: `scripts/eclipse_headless_build.py` (pure Python) cross-platform; finds `TelinkIoTStudio.exe`/`eclipse.exe` on Windows, `eclipse`/`TelinkIoTStudio` on Linux/macOS.
- **A/B-type**: on Windows uses the repo's native `.bat` (preserves Telink multi-stage orchestration); on Linux/macOS the generator auto-switches the build script to the cross-platform `eclipse_headless_build.py` and drops `.exe` from the launcher name.
- **runner auto-selects interpreter**: `.py`→python/python3, `.sh`→bash, `.ps1`→powershell/pwsh, `.bat`→cmd.exe.
- **Requirements**: Python 3.8+; for Eclipse headless builds, the platform-appropriate Telink IoT Studio / Eclipse CDT.

## Using with non-Trae tools

The MCP server (`trae_build_mcp.py`) speaks the **standard MCP stdio protocol** and does not depend on Trae. Any MCP-capable client can connect and gets the `build_info` / `build_presets` / `build_run` / `build_list` tools.

Clone the repo and note its path (referred to as `<BUILDER>` below).

### Claude Desktop

Edit the config file:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "trae_builder": {
      "command": "python",
      "args": ["<BUILDER>/trae_build_mcp.py"]
    }
  }
}
```

### Cursor

Create `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "trae_builder": {
      "command": "python",
      "args": ["<BUILDER>/trae_build_mcp.py"]
    }
  }
}
```

### VSCode (Cline / Continue / other MCP extensions)

Add to the extension's MCP settings (e.g. `~/.cline/mcp_settings.json`):

```json
{
  "mcpServers": {
    "trae_builder": {
      "command": "python",
      "args": ["<BUILDER>/trae_build_mcp.py"]
    }
  }
}
```

### Any MCP client (generic)

stdio launch command: `python <BUILDER>/trae_build_mcp.py` (no args). The server auto-discovers `builder.json` in the current workspace.

> Note: non-Trae clients don't get the `/build`, `/build-init` commands and the build skill (those are Trae-specific formats), but the MCP tools work fully. To generate a `builder.json` for an SDK repo the first time, use the CLI below.

### Plain CLI (no AI client needed)

```bash
# Generate builder.json (scan repo, auto-detect build pattern)
python <BUILDER>/trae_build_init.py /path/to/sdk-repo

# Show config
python <BUILDER>/trae_build_runner.py --project /path/to/sdk-repo info

# Build by preset
python <BUILDER>/trae_build_runner.py --project /path/to/sdk-repo build --preset rx_default

# List artifacts
python <BUILDER>/trae_build_runner.py --project /path/to/sdk-repo list

# List serial ports (requires pyserial)
python <BUILDER>/trae_build_runner.py --project /path/to/sdk-repo serial list

# Capture serial output for 5 seconds (port/baud default to builder.json 'serial' section; empty = auto-pick first)
python <BUILDER>/trae_build_runner.py --project /path/to/sdk-repo serial capture --port COM3 --baud 115200 --duration 5
```

## Requirements

- **Python 3.8+**
- **Build features**: zero third-party deps (pure stdlib)
- **Serial capture**: optional dependency `pyserial` (`pip install pyserial`); serial_list/serial_capture give a friendly install hint if missing
- **Eclipse headless builds**: the platform-appropriate Telink IoT Studio / Eclipse CDT

## Self-test

```bash
# Feed JSON-RPC directly to the MCP server (no Trae needed)
python <BUILDER>/trae_build_mcp.py < <BUILDER>/scripts/_mcp_probe_in.json
```

Verified: initialize / tools/list / build_info / build_presets / build_run (dry-run) / build_list / serial_list / shutdown all return correctly.

## Contributing & spreading the word

Issues and PRs welcome. If you find it useful, a ⭐ helps others discover it.

**Suggested GitHub Topics** (improves discoverability):
`mcp` `mcp-server` `trae-plugin` `embedded` `firmware` `eclipse-cdt` `telink` `sdk-builder` `cross-platform` `ai-coding`

## License

MIT
