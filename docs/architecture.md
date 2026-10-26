# 模块边界与兼容入口

程序与工作台扩展：programs.py 统一程序解析；accounts.py 管理只读清单和可选额度缓存；program_maintenance.py、release_install.py、compatibility.py 分别负责审阅更新、校验发行回退和隔离兼容记录。workspace.py 复用既有索引／原生分页形成项目视图与带字节来源的材料，ui/workspace.py 和 ui/maintenance.py 提供界面。MCP 项目上下文入口使用只读数据库，不触发采集；运行策略与记录均在私人数据目录。

history_app 按职责组织应用实现。根目录脚本保留已安装快捷方式、MCP、旧任务记录和第三方导入使用的路径。

| 位置 | 职责 |
|---|---|
| paths.py、locks.py | 源码根目录、运行数据位置和 Windows / POSIX 文件锁 |
| storage/history.py | 历史采集、检索、CLI 与 MCP 请求处理 |
| storage/revisions.py、merge.py | 逻辑会话、不可变修订、显式分支合并 |
| adapters/codex_rpc.py、codex.py、claude.py | Codex 原生传输／索引与 Claude 格式转换 |
| launch/planning.py | 启动计划、命令和所选物理会话读取 |
| launch/jobs.py、job_runner.py、runner.py | 独立进程、任务状态、退出码和接管观察 |
| launch/reservations.py、handoff.py | 会话预约、写锁检查、接续与收尾 |
| history_queries.py | 列表筛选、项目列表及预览数据准备 |
| configuration.py、config_backup.py | 配置验证、不可变配置快照、预览和恢复 |
| diagnostics.py、installer.py、installation.py | 只读诊断、安装归属及维护操作 |
| platform/terminal.py | 平台终端发现和参数处理 |
| ui/application.py | 主窗口生命周期、外观和控制器组合 |
| ui/layout.py、listing.py、reader.py | 控件布局、列表状态、正文分批渲染和阅读位置 |
| ui/workers.py、launch.py、recycling.py | 后台工作队列、启动交互和回收站操作 |
| ui/maintenance.py、widgets.py、tool_panel.py、markdown.py | 维护窗口及共享显示组件 |

UI 控制器在创建窗口时接收桌面依赖和兼容绑定，运行中的队列、选择、渲染序号及任务保存在窗口实例中。无需向新模块注入全局 Tk 对象；原先替换 history_ui 的读取／启动函数的集成继续生效。主窗口不再包含列表、正文、启动和回收站的全部实现。

根目录旧模块与对应包模块具有相同导入身份，避免配置或测试替换产生两份状态。旧的 agent_history.ROOT、DATA 默认值继续兼容；它们仍属于单进程运行上下文，并不承诺同一进程可同时管理两个不同数据根。维护服务使用显式 RuntimePaths。

源码入口通过 paths.SOURCE 定位；禁止从后端模块的 __file__ 推断根目录脚本。独立任务仍执行根目录 launch_job_runner.py，旧任务内已保存的绝对入口路径保持有效。根脚本保留退出码、UTF-8 输出和错误处理。

后端可在没有 Tk 的进程中导入；桌面组件在桌面启动时加载。配置恢复不恢复数据库。维护和启动准备共用采集锁，原生文件由 Agent 写锁保护，终端 PID 不能代替原生会话状态。

模块导入顺序、入口的非零退出码、自定义数据目录及其他工作目录启动由 test_module_boundaries.py 验证。UI、独立 runner、原生接续、解压安装和卸载使用合成数据回归。
