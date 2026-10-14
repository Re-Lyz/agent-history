# 自定义启动配置与后端接口

用户配置保存在 `~/.agent-history/launch-profiles.json`，设置 `AGENT_HISTORY_HOME` 时使用该目录。源码仓库只保留 [通用模板](../templates/launch-profiles.example.json)。文件格式为 `{"version": 1, "profiles": [...]}`，修改后在 UI 点“检查环境”或重开查看器加载；不需要修改源码。内置的连接入口和 `claude` 继续可用。

## 管理配置

UI 顶部“启动配置”打开管理表单，可新增、编辑、删除、上移／下移、收藏和设为默认；参数每行一个，环境变量使用 JSON。无效配置在表单中指出字段；“测试配置”仅检查环境，不启动 Agent。保存后立即重新加载入口。高级用户可点“打开配置文件”。

管理表单同时列出注册表中的 Codex 连接、Claude 和已有快捷入口，以连接身份去重。显示名称、启用、收藏、默认和顺序可以编辑；程序、关联 home 和参数属于原有连接配置，在表单中只读，不展示认证值。仅历史连接明确标注且不会参与终端启动。已有入口的“删除”恢复默认显示设置，保留连接和登录；自定义配置的“删除”才移除该配置。已有入口的显示设置位于仓库外 launch-entries.json，和右栏共用发现与显示逻辑。

Windows 的旧连接继续调用个人 codex-switch.ps1；直接接入的 home 和 Linux 使用原生程序及 CODEX_HOME。Linux 参数以数组执行，终端发现／自定义前缀和执行权限要求见 [Linux 说明](linux.md)。程序不在 PATH 中可在自定义 Codex／Claude 类型配置中指定完整路径；环境检查会检查所选程序。

```shell
python launch_profiles.py list
python launch_profiles.py put my-profile.json
python launch_profiles.py remove my-profile
python launch_profiles.py check my-profile
python launch_profiles.py plan my-profile --session SESSION_ID --source CONNECTION --cwd "C:/My Project"
python launch_profiles.py prepare my-profile --session SESSION_ID --source CONNECTION --cwd "C:/My Project"
python launch_profiles.py new-plan my-profile --cwd "C:/My Project"
python launch_profiles.py start-new my-profile --cwd "C:/My Project"
```

`put` 接收一个配置对象文件，按 ID 新增或替换，原子保存。`list` 列出有效配置及字段错误，不打印环境变量值；坏条目不影响其他有效条目或内置入口。存在坏条目时拒绝通过 CLI 覆盖配置，需先修正文件。`check` 仅检查程序、目录、关联连接和环境引用，不启动模型；`new-plan` 仅预览，`start-new` 才打开独立终端。

桌面 UI 的“新增对话”共用新会话适配器，可选择现有 Codex／Claude 入口及自定义配置，包括没有恢复模板的配置。只使用 `args`，不追加 `resume_args`；目录策略和环境引用沿用配置。准备时重新核对命令预览，并登记持久启动报告，终端退出后采集已接入的历史。新会话启动检查允许尚无原生恢复数据库的 Codex home，实际登录由终端完成。

`plan` 检查恢复方案，`prepare` 发布必要副本并返回实际目标 ID／终端参数，两者均不打开终端或发送模型请求。`--source claude` 明确选择 Claude 来源；环境值不写入输出。Windows 下程序间以 UTF-8 读取 JSON 输出时，可设置 `PYTHONIOENCODING=utf-8`。

## 字段

| 字段 | 含义 |
|---|---|
| `id` / `name` | 稳定 ID 与显示名称，ID 使用字母、数字、下划线、短横线；UI 内部标识为 `profile:ID` |
| `type` | `codex`、`claude` 或 `custom` |
| `connection` | Codex 配置关联的已注册连接，不能是只读历史连接 |
| `program` | 可执行程序或 `.ps1` 脚本；Codex／Claude 可省略并使用已有入口；自定义类型必填 |
| `args` | 每个参数单独一个字符串；内置 Codex 配置通过启动器的 `CodexArgs` 数组传给 Codex |
| `resume_args` | 程序覆盖／自定义恢复参数数组，必须含 `{session_id}`；内置 Codex 启动器自行构造，不能覆盖 |
| `resume_tools` | 自定义命令声明可恢复的来源：`["codex"]`、`["claude"]` 或两者；与恢复参数一起省略时只启动新会话 |
| `cwd_policy` | `chosen` 使用 UI／CLI 选择的目录，`session` 使用原会话目录，`fixed` 使用配置的 `cwd` |
| `cwd` | `fixed` 策略的绝对目录 |
| `env` | 环境变量名到字面值或 `{"from_env": "EXISTING_VARIABLE"}` 的映射，凭据优先引用既有环境变量 |
| `enabled` | 是否在启动入口中启用，默认 `true` |
| `favorite` / `order` / `default` | 收藏、整数排序位置、默认入口；最多一个默认配置。入口优先显示默认与收藏，然后按排序位置排列 |

参数支持 `{session_id}`、`{source_session_id}`、`{source}`、`{connection}`、`{cwd}`。字面大括号写成 `{{`／`}}`；不支持表达式、属性访问和格式转换。参数是数组，不能填写一整段任意 shell 文本。Shell 函数／别名应改为可执行程序或明确调用的 `.ps1` 包装脚本。

## 恢复适配

`launch_backend.py` 不依赖 Tk：`launch_plan` 只检查和预览；`prepare_launch` 在调用方持有收集锁时准备实际目标会话，返回工作目录、终端参数和环境变量；`prepare_new` 构造新会话启动。UI 复用这些函数，准备成功后才调用 `start_terminal`。

Codex 类型使用已有原连接核对／跨连接快照／原生 resume 适配；Claude 类型复用现有文字摘要转换和项目目录恢复。Claude → Codex 生成独立目标 ID，见 [转换说明](claude-to-codex.md)。导入完成后使用目标 ID，绝不把来源 ID 当作目标副本 ID。自定义类型按声明的参数恢复选中会话的原 ID，自行负责其程序的恢复协议，不推测跨 Agent 转换。仅新会话的配置在历史恢复菜单中说明不可恢复，可使用 `start-new` 启动。

覆盖 Codex 程序时传入目标 `CODEX_HOME`；配置不能用环境变量覆盖 Codex 存储位置。推荐省略 `program`，继续使用现有连接启动器生成配置；直接调用自定义 Codex 程序时须保证其使用该连接已有配置。

Windows 中 `.exe` 参数通过 Python 的参数数组传递，`.ps1` 参数通过逐项引用的 PowerShell 字面量传递，中文、空格、引号和特殊字符均有实际进程验证。`.cmd`／`.bat` 参数含引号或 shell 特殊字符时明确拒绝，需改用 `.ps1` 或可执行包装程序，避免误解析。配置了环境变量时使用独立 PowerShell 控制台，子进程继承变量；不依赖已运行 Windows Terminal 的旧环境，也不把变量值写入命令预览。终端打开仅表示进程已启动，恢复成功需在终端确认；退出报告和采集方式见 [跨平台接续](conversation-handoff.md)。

首次打开缺少配置或历史为空时显示来源向导；也可随时点“配置历史来源”。发现已注册连接、常见本机 Codex／Claude 目录与程序，可选择接入范围、修改显示名称或手动添加已有目录。已有 Codex `config.toml` 使用直接入口与对应 `CODEX_HOME`，不要求补建 profile／common 文件；仅 Claude 环境也可使用。选择存于仓库外 `sources.json`，现有登录与配置文件保留。需先在 Agent 完成原生初始化才能恢复历史；程序缺失或尚未初始化会显示具体原因。
