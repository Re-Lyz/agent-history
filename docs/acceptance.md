# 功能覆盖与干净系统验收

合成验证使用生成的数据，不提交个人转录、认证或数据库。

| 场景 | 自动验证入口 | 边界 |
|---|---|---|
| 首次／重复安装、配置、Markdown、升级、卸载保留数据 | first_use_check.py、test_p1_features.py | 临时用户不等于全新 OS |
| 单 Agent、缺失程序、中文／特殊路径、自定义命令 | test_setup_settings.py、test_launch_backend.py、test_launch_jobs.py | 不代替真实登录 |
| 同连接、跨连接、Claude 转换与接续 | test_single_conversation.py、test_handoff_lifecycle.py、verify_*.py | 合成会话，原生检查零模型请求 |
| 配置损坏、恢复失败回退、预览过期、活跃任务阻止 | test_app_maintenance.py | 不操作个人配置或原生数据库 |
| 安装中途失败后的登记撤销，保留其他集成 | test_app_maintenance.py | 系统任务失败保留可重试日志 |
| 未接管取消、原生切出、迟到任务保护 | test_single_conversation.py、test_history_ui.py | 写锁场景不能代替真实 TUI 的 /resume 全流程 |
| 不同工作目录、无 Tk 后端、旧模块入口 | test_module_boundaries.py、test_app_maintenance.py、完整回归 | 保留旧入口、非零退出码与导入身份；检查便携包和独立 runner |
| 真实 Linux 终端、中文参数、退出报告、无桌面 CLI | linux_terminal_check.py | CI 中实际运行 xterm，使用合成记录程序 |
| 真实 systemd timer、失败状态、重试和卸载 | systemd_check.py | CI 中创建独立临时用户并启动真实用户级管理器，不运行 Agent |

## 本机验证记录

2026-10-22：Windows / Python 3.12 完整回归 249 项（1 项 Linux 专用检查跳过），六项 Codex 0.162.1 原生检查通过，零模型请求。模块拆分后的便携包与临时用户安装／升级／卸载演练通过。

本次拆分代码的 [Windows / Ubuntu 22.04 CI](https://github.com/Re-Lyz/agent-history/actions/runs/39719705194) 已通过，公开验收分支为 validation/remaining-todo-20261022，提交 06f0adf。Linux 完整回归 249 项（7 项平台相关检查跳过），并通过实际 xterm 的参数／环境／目录／退出码检查及无桌面 CLI。独立临时 Linux 用户的真实 systemd timer 已验证定时触发、服务失败、重试恢复、重复安装、卸载及数据保留。双平台同时通过便携包、临时用户首次使用及六项 Codex 0.159.2 原生检查。该分支尚未合入 main。

Codex 0.162.1 的真实 TUI 空闲切换已通过本机及 [Windows / Ubuntu CI](https://github.com/Re-Lyz/agent-history/actions/runs/39741047257)（04e243c）：在同一终端输入 `/resume <会话 ID>` 后读出目标历史，终端保持运行，旧会话写锁仍被持有；`/quit` 正常退出后两条会话的锁均释放，原有用户与助手消息完整保留。`verify_tui_resume.py` 使用隔离配置和合成历史，模型请求为零；双平台完整回归、原生兼容、打包与隐私审计同时通过。不代表已验收选择器操作、登录后的模型回合或后台任务。

## 仍未验收的真实环境

- [ ] 干净 Windows 系统：安装依赖、真实登录、新建／恢复、可选集成、升级和卸载。
- [ ] 干净 Linux 桌面与无桌面环境：安装依赖、真实登录、新建／恢复、可选集成、升级和卸载；无桌面 CLI、xterm 和真实 systemd 调度的 CI 验收已完成。
- [ ] 真实 Claude CLI 登录和交互接续；JSONL 和格式转换不代表完成此项。
- [ ] Codex TUI 选择器操作、真实模型回合和后台任务运行期间的 /resume 切换及退出；空闲按 ID 切换已单独验证。

在独立环境按 first-use.md 安装，用专门测试账号及合成内容，记录 OS、Python、Tk、Agent 版本和各步骤结果。证据使用脱敏诊断，不包含认证、真实对话或私人路径。核对卸载后数据仍在、第三方修改保留。仅将实际完成的系统与版本列入支持范围。
