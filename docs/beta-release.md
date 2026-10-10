# 0.1.0-beta.1 发行说明与验证

2026-10-10。源码公开于 [Re-Lyz/agent-history](https://github.com/Re-Lyz/agent-history)，发行版本 [v0.1.0-beta.1](https://github.com/Re-Lyz/agent-history/releases/tag/v0.1.0-beta.1)。按用户最终选择采用 MIT 协议，允许商业及非商业使用、修改和分发，保留版权与许可声明。

## 发行内容

源码 ZIP 包含代码、LICENSE／NOTICE、双语 README、安装入口、通用模板、文档和合成测试；不包含 Python、Agent CLI、依赖轮子、登录凭据、实际历史或数据库。需要 Python 3.11+、Tk、SQLite FTS5/trigram。ZIP 解压目录应保留，安装器入口指向它。完整功能见 [README](../README.md)。

## 内容审计与公开 Git 仓库

实施开始时扫描原开发仓库全部 23 个可达提交、244 个不同历史 blob，发现 HELP.md 的个人用户路径；tests/test_ui_support.py 的密码命中为已核实的合成字符串 `never persist`。当前 HELP 与 README 已换为通用说明；截图使用生成历史，裁掉包含临时路径的启动预览。未发现扫描规则能识别的真实凭据、转录或数据库；这不构成对所有任意文字或未知凭据格式的保证。

原开发历史保留本地，不直接发布。`integrations/prepare_public.py` 从 Git 索引中的源码生成新的公开 Git 快照，不改写原仓库；公开仓库从一条 beta 初始提交开始。公开候选及源码 ZIP 必须重新通过审计。检查全部可达对象、当前追踪文件、常见凭据、私人用户路径、运行文件、非预期二进制与不安全 ZIP 路径，只输出位置／类型，不输出秘密原文。PNG 按生成脚本及人工视觉检查确认。

```text
python tests/release_audit.py --output dist/local-history-audit.json
python integrations/prepare_public.py --output dist/public-beta
python tests/release_audit.py --root dist/public-beta --artifact dist/agent-history-0.1.0-beta.1.zip --output dist/public-content-audit.json
```

原开发仓库历史扫描因旧路径应保持失败；公开快照才是拟上传对象。报告数字是基线，新提交会增加数量。未来开发应在选定公开仓库中继续，避免每次发行重置历史。

## 复现发行验证

在新建虚拟环境安装 requirements-ui.txt，暂存审查过的文件，从 Git 索引构建（不是打包任意工作目录）：

```text
git add .
python integrations/build_portable.py --output dist/agent-history-0.1.0-beta.1.zip
python tests/portable_check.py dist/agent-history-0.1.0-beta.1.zip
python tests/first_use_check.py dist/agent-history-0.1.0-beta.1.zip
python -m unittest discover -s tests -t . -q
```

Linux 无桌面测试用 xvfb-run 包装真实 Tk 场景。first_use_check 从 ZIP 解压，用独立临时用户执行：预检、基础／重复安装、来源发现、首次配置、真实 Markdown 阅读、可选 Codex／Claude MCP 与 Hook、源码移动后的升级、卸载、源历史／认证／数据库保留。它不登录或启动真实 Agent、不测试系统任务调度，也不等于重装 Windows／Linux；真实 OS 装机仍保留在 OSS-08。

原生兼容另跑 tests/verify_ui_resume.py、verify_claude_to_codex.py、verify_handoff.py 和 verify_temporary_handoff.py，使用合成会话、临时 home，检查 list／resume／内容读回／重启等，零模型请求。真实 Linux 终端和无桌面 CLI 由 linux_terminal_check.py 单独验证。见 [测试说明](../tests/README.md)。

## 兼容边界与发布条件

本轮验证结果（2026-10-10）：

| 检查 | 结果 |
|---|---|
| Windows 新 venv，Python 3.12，markdown-it-py 4.2.0／Pygments 2.21.0／mdurl 0.1.2 | 159 项回归，跳过 1 项 Linux 专用检查，其余通过 |
| Ubuntu 20.04／WSLg 新 venv，Python 3.12.2，同上依赖版本 | 159 项回归，跳过 6 项 Windows 专用检查，其余通过 |
| 双平台源码 ZIP 解压／校验和／编译／预检 | 通过 |
| 双平台临时用户首次使用／Markdown／可选集成／移动源码升级／卸载保留数据 | 通过；没有真实 Agent 登录和新装 OS |
| 双平台四项 Codex CLI 0.159.2 隔离原生检查 | list／resume／读回／重启、转换和临时回收通过，零模型请求；Windows 进程保护检查需 CIM 权限 |
| Linux 真实缓存 xterm | 中文／特殊参数、工作目录、环境、退出码 23、安装卸载及无桌面 CLI 通过 |
| 公开候选全部可达历史／当前文件／beta ZIP | 扫描零命中；截图人工检查；本地开发旧历史仍有 4 处 HELP 私人路径，不拟公开 |
| README、许可证和发行说明的本地链接 | 通过 |
| GitHub-hosted windows-latest／Ubuntu 22.04 CI | 159 项回归（平台专用项跳过）、源码打包／审计、临时用户首次使用、四项原生 Codex 检查全部通过，见下方运行链接 |
| 全新 OS 装机／真实 Claude CLI 交互 | 尚未完成 |

已实测 Windows／Python 3.12 和 Ubuntu 20.04／WSL2／WSLg／Python 3.12.2／xterm，Codex CLI 0.159.2。Python 3.11 是最低要求，本地完整回归使用 3.12。Claude CLI 实际交互恢复、其他 Linux 发行版／终端、原生 Wayland、真实 systemd 调度和 macOS 尚未验收。界面中文，英文 README 不代表英文 UI。

跨 Agent 仅迁移文字和工具摘要，省略图片、附件、隐藏推理和执行状态，不重放工具；工具全文读取与摘要检索范围不同。外部资源依赖原路径，加密记录可能无法跨服务解码。Codex 内部存储格式升级后先隔离验证。交付是源码 ZIP，需要 Python。

Actions 已完成双平台全流程，实际证据为 [运行 38031469719](https://github.com/Re-Lyz/agent-history/actions/runs/38031469719)，验证的代码提交为 `bf7741ffb42af0ac02b5acc88f6a4f575bf2885c`。首次云端运行修复了临时目录的 Windows 短／长路径比较、数百文件索引测试等待时间，以及 npm codex.cmd 的版本报告解析；保留全部功能断言。后续发行记录提交只改文档，使用 skip-ci 标记避免重复功能测试；源码／发行包内容审计与打包另行通过。

公开仓库已启用私密漏洞报告。Release 提供源码 ZIP 和 SHA-256；仍需 Python／Tk，不能当作免运行环境的 EXE 使用。原本沙箱内的 gh 登录状态误报已由联网检查纠正。原开发历史未推送，公开仓库仅包含审计后的源码历史。
