---
description: 用 trae-builder 编译当前 SDK 仓库（按预设或参数）。例如 /build rx_default 或 /build build --param Target=tx
argument-hint: <preset|build|info|list> [args...]
---

使用通用构建运行器处理当前工作区 `${workspaceFolder}` 的 SDK 仓库（其根目录需含 builder.json 或 robin_builder.json）。

调用 `${COCO_PLUGIN_ROOT}/trae_build_runner.py` 完成实际编译，不要自己拼命令。根据用户参数 `$ARGUMENTS` 选择子命令：

- 若参数为空或以 `info` 开头：执行 `info` 子命令，展示该仓库的构建配置（参数、预设、产物配置）。
- 若参数以 `list` 开头：执行 `list` 子命令，列出已收集的构建产物。
- 若参数以 `build` 开头：把 `$ARGUMENTS` 里 `build` 之后的剩余内容原样作为子命令参数传给 runner（支持 `--preset <名称>`、`--param 键=值`、`--timeout 秒数`、`--dry-run`）。
- 若参数以非上述关键字开头（如 `rx_default`）：视为预设名，执行 `build --preset <第一个参数>`，其后的参数继续透传。

runner 路径固定为：`${COCO_PLUGIN_ROOT}/trae_build_runner.py`
项目路径用 `--project "${workspaceFolder}"`。

请按上述规则构造一条 python 命令并用 Bash 工具执行，然后把 runner 的 JSON 输出解析后向用户汇报：
- 编译是否成功（status 字段）
- 产物路径和大小（artifacts 数组）
- 若 dry_run，展示将要执行的完整命令
- 若失败，展示 exit_code 和日志路径

下面是 runner 用法速查（供你构造命令参考）：

```
python "${COCO_PLUGIN_ROOT}/trae_build_runner.py" --project "<项目路径>" info
python "${COCO_PLUGIN_ROOT}/trae_build_runner.py" --project "<项目路径>" list
python "${COCO_PLUGIN_ROOT}/trae_build_runner.py" --project "<项目路径>" build --preset <名称> [--param 键=值 ...] [--timeout 秒] [--dry-run]
```

注意：`$ARGUMENTS` 为用户在 `/build` 后输入的全部参数。请正确拆分出子命令与透传部分。
