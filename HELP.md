# 命令与高级连接帮助

基础安装不创建个人快捷命令。从源码目录运行 `python agent_history.py --help`、`python trash.py --help`、`python launch_profiles.py --help` 和 `python install.py --help` 获取完整参数。默认 CLI 配置读取仓库上级目录；用 `--shared-root 配置目录` 指向安装向导写入的来源配置。

```text
python agent_history.py --shared-root 配置目录 collect
python agent_history.py --shared-root 配置目录 sessions
python agent_history.py --shared-root 配置目录 search "关键词"
python agent_history.py --shared-root 配置目录 import-codex 会话ID --to 目标连接 --dry-run
python trash.py list
python launch_profiles.py list
```

先检查预览和目标 home，再执行导入。程序的 `resume --all` 可以列出其他工作目录的记录；仍遵循原生 CLI 的筛选与版本行为。

环境诊断和配置维护使用 `python maintenance.py diagnose`、`backup`、`backups`、`restore-config`、`upgrade` 和 `uninstall`。恢复／更新／卸载默认仅预览，显式 `--apply` 才执行；旧的修订存储维护入口保持兼容。详见 [维护说明](docs/maintenance.md)。

## 已有 Windows 快捷入口

旧版个人包装脚本可能定义 `codex1`、`codex2`、`codexc`、`codexh` 或 `codexui`。这些是用户自己配置的函数／脚本，不是基础安装的必备命令，不保证克隆本项目后就存在；名称、连接映射和登录位置可自行配置。现有脚本可调用仓库的 `launch.ps1`，其参数见脚本的 `-Help`。

例如已经配置好的 `codex2 -From 1 -Session 会话ID` 会从映射的来源连接准备独立原生副本，并在目标连接恢复；同连接直接用原 ID。`-PrepareOnly` 预览，`-NoSync` 本次跳过批量同步，`-SyncHistory` 主动同步。`-Session` 接受完整 ID 或 latest，不接受选择列表里的序号。

自动批量同步仅对启用 `sync_history` 的连接生效；向导接入已有 home 默认关闭它。当前个人入口不需要迁移，但公共示例不能预设其存在。通用配置见 [首次使用](docs/first-use.md)、[启动配置](docs/launch-profiles.md) 和 `templates/` 中的示例。示例中的连接名称为占位名，运行配置应保存在仓库外。
