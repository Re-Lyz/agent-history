# 测试与原生兼容验证

程序与工作台：test_program_maintenance.py、test_release_install.py、test_workspace.py 覆盖稳定／显式路径、npm 归属、脚本不自动执行、写锁／过期预览、分账户失败／重试、固定包、SHA 拒绝、并发入口和旧 release 保留，以及项目精确范围、字节溯源、游标、重写、缺失／停用来源和只读 MCP。真实 Tk 验证清单、脱敏复制、更新预览、固定账户、兼容按钮与项目历史筛选；维护恢复验证旧配置快照保留新偏好，额度缓存验证不保存凭据。界面测试不执行真实更新、登录或模型请求。

`test_scoped_collection.py` 验证指定路径采集不遍历无关目录，遵守来源和目录边界，支持归档与 Claude 项目、超过 SQLite 单批参数限制的列表，以及删除后不复活文件。单主会话测试还覆盖索引失败后的预约保留／重试和同 ID 副本的重新索引。

`python tests/benchmark_sync.py --files 400 --repeat 3` 用临时合成历史测量单条同连接会话的退出同步，不启动 Agent、不访问私人数据。2026-10-23 本机 Windows / Python 3.12 基准中，精确路径更新前后中位耗时约 0.230／0.022 秒，文件头读取 1210／8 次。它测量小会话周围有无关历史时的开销，不代表大正文校验、跨连接原生恢复或用户完整历史库的耗时，也不作为固定时限的 CI 测试。

`test_module_boundaries.py` 在新进程中验证根入口与包模块的导入身份、无 Tk 后端、中文自定义目录和独立 runner 的真实非零退出码。可执行入口从源码根目录定位，不从实现模块的 `__file__` 推断。

`systemd_check.py` 仅用于有真实用户级 systemd 管理器的临时 Linux 测试用户，要求显式设置 `AGENT_HISTORY_DISPOSABLE_USER=1`。它验证实际定时触发、服务失败及重试、重复安装和卸载保留数据；CI 自动创建该用户，不应在日常用户下运行。

`verify_tui_resume.py` 使用真实终端、临时会话、独立配置和拒绝模型请求的本机 provider，验证空闲时 `/resume <会话 ID>` 切换、终端保持运行、正常 `/quit` 后释放写锁及原消息保留。Windows 需要 pywinpty，Linux 需要 pexpect；双平台 CI 使用 Codex 0.162.1。它记录切换后旧锁是否仍被持有，不强行释放；不涵盖选择器、真实登录、模型回合或后台任务。

维护和模块边界：`test_app_maintenance.py` 验证只读／脱敏诊断、损坏和过期备份、恢复失败回退、活跃任务阻止、保留历史／认证／删除记录、安装失败后的登记撤销、保持集成及自定义数据位置，以及不同目录和无 Tk 后端导入。维护窗口验证真实表单、脱敏剪贴板、配置恢复及关闭时不覆盖已恢复偏好。

原生切出兼容：`python tests/verify_resume_switch.py` 使用真实 Codex app-server 验证写锁接管记录、使用中拒绝释放、空闲后同步、终端状态仍为 running、文件保留及迟到清理保护；零模型请求，不代表交互式 TUI 的 `/resume` 已验收。该脚本纳入双平台 Actions 原生检查。单元和界面验证取消未接管启动、迟到 runner 拒绝执行、未知接管记录保守保护及当前选择的任务范围。

UI 长对话测试主动触发分批渲染，等待渲染及窗口布局完成后核对正文和阅读位置。恢复操作允许云端磁盘耗时；测试退出时先等待后台线程释放临时目录中的锁。

beta 发行检查：`release_audit.py` 检查当前源码／完整可达 Git 历史／可选 ZIP，报告只含分类和位置；`test_release_audit.py` 用临时 Git 验证已删除秘密也能检出。`first_use_check.py 源码包.zip` 从解压源码用临时用户执行安装、首次配置、真实 Markdown 阅读、可选集成、源码移动升级、卸载和保留数据，不访问个人 home。先在独立 venv 安装 requirements-ui.txt；Linux 无桌面用 xvfb-run。`beta_linux_run.py` 是可选本地 Linux helper，需要已准备的验证 CLI／终端缓存，不是产品安装依赖。完整说明见 [beta 记录](../docs/beta-release.md)。

`test_*.py` 为单元测试，使用临时目录和合成数据；`verify_native.py`、`verify_ui_resume.py` 为调用已安装 Codex 的隔离兼容验证脚本，不会随单元测试自动运行。

