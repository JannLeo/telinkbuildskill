---
name: build
description: 编译当前 SDK 仓库的固件。用 trae-builder 读取 builder.json，按预设或参数执行构建脚本并收集产物。当用户要求"编译/构建/烧录固件""build firmware""用某个预设编译"时自动调用。
---

# build skill

本 skill 指导你使用 trae-builder 插件编译当前 SDK 仓库的固件。

## 前提

当前工作区 `${workspaceFolder}` 根目录需存在 `builder.json` 或 `robin_builder.json`。若不存在，先建议用户运行 `/build-init` 生成。

## 核心入口

构建运行器位于 `${COCO_PLUGIN_ROOT}/trae_build_runner.py`，用 Python 调用。**不要自己拼编译命令**，一律通过 runner。

## 子命令

### info — 查看配置
```
python "${COCO_PLUGIN_ROOT}/trae_build_runner.py" --project "${workspaceFolder}" info
```
返回 JSON：sdk 信息、parameters、presets、artifacts 配置、toolchain、script。

### list — 列出产物
```
python "${COCO_PLUGIN_ROOT}/trae_build_runner.py" --project "${workspaceFolder}" list
```
返回 artifacts 数组（path、size、mtime），按修改时间倒序。

### build — 执行编译
```
python "${COCO_PLUGIN_ROOT}/trae_build_runner.py" --project "${workspaceFolder}" build --preset <预设名> [--param 键=值 ...] [--timeout 秒] [--dry-run]
```
- `--preset`：用 builder.json 中定义的预设名（如 `rx_default`、`b80_release`）。
- `--param 键=值`：覆盖单个参数，可多次。键须匹配 builder.json 的 parameters。
- `--timeout`：覆盖超时秒数。
- `--dry-run`：只打印将要执行的命令，不真正编译。

返回 JSON 含：status（success/failed/timeout/dry-run/error）、exit_code、elapsed_seconds、artifacts、command。

## 判断用户意图

用户说"编译/构建/build"且当前仓库有 builder.json 时，主动用 build 子命令：
- 用户给了预设名（如"用 rx_default 编译"）→ `build --preset rx_default`
- 用户给了参数（如"Target 设成 tx"）→ `build --param Target=tx`（可叠加预设）
- 用户只想看配置/产物 → 用 info / list
- 用户不确定有哪些预设 → 先跑 info 列出 presets，让用户选

## 汇报规范

- 成功：status、产物路径+大小、耗时
- dry-run：展示完整命令，提醒去掉 --dry-run 即可真编译
- 失败：exit_code、日志路径（robin_build_variant.log 或 eclipse_headless_build.log）
- 超时：提示增大 --timeout

## 多芯片仓库

部分 Telink 仓库有多个 Eclipse 工程（B80/B80B/TC1211 等），builder.json 会为每个工程生成一个预设。用户说"编译 B80 固件"时，选匹配芯片名的预设。

## 注意

- `${COCO_PLUGIN_ROOT}` 是插件安装目录，runner 在其中。
- `${workspaceFolder}` 是当前打开的工作区根。
- runner 输出是 JSON，解析后向用户汇报，不要原样堆 JSON。
- 编译可能耗时较长（Telink IDE headless 几十秒到几分钟），用 Bash 工具执行时确保超时充足（builder.json 的 timeout_seconds，默认 900-1200）。
