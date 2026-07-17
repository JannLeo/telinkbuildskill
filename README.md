# Trae Builder

通用、SDK 无关的构建编排工具,让 Trae CLI / VSCode Trae 插件能通过自然语言调用任意 SDK 仓库的构建脚本。

## 组成

| 文件 | 作用 |
|------|------|
| `trae_build_runner.py` | 通用构建运行器。读取项目根的 `builder.json`(或旧名 `robin_builder.json`),按 preset / 参数调用配置中指定的构建脚本,收集产物。可独立命令行使用。 |
| `trae_build_mcp.py` | MCP stdio server(零依赖,纯 Python stdlib)。暴露 4 个工具,供 Trae 会话调用。内部委托给 runner。 |
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
```

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
| **Telink C 型**(纯 Eclipse 工程) | 有 `.cproject` 但无上述脚本 | 扫描所有 `.cproject`,每工程一个预设,用通用 `eclipse_headless_build.ps1` |
| **Makefile** | 找 `Makefile`/`makefile` | 解析出 target 列表,每个 target 一个预设 |
| **通用脚本** | 找根目录 `build.ps1`/`build.bat`/`build.sh` | 基础配置 |
| **fallback 模板** | 以上均未命中 | 提示用户手填 `build.script.path` |

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

## 自测

```powershell
# 直接喂 JSON-RPC 测 MCP server(不依赖 Trae)
python D:\work\workspace\trae_builder\trae_build_mcp.py < D:\work\workspace\trae_builder\scripts\_mcp_probe_in.json
```

已验证:initialize / tools/list / build_info / build_presets / build_run(dry-run) / build_list / shutdown 全部正常返回。