`test_history_ui.py` 使用透明 Tk 窗口在 Windows／Linux 验证实际按钮、刷新、阅读位置、偏好保存、右栏滚动与窗口缩放、项目筛选及回收站详情／恢复，不启动终端或模型。没有可用 Tk 桌面时跳过这项检查；核心数据与设置测试仍可执行。测试的配置、数据库和历史均位于临时目录。`test_trash.py` 还覆盖精确项目匹配、超过列表上限的目录选项、逐项恢复结果及不覆盖已有分支；`test_ui_support.py` 覆盖旧回收站元数据与到期时间摘要。

退出回归场景覆盖 Tk 原生 Tcl 定时任务和 Python 定时回调同时存在时关闭窗口，以及偏好保存失败仍退出并记录日志。

缩放回归覆盖连续尺寸通知只应用最终尺寸、仅高度变化不重复设置右栏换行／滚动区域、缩放不重新编译 Markdown 或准备启动、大正文暂停重绘后保留全文／阅读位置，以及拖动回原尺寸和排版任务待处理时关闭。

新增对话回归覆盖空历史、内置／仅新会话配置、禁用入口、目录校验、命令复制、重复点击、准备中取消和终端失败重试。后端检查新启动不携带旧 ID／恢复参数、预览变化阻止启动，以及未初始化 home 的检查行为；退出采集使用合成记录。独立 `launch_job_runner.py` 子进程实际执行临时 Python 记录程序，并验证退出状态及历史入库；临时 home／来源配置隔离，不运行真实 Agent 或模型。

从仓库根目录运行全部单元测试：

```shell
python -m pip install -r requirements-ui.txt
python -m unittest discover -s tests -t . -v
```

`test_markdown_preview.py` 验证标题、嵌套列表、引用、表格对齐、行内格式、未闭合代码围栏、精确代码高亮及不加载资源。新增中文标点相邻的加粗／斜体、数学分隔符与围栏、金额／转义／代码保护、矩阵／aligned 离线 PNG、超限／不支持语法回退和外部 SVG 拒绝检查。隐形窗口场景验证 Markdown／原文切换、代码／公式及含公式的正文复制、公式源码搜索与突出显示、后台排版不调用 Tk、链接点击、中文搜索、长代码／宽表格横向滚动、分页阅读位置和源文件不变；缺少 Markdown 依赖时，相关检查跳过，原文回退检查继续执行。渲染检查使用合成会话，浏览器与网络请求均替换为测试对象，不实际打开外部页面。

本地链接检查覆盖 file://、Windows 正反斜杠、Codex 路径行号后缀、相对路径、空格编码及缺失文件；只在点击时定位文件，文件管理器被测试对象替代。实际 Tk 场景还核对左列纯标题、右栏模型／版本／分支信息、紧凑段落、工具行无背景条，以及切换回原文后内容不变。

`test_launch_backend.py` 覆盖配置校验／原子保存／坏条目隔离、CLI 管理与恢复准备、环境引用、目录策略、完整结果排序及原生目标 ID。Windows PowerShell 场景调用临时捕获程序／脚本，验证真实参数中的中文、空格、引号、特殊字符与 CodexArgs 数组，不启动 Agent。`test_claude_to_codex.py` 覆盖转换生命周期、完整文字、遗漏统计、重复 UUID、源类型辨别、工具不重放、写锁、来源变化、schema 失败和回收站；UI 验证 Claude → Codex 使用新目标 ID。

`test_framework_safety.py` 覆盖回收站文件损坏／移动期间变化、恢复与到期清理互斥、未完成恢复保留、Claude 活跃或无法检查时禁止删除、自定义 Claude home、POSIX 进程识别、新会话和旧版任务重放、并发任务认领、PID 实例变化及过期检查不覆盖新归属，以及 MCP 请求错误隔离和数据库连接关闭。进程查询使用模拟或临时 proc 目录；通用合成历史夹具默认模拟 Claude 空闲，不扫描本机真实 Agent。新报告记录 PID 与创建标识，旧报告缺少该标识时维持保守保护。

运行 `python tests/verify_claude_to_codex.py` 使用合成 Claude 会话与系统临时目录验证已安装 Codex 的原生列出、resume、读回、写锁、回收站与脱离来源后的重启恢复；不读取真实会话或认证，不发送模型请求。该原生检查不会由 unittest 自动启动。

默认的 `python -m unittest discover -v` 也可以发现这些测试。验证单个模块时使用模块名：

```shell
python -m unittest tests.test_trash -v
```

原生兼容验证需要本机已安装 Codex，并且统一索引中存在可用的来源会话：

