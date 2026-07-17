# Telink SDK Builder

[English](README.en.md) | 简体中文

通用、SDK 无关的构建编排工具,让 AI 编程助手(Trae / Claude Desktop / Cursor / Cline 等)通过自然语言调用任意 SDK 仓库的构建脚本。

打包为 **TraeCLI plugin**(含 skill + slash command + MCP server),同时其 **MCP server 是标准协议**,任何支持 MCP 的客户端都能直接接入。也可纯命令行使用,不依赖任何 AI 工具。

![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue)
![python](https://img.shields.io/badge/python-3.8%2B-green)
![license](https://img.shields.io/badge/license-MIT-lightgrey)
![mcp](https://img.shields.io/badge/MCP-stdio-orange)

## 一键安装

**Trae CLI:**
```bash
trae-cli plugin marketplace add git@github.com:JannLeo/telinksdk-builder-mcp.git
trae-cli plugin install telinksdk-builder
```

**其他 MCP 客户端(Claude Desktop / Cursor / Cline):** 见下方[给非 Trae 工具用](#给非-trae-工具用claude-desktop--cursor--cline--命令行)。

## Demo

在 Trae 里打开任意 Telink SDK 仓库,对话直接说:

```
> /build-init            # 一键扫描仓库,自动生成 builder.json
> /build b80_dongle_flash  # 编译 B80 dongle flash 固件
```

**`/build-init` 输出**(自动检测,生成配置):
```text
检测结果:Telink eclipse-only (C-type)
构建脚本:scripts/eclipse_headless_build.py
IDE 路径:C:\TelinkIoTStudio
预设(25 个,每个对应真实 Eclipse 配置):
  - 8366_dongle -> 8366_dongle_for_8373_km/8366_dongle
  - b80_dongle_flash -> B80_Driver_Demo/B80_dongle_flash
  - b80_dongle_otp -> B80_Driver_Demo/B80_dongle_otp
  - b80b_mouse_flash_sram -> B80B_Driver_Demo/B80b_mouse_flash_sram
  - lark_dongle_demo -> TC_PLATFORM_SDK_1211/Lark_Dongle_Demo
  ... (全覆盖,无截断)
```

**`/build b80_dongle_flash` 输出**(真实编译,产物回传):
```text
✓ build: success (exit=0, 42s)
  copied bin: build_variants/B80_Driver_Demo_b80_dongle_flash_20260717.bin (17400 bytes)
```

**`/build info` 输出**(查看配置):
```text
项目:telink_8373_kmd_2.4g_mode_sdk (eclipse)
配置文件:builder.json
参数(5):IdePath / ProjectPath / BuildTarget / WorkspaceDir / OutputDir
预设(25):8366_dongle, b80_dongle_flash, ... , lark_dongle_startup_from_boot
工具链:TelinkIoTStudio.exe, 默认目标 8366_dongle_for_8373_km/8366_dongle
```

## 组成

| 文件 | 作用 |
|------|------|
| `trae_build_runner.py` | 通用构建运行器。读取项目根的 `builder.json`(或旧名 `robin_builder.json`),按 preset / 参数调用配置中指定的构建脚本,收集产物。可独立命令行使用。 |
| `trae_build_mcp.py` | MCP stdio server(零依赖,纯 Python stdlib)。暴露 4 个工具,供任意 MCP 客户端调用。内部委托给 runner。 |
| `trae_builder_schema.json` | `builder.json` 的 JSON Schema,可用于校验/IDE 补全。 |

## 工作原理

1. 任意 SDK 仓库根放一个 `builder.json`,声明:构建脚本路径、参数列表、预设、产物目录。
2. `trae_build_mcp.py` 被 Trae 以 stdio 方式拉起,自动发现当前工作区的 `builder.json`。
3. 在 Trae 对话里说"用 rx_default 预设编译",agent 调用 `build_run` 工具,runner 据此生成命令并执行构建脚本,产物扫描后回传。

换仓库时只要新仓库自带 `builder.json`,同一份 `trae_builder` 工具链直接复用,无需改动。

## 暴露的 MCP 工具

| 工具 | 说明 |
|------|------|
| `build_info` | 显示当前项目的构建配置(参数、预设、产物配置、工具链)。不执行编译。 |
| `build_presets` | 列出 `builder.json` 里定义的预设。 |
| `build_run` | 执行一次构建。可传 `preset` 和/或 `params` 覆盖;`dry_run` 只打印命令不执行。 |
| `build_list` | 列出已收集的构建产物(按 `artifacts.scan_dirs` 和 `max_age_hours` 过滤)。 |
| `serial_list` | 列出本机可用串口(Windows COMx / Linux/macOS /dev/tty*)。需 pyserial。 |
| `serial_capture` | 抓取串口输出(默认 5 秒/200 行),用于烧录后验证固件行为。端口/波特率默认取 `builder.json` 的 `serial` 段;留空自动选第一个串口。agent 拿到日志原文自行判断"功能对不对"(如有没有 `boot ok`/版本号)。需 pyserial。 |

## 命令行直接使用(不经过 Trae)

```powershell
# 查看某项目的构建配置
python D:\work\workspace\trae_builder\trae_build_runner.py --project D:\work\workspace\tc_ble_lite_sdk-1.2.2_allinone info

# 按预设编译(真实执行)
python D:\work\workspace\trae_builder\trae_build_runner.py --project <SDK路径> build --preset rx_default

# 自定义参数 + 超时
python D:\work\workspace\trae_builder\trae_build_runner.py --project <SDK路径> build --param Target=tx --param RomType=txrx --timeout 1200

# 仅打印命令不执行
python D:\work\workspace\trae_builder\trae_build_runner.py --project <SDK路径> build --preset rx_default --dry-run

# 列出产物
python D:\work\workspace\trae_builder\trae_build_runner.py --project <SDK路径> list

# 列出串口(需 pyserial)
python D:\work\workspace\trae_builder\trae_build_runner.py --project <SDK路径> serial list

# 抓取串口输出 5 秒(端口/波特率默认取 builder.json 的 serial 段,留空自动选第一个)
python D:\work\workspace\trae_builder\trae_build_runner.py --project <SDK路径> serial capture --port COM3 --baud 115200 --duration 5
```

## 依赖

- **Python 3.8+**
- **构建功能**:零第三方依赖(纯 stdlib)
- **串口抓取**:可选依赖 `pyserial`(`pip install pyserial`),未装时 serial_list/serial_capture 会给出友好提示
- **Eclipse headless 编译**:需对应平台的 Telink IoT Studio / Eclipse CDT

## 注册到 Trae CLI

在 `~/.trae/trae_cli.yaml`(用户级,对所有项目生效)添加:

```yaml
mcp_servers:
  - name: trae_builder
    type: stdio
    command: python
    args:
      - D:\work\workspace\trae_builder\trae_build_mcp.py
    timeout: 30s
```

或在项目根放 `.mcp.json`(项目级):

```json
{
  "mcpServers": {
    "trae_builder": {
      "type": "stdio",
      "command": "python",
      "args": ["D:\\work\\workspace\\trae_builder\\trae_build_mcp.py"],
      "timeout": 30
    }
  }
}
```

注册后用 `trae-cli doctor` 检查连接状态;在交互模式用 `/mcp` 查看详情和工具列表。

> **关于 MCP 工具的可见性**:stdio MCP server 是异步加载,会话启动的 `init` 事件里 `mcp_servers` 可能为空(此时握手尚未完成),但通常 1 秒内握手完成,工具即可被 agent 调用。若首轮 prompt 调用 MCP 工具偶发失败,重试或等待片刻即可(详见 trae-cli 文档「首轮调用 MCP 工具失败/第二轮自动恢复」)。本工具链已在 `tc_ble_lite_sdk-1.2.2_allinone` 上实测 `build_info` 与 `build_run` 均可正常返回。

## 在 Trae 里直接编译: `/build` 自定义命令

除 MCP 外,还提供了 `/build` 自定义 prompt command(走 Bash 工具调 runner,不依赖 MCP),作为更直观的入口。

命令文件已放在 `~/.trae/commands/build.md`(用户级,对所有项目生效)。在任意含 `builder.json` 的仓库根目录打开 Trae,输入:

```
/build info              # 查看该仓库的构建配置(参数/预设/产物)
/build list              # 列出已收集的构建产物
/build rx_default        # 按预设 rx_default 编译(真实执行)
/build rx_default --dry-run   # 预演,只打印命令不编译
/build build --preset rx_default --param Target=tx   # 显式 build 子命令 + 覆盖参数
```

`/build` 会自动用 `${workspaceFolder}` 定位当前仓库,调用 `trae_build_runner.py`,并把结果整理后汇报。

> Windows 终端注意:在 `cmd.exe` 里用 `trae-cli -p "/build ..."` 带空格参数可能被截断;推荐在 VSCode 集成终端( PowerShell )里直接输入 `/build ...`,或用 `trae-cli -p` 时整体加引号并在 PowerShell 下运行。

## 让旧仓库接入: `/build-init` 自动生成 builder.json

旧仓库没有 `builder.json` 时,用 `/build-init` 一键扫描并生成:

```
/build-init                       # 扫描当前仓库,自动生成 builder.json
/build-init --ide C:\TelinkIoTStudio   # 指定 IDE 路径覆盖默认值
```

`/build-init` 调用 `trae_build_init.py`,自动识别常见构建模式:

| 模式 | 识别方式 | 生成内容 |
|------|---------|---------|
| **Telink A 型**(release_sdk_tool) | 找 `tools/release_sdk_tool/compile.bat` | IDE 路径、芯片列表(B80/B80B/tc122x 等)、每芯片一个预设 |
| **Telink B 型**(telink_ble 后处理) | 找 `rom_lib.bat`/`flash_on_rom_lib.bat` + `.cproject` | 从 `.cproject` 提取构建配置名、芯片 |
| **Telink C 型**(纯 Eclipse 工程) | 有 `.cproject` 但无上述脚本 | 从 `.project` 提取真实项目名、`.cproject` 提取所有配置,每工程×配置一个预设,用跨平台 `eclipse_headless_build.py` |
| **Makefile** | 找 `Makefile`/`makefile` | 解析出 target 列表,每个 target 一个预设 |
| **通用脚本** | 找根目录 `build.ps1`/`build.bat`/`build.sh` | 基础配置 |
| **fallback 模板** | 以上均未命中 | 提示用户手填 `build.script.path` |

## 跨平台支持(Windows / Linux / macOS)

- **runner / MCP server / 生成器**:纯 Python stdlib,三平台通用。
- **C 型构建脚本**:`scripts/eclipse_headless_build.py`(纯 Python)三平台通用;在 Windows 找 `TelinkIoTStudio.exe`/`eclipse.exe`,Linux/macOS 找 `eclipse`/`TelinkIoTStudio`。
- **A/B 型**:Windows 下用仓库自带的 `.bat`(保留 Telink 多阶段编排);Linux/macOS 下生成器会自动把构建脚本切换到跨平台的 `eclipse_headless_build.py`,并把 IDE 启动器名去掉 `.exe`。
- **runner 自动选解释器**:`.py`→python/python3,`.sh`→bash,`.ps1`→powershell/pwsh,`.bat`→cmd.exe。
- **依赖**:仅需 Python 3.8+;Eclipse headless 编译还需对应平台的 Telink IoT Studio / Eclipse CDT。


命令文件在 `~/.trae/commands/build-init.md`。也可命令行直接用:

```powershell
python D:\work\workspace\trae_builder\trae_build_init.py <仓库路径> --dry-run   # 预览不写
python D:\work\workspace\trae_builder\trae_build_init.py <仓库路径>              # 写入 builder.json
python D:\work\workspace\trae_builder\trae_build_init.py <仓库路径> --ide C:\TelinkIoTStudio
```

生成后即可 `/build info` 查看、`/build <预设名>` 编译。若是 fallback 模板,按提示编辑 `builder.json` 补全 `build.script.path` 即可。

## 手动编写 builder.json

也可手写 `builder.json`,参考 `trae_builder_schema.json` 或现有 `tc_ble_lite_sdk-1.2.2_allinone/robin_builder.json`。最小示例:

```json
{
  "schema_version": "1.0",
  "sdk": { "name": "my-sdk", "type": "make" },
  "build": {
    "script": { "path": "build.ps1", "interpreter": "powershell.exe" },
    "parameters": [
      { "name": "Target", "type": "enum", "enum": ["debug","release"], "default": "release" }
    ],
    "presets": [
      { "name": "dbg", "params": { "Target": "debug" } }
    ],
    "artifacts": { "scan_dirs": ["build"], "name_pattern": "*.bin", "max_age_hours": 168 },
    "timeout_seconds": 600
  }
}
```

## 给非 Trae 工具用(Claude Desktop / Cursor / Cline / 命令行)

本仓库的 MCP server(`trae_build_mcp.py`)是**标准 MCP stdio 协议**,不依赖 Trae。任何支持 MCP 的客户端都能接入,同样获得 `build_info` / `build_presets` / `build_run` / `build_list` 四个工具。

先 clone 仓库(或下载),记下路径(下面用 `<BUILDER>` 代指 clone 后的 `telinkbuildskill` 目录)。

### Claude Desktop

编辑配置文件:
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

重启 Claude Desktop,对话框会出现 `build_info` 等工具。

### Cursor

在项目根创建 `.cursor/mcp.json`:

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

### VSCode(Cline / Continue 等 MCP 扩展)

以 Cline 为例,在其 MCP 设置(通常 `~/.cline/mcp_settings.json` 或扩展设置)加:

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

### 任何 MCP 客户端(通用)

stdio 启动命令:`python <BUILDER>/trae_build_mcp.py`,无参数。server 自动发现当前工作区的 `builder.json`。

> 注意:非 Trae 客户端没有 `/build`、`/build-init` 命令和 build skill(那些是 Trae 专用格式),但 MCP 工具完全可用。首次为一个 SDK 仓库生成 `builder.json`,用下面的命令行方式。

### 纯命令行(不依赖任何 AI 客户端)

```bash
# 生成 builder.json(扫描仓库,自动检测构建模式)
python <BUILDER>/trae_build_init.py /path/to/sdk-repo

# 查看配置
python <BUILDER>/trae_build_runner.py --project /path/to/sdk-repo info

# 按预设编译
python <BUILDER>/trae_build_runner.py --project /path/to/sdk-repo build --preset rx_default

# 列出产物
python <BUILDER>/trae_build_runner.py --project /path/to/sdk-repo list
```

## 自测

```powershell
# 直接喂 JSON-RPC 测 MCP server(不依赖 Trae)
python <BUILDER>/trae_build_mcp.py < <BUILDER>/scripts/_mcp_probe_in.json
```

已验证:initialize / tools/list / build_info / build_presets / build_run(dry-run) / build_list / shutdown 全部正常返回。

## 贡献与推广

欢迎 issue / PR。如果你觉得有用,给个 ⭐ 让更多人看到。

**建议在 GitHub 仓库 Settings 里添加 Topics**(提升被搜索到的概率):
`mcp` `mcp-server` `trae-plugin` `embedded` `firmware` `eclipse-cdt` `telink` `sdk-builder` `cross-platform` `ai-coding`

**分享到社区时可用:**
- Trae / Claude / MCP 社区(Discord、GitHub Discussions)
- 嵌入式开发论坛(说明:让 AI 直接编译 Telink/嵌入式 SDK 固件)
- 掘金/知乎等技术博客(配 demo 截图/录屏效果最佳)
