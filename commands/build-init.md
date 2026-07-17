---
description: 为当前仓库自动生成 builder.json，让 trae-builder 能编译它（扫描构建脚本/IDE/芯片，输出配置）
argument-hint: [--ide <IDE路径>]
---

使用生成器 `${COCO_PLUGIN_ROOT}/trae_build_init.py` 为当前工作区 `${workspaceFolder}` 生成 `builder.json`，使该仓库接入 trae-builder 构建体系。

用户参数 `$ARGUMENTS`：
- 默认（空）：扫描 `${workspaceFolder}` 并生成 `builder.json`。
- 若含 `--ide <路径>`：用该 IDE 路径覆盖检测到的默认值。

执行步骤：
1. 用 Bash 工具运行：
   `python "${COCO_PLUGIN_ROOT}/trae_build_init.py" "${workspaceFolder}" $ARGUMENTS`
2. 读取生成的 `${workspaceFolder}\builder.json`（用 Read 工具）。
3. 向用户汇报：
   - 检测器命中的模式（A-type release_sdk_tool / B-type telink_ble post-build / C-type eclipse-only / makefile / generic / fallback 模板）
   - 构建脚本路径、解释器
   - IDE 路径、芯片、默认构建目标
   - 生成的预设列表
4. 若是 fallback 模板（`build.script.path` 为 `REPLACE_WITH_BUILD_SCRIPT`），明确告诉用户：未自动识别出构建脚本，需要手动编辑 `builder.json` 的 `build.script.path` 指向实际的构建脚本（.bat/.ps1/Makefile），并补全参数。
5. 提醒用户：生成后即可用 `/build info` 查看配置、`/build <预设名>` 编译；如需调整，直接编辑 `builder.json`。

注意：
- `${workspaceFolder}` 是当前工作区根目录。
- 生成器会覆盖已存在的 `builder.json`；若已存在 `robin_builder.json` 会优先复用其内容并规范化。
- 不要自行编造构建脚本路径，一切以生成器输出为准。