```shell
python tests/verify_native.py --source-session UUID_OR_PREFIX
python tests/verify_native.py --source-session UUID_OR_PREFIX --trash
```

该脚本只读来源会话，在临时连接中检查原生列出、恢复、历史读取、写锁，以及可选的删除／恢复，不发送模型请求。可用 `--help` 查看参数。Windows 用户可将 `python` 替换为自己的 Python 可执行文件路径；源码运行使用 Python 3.11+。

平台兼容范围见 [Linux 说明](../docs/linux.md) 和 [P1 验收记录](../docs/p1-verification.md)。

`test_p1_features.py` 覆盖逻辑重命名／标题搜索、接续与删除恢复保留名称、并行工具 ID 和完整输出／变化检测、已有入口显示管理、过期后台搜索丢弃及较早消息自动定位、重复安装／可选集成／卸载归属、Windows 实际快捷方式和 Linux systemd 单位文件。

`test_conversation_titles.py` 覆盖账户前缀与普通标签区分、原生标题修改前记录、原生写锁、缺失文件和接口失败；`test_ui_support.py` 与 UI 回归覆盖每条对话独立记忆启动命令、关联版本继承及重启持久化。`verify_ui_resume.py` 另以真实 Codex 验证重命名后 list／read／resume／重启读回和恢复自动标题，零模型请求。

`test_lock_waiting.py` 使用真实 OS 锁核验有界等待、取消和互斥；Windows 保持占锁超过旧 CRT 的约 9 秒等待上限，再释放并验证后续操作。UI 场景覆盖等待期间响应、重复启动去重、取消／超时不创建任务、关闭只取消等待，以及源文件变化后拒绝启动。

右栏交互覆盖只读文字选择、Ctrl+A／Ctrl+C、右键复制、动态内容更新和自适应高度；合成 160 组工具验证收起时不创建详情文本框、短工具消息也分批让出事件循环、同轮启动预览计算合并、读取工具不重算启动计划，以及刷新未变正文不重建排版。性能回归验证行为和控件数量，不以机器相关的耗时阈值判定成功。

Linux 可运行 `python tests/linux_check.py`；它要求真实 Tk 桌面并运行完整回归，CI 使用 xvfb-run。`linux_terminal_check.py` 是显式运行的真实终端验证：传入 xterm 完整路径或设置 AGENT_HISTORY_TERMINAL，检查中文／特殊参数、环境、目录、退出报告、无桌面 CLI 和安装卸载。该脚本会打开测试终端，只执行临时记录程序，不启动 Agent。

`linux_prepare.py` 为本机验证使用的可选准备脚本，明确执行时才从官方 npm 包下载 Codex 0.159.2 及安装隔离 Markdown 依赖到用户缓存，不安装到系统位置。原生脚本需要 PATH 中实际可运行的 Codex；Linux 的 Claude 往返验证使用 Claude 可执行占位和合成 JSONL，未验证 Claude CLI 的实际交互。所有 verify 脚本都不发送模型请求。

便携包：先暂存／提交源码，运行 `python integrations/build_portable.py`；构建使用 Git 索引中的源码并规范脚本行尾。`python tests/portable_check.py dist/agent-history-portable.zip` 在临时目录检查解压、checksum、源码编译和安装 dry-run，不修改用户配置。

`test_review_fixes.py` 覆盖删除中途失败的文件／原生 SQL 恢复日志、恢复重试与缓存去重、等长／变长重写、停用来源、超过 100 条任务及并发报告读写、自定义 PS1 的原生命令退出码、未变化版本不重复解析、增量快照检查点与空间增长，以及旧快照维护中断后续做。针对删除速度还检查只更新相关副本、未索引的新分支仍保护父会话；Tk 测试检查排序不重新检索／读正文、长文件后台读取及过期结果丢弃。

`test_setup_settings.py` 使用临时来源和真实 Tk 表单验证首次配置、接入范围、仅单 Agent、配置管理、排序／收藏／默认，以及已有配置／认证文件保持不变。`test_launch_jobs.py` 验证独立报告进程、真实 PowerShell 退出码、异常中断、采集失败和重试；不启动模型。

`test_conversation_store.py` 覆盖 Codex ↔ Claude 往返、多 Codex 连接、重复问题和采集、并行分支、压缩／重写／回退、旧快照不可变、读取失败后恢复、发布意图恢复，以及删除／恢复不自动复活目标。UI 场景验证默认归组、显式分支选择及另一平台更新后保留逻辑选中项。

