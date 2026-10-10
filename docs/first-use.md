# 首次安装与使用

本工具管理本机 Codex／Claude Code 历史，在独立终端中执行所选 Agent。只使用一个 Agent、只有一个账号也可以使用；不需要预先配置 codex1、多个连接或个人专用目录。

## 1. 准备 Python 和源码

需要 Python 3.11+，并包含 Tk、SQLite FTS5。Windows 推荐 Python 官方安装程序并启用 Tcl/Tk；Linux 使用发行版提供的 Python 和 Tk 包，或自己已有的兼容环境。核心 CLI 不依赖 Markdown；桌面安装会检查以下依赖。

克隆仓库或解压便携源码包，进入解压后的 agent-history 文件夹，使用准备运行本工具的同一个 Python：

```text
python -m pip install -r requirements-ui.txt
python install.py --dry-run
python install.py
```

Linux 的发行版 Python 如禁止全局 pip 安装，先创建虚拟环境，然后使用该环境的 python；Tk 通常需由系统包提供。安装器实际运行所选 Python 检查版本和依赖，因此 Windows 商店占位入口不能通过预检。可用 `--python 完整路径` 指定解释器。

默认基础安装只创建查看器入口，不注册 MCP、Hook 或后台任务。Windows 创建开始菜单的 Agent History 快捷方式；`--desktop` 额外创建桌面快捷方式。Linux 创建应用菜单的 Agent History 和安装目录中的 codexui 脚本；有桌面时从应用菜单打开，或执行脚本。

默认安装目录为用户目录下 `.local/share/agent-history`，可通过 `--prefix 自定义目录` 改变。源码包需要 Python，不能作为免运行环境的单文件程序使用。安装后的入口指向这份源码，解压目录应保留；移动源码后重新运行安装器更新入口。

## 2. 安装和登录自己使用的 Agent

Codex／Claude Code 的 CLI 安装、服务配置和登录由各自官方程序完成，本工具不代为安装或登录。先在终端中确认所需的 `codex --version`／`claude --version` 能运行，再启动一次所用 Agent 完成配置。无需安装未使用的另一个 Agent。

如果程序尚未安装，可以先打开查看器并接入已有历史。启动入口会说明缺少程序／配置的原因，历史阅读不受影响。程序不在 PATH 中时，在“启动配置”新建 Codex／Claude 类型入口，指定程序完整路径；Codex 类型还需关联接入后的连接。

Linux 应使用 Linux 原生程序或具有执行权限的脚本，不能使用 Windows 的 cmd／ps1 包装入口。Shell 函数或别名不能作为可执行程序发现；请创建脚本，显式加载自己的环境后用 exec 调用 Agent，并在启动配置中选择该脚本。

## 3. 首次打开，选择历史来源

首次配置向导会发现已有 Codex home、Claude home 和已注册连接。仅选自己需要的目录，修改显示名称后点击“保存并索引历史”。未发现自定义位置时，用“添加目录”手动选择。

- 仅 Codex：通常选择 `.codex` 或实际 CODEX_HOME；向导直接接入现有 config.toml 和历史，保留认证文件。
- 仅 Claude：选择 `.claude` 或自己的 Claude home，其中应包含 projects 子目录。
- 尚无历史：可以保存空来源或稍后配置。先用 Agent 创建对话，再回到“历史来源”添加目录并刷新。
- 多账号／自定义目录：逐个添加对应的 home，以显示名称区分；不需要复制密码或认证文件到仓库。

配置、偏好和数据库保存在仓库外。安装目录中的 config 是默认来源配置目录，历史数据库默认为 `~/.agent-history/history.db`。也可通过 `--shared-root` 复用已有来源配置；AGENT_HISTORY_HOME 可改变数据目录。

## 4. 阅读和第一次接续

左栏搜索、筛选并选择对话；中栏显示 Markdown，可切换原文、搜索并加载更早消息。全局全文命中后会定位到消息，工具详情点标题展开，独立滚动完整原记录。工具索引只保留摘要；原文件缺失或变化时会提示刷新。

F2 或右栏“修改对话名称”可设置逻辑对话的名称，空名称恢复自动标题。名称用于本工具的列表、搜索和排序，跨平台版本共享名称；不改写 Codex／Claude 原生标题或历史正文。

右栏选平台版本、启动入口和工作目录，检查环境并核对预览，再点击“打开终端并继续”。同连接恢复所选原生 ID；跨连接／跨 Agent 自动准备临时目标，终端结束后持久保存增量并回收临时目标。版本之间的并行分支需明确选择。登录和恢复是否成功以终端提示为准。

Claude 对话通常必须在原项目目录恢复；不存在的目录请先恢复路径或使用支持转换的目标。Windows 使用 PowerShell／Windows Terminal，Linux 自动寻找桌面终端；无桌面环境只能使用适用的历史 CLI。

## 5. 需要时再启用集成

安装前用 --dry-run 查看将修改的路径。功能可以分别选择，并可重复运行安装器：

```text
python install.py --codex-mcp
python install.py --claude-mcp
python install.py --claude-hook
python install.py --collect-task
python install.py --sync-task
```

MCP 为 Agent 提供历史查询。Claude Hook 在启动时调用 Claude 同步；collect-task 每 5 分钟采集，并沿用已启用连接的 Codex 同步策略；sync-task 每 60 分钟调用 Claude 同步。后台同步会生成原生导入副本，建议先确认来源范围再启用。Linux 任务使用用户级 systemd，需要可用的 systemctl --user；不修改 shell 启动文件。

重复安装不会重复登记同一功能。installation.json 记录已完成步骤，部分失败时可修复错误后重试。用户修改过的快捷入口不会被覆盖。各 Agent 的其他 MCP 和配置会保留。

## 6. 更新、迁移和卸载

源码更新后重开 UI；长期运行的 MCP 进程也需重启。安装路径或 Python 改变时重新运行安装器。使用此前个人配置的用户可加 `--shared-root 现有配置目录`，继续使用自己的快捷命令；基础安装不创建或替换 codex1 等个人命令。

```text
python install.py --prefix 原安装目录 --uninstall --dry-run
python install.py --prefix 原安装目录 --uninstall
```

卸载只撤销未被用户修改的登记，包括快捷入口、已登记的可选集成和任务。用户修改过的项目会保留并列在 kept 中；Claude SessionStart 列表有后续修改时整项保留供核对。历史、来源配置、备份和安装记录保留，源码可在核对后自行移走。不要用原生数据库旧备份覆盖已有新对话的数据库。

详细平台范围和验证记录见 docs/linux.md 和 docs/p1-verification.md。
