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

## 本机验证记录

2026-10-22：Windows / Python 3.12 完整回归 249 项（1 项 Linux 专用检查跳过），六项 Codex 0.162.1 原生检查通过，零模型请求。模块拆分后的便携包与临时用户安装／升级／卸载演练通过。前一版公开提交的 Windows / Ubuntu CI 已通过；本次拆分的新代码尚需 Linux 复验。当前本机 WSL 只有 Python 3.8.10 且缺少 Tk，不满足 Python 3.11+ 的要求。

## 仍未验收的真实环境

- [ ] 干净 Windows 系统：安装依赖、真实登录、新建／恢复、可选集成、升级和卸载。
- [ ] 干净 Linux 桌面与无桌面环境：上述流程，以及真实用户级 systemd timer 的触发、失败和撤销。
- [ ] 真实 Claude CLI 登录和交互接续；JSONL 和格式转换不代表完成此项。
- [ ] 当前 Codex TUI 同终端 /resume 后旧会话写锁、后台任务及终端生命周期；分别检查锁释放和仍被持有的情况。

在独立环境按 first-use.md 安装，用专门测试账号及合成内容，记录 OS、Python、Tk、Agent 版本和各步骤结果。证据使用脱敏诊断，不包含认证、真实对话或私人路径。核对卸载后数据仍在、第三方修改保留。仅将实际完成的系统与版本列入支持范围。