`test_conversation_merge.py` 验证公共前缀归并、独立分支／重复问题、工具全文及末尾换行、旧文件不变、来源变化阻止发布、验证失败后的重试，以及旧文件新增或合并文件删除时恢复旧分支。`python tests/verify_conversation_merge.py` 在临时目录用真实 Codex 检查合并后的列出／resume／重启读回；包含旧压缩记录和长工具输出，零模型回合。

跨平台返回与首次配置的原生验证：

```shell
python tests/verify_handoff.py
```

该脚本只用系统临时目录中的合成会话，通过首次配置 API 和启动检查后，执行 Codex → Claude → Codex 及已续聊副本再跨 Codex 连接，核对原生列出／resume／新增文字读回／重启、来源不变；不发送模型请求。Claude 新增回合为合成记录，未执行交互式 Claude CLI。P0 的逐项证据与实际验证边界见 [验收记录](../docs/p0-verification.md)。

单主会话接续验证：

```shell
python -m unittest tests.test_single_conversation -v
python tests/verify_single_conversation.py
```

验证同连接不复制、跨 Agent／连接正常结束后只保留一个可续聊原生文件、启动准备与独立终端进程的重复／过期任务拦截、合并签名回收、并行修改保留、原生锁、异常退出、验证失败、清理中断和重试。原生脚本使用隔离临时用户、合成 Claude JSONL、真实 Codex list／resume／重启读回和独立退出进程，零模型请求；不验证真实 Claude 模型执行。备份快照仍独立存在。

P1-14 升级前任务的兼容验证（显式 legacy 策略）：

```shell
python -m unittest tests.test_handoff_lifecycle -v
python tests/verify_temporary_handoff.py
```

单元检查覆盖双向返回、多连接、新 ID／旧删除记录、原文件保留、重复回调、原生写锁、并行分支、异常退出、损坏／半条 JSONL、验证失败、发布／清理崩溃重试及未登记文件不清理。原生脚本使用隔离合成历史，调用实际 Codex resume 和列表确认内容与临时记录回收，并在真实独立 Python／PowerShell 退出进程中完成返回 Claude 的收尾，不发送模型请求。Claude CLI 交互恢复未验证，Claude 侧验证原生 JSONL、ID 和新增文字。Windows Claude 进程检查使用 CIM；权限受限无法检查时保留临时文件并报告失败。

UI 恢复准备的原生验证可以直接使用完全合成的历史，无需本机私密会话：

```shell
python tests/verify_ui_resume.py
```

该脚本只在临时目录初始化两个 Codex 连接，创建空线程并填入合成 legacy 记录，检查 UI 的准备计划、快照与预览一致、列出、恢复、历史读取、重启恢复和来源不变；不执行任何模型回合。2026-10-07 已在 Windows / Codex 0.159.2 验证通过。

新增单元及隐形窗口场景还覆盖同 ID 的文件独立预览／全文匹配／Claude 转换、祖先截止位置、续聊目标保护、变化来源的启动阻止、禁用入口菜单、超过 500 条的列表加载、超过 400 条的消息加载和 Ctrl+F 搜索定位。

`test_read_performance.py` 验证读取分页只解析所需尾部、跨记录／跨分支分页重建全文、UTF-8 工具记录直接定位及旧序号回退、SQL 数据分页与选中项保留、精确全文搜索／旧数据库补齐、旧版替换写入的索引一致性，以及报告缓存不会把重新运行的任务标为已采集。隐形窗口还检查后台发现未完成时已有列表可用、过时读取取消、加载更早消息后的阅读位置和完整对话搜索。

性能复现：`python tests/benchmark_reading.py --files 1000 --messages 20000 --repeat 3`。仅生成隔离的合成文件，不调用模型；测量边界和本机参考值见[性能说明](../docs/performance.md)。

`test_optimization_followup.py` 覆盖追加片段、跨消息子串、重写／未完成记录、逐文件失败隔离与精确路径恢复、旧进程检索兼容、分组缓存失效、取消后重试、有界读取并发及性能统计脱敏。界面回归另验证小文件和启动选项后台计算、远距离匹配仅排版一页，以及加载更早消息保留已有工具控件和展开内容。Linux 搜索测试按实际任务／排版状态等待，不通过放弃内容和位置断言来绕过失败。

`test_schema_upgrade.py` 使用合成历史复现缺表的早期 v5 数据库，验证列表／采集前完成升级、完整 v5 不重建片段、当前版本缺对象时修复、并发打开只迁移一次，以及未知更新版本保持原状；核对逻辑记录、不可变修订和原生文件不变。
