# 基础模块边界

history_app 是应用服务包，根目录保留既有入口和兼容导入。

| 位置 | 职责 |
|---|---|
| history_app/paths.py、locks.py | 明确传递运行路径，提供 Windows / POSIX 文件锁 |
| history_app/history_queries.py | 历史筛选、项目列表、恢复任务定位及纯文本预览数据，不加载 Tk |
| history_app/configuration.py、config_backup.py | 配置验证、不可变配置快照、预览和恢复 |
| history_app/diagnostics.py | 只读诊断及白名单字段导出 |
| history_app/installer.py、installation.py | 安装归属登记，带预览和备份的更新／卸载 |
| history_app/platform/terminal.py | 平台终端发现和参数处理 |
| history_app/ui/ | 维护窗口、共享文本控件、工具详情及 Markdown 渲染 |
| agent_history.py、conversation_store.py | 采集检索、逻辑关系和不可变修订存储 |
| launch_backend.py、single_conversation.py、handoff_lifecycle.py | 原生适配、预约和跨平台接续收尾 |
| launch_job_runner.py、launch_jobs.py | 独立任务生命周期和原生接管观察 |

视图调用服务，服务和平台模块不导入桌面窗口。MCP、历史 CLI、安装和维护后端不依赖 Tk 加载；安装预检仍会检查桌面依赖。路径不依赖启动时的工作目录。

install.py、ui_widgets.py、tool_panel.py、markdown_preview.py 和 terminal_platform.py 保持原导入身份的兼容桥接，现有调用和模块替换继续生效。history_ui.py 保留主窗口协调及兼容查询入口。主控制器和其他旧后端可继续分步拆分，本轮未改写全部存储接口。

配置恢复不恢复数据库。维护和启动准备共用采集锁；原生文件仍由 Agent 写锁保护，终端 PID 不能代替原生会话状态。
