# 程序、账户与项目工作台

启动、原生 RPC、诊断和维护共用 history_app.programs。默认 Codex 每次重新解析稳定 current 入口，显式版本路径继续使用指定版本；自定义启动配置在目标原生导入期间使用同一程序。Python 实际运行检查要求 3.11+，识别 WindowsApps 占位入口，后台检查使用 python.exe。

## 程序与账户

打开“诊断与维护 → 程序与账户”，点击“扫描程序与账户”。按实际安装归组，分别显示 CLI、账户 home、后台包安装版本、运行版本、控制状态和兼容记录。控制服务不可达时仍读取安装包版本；未安装、未检查、不一致和权限不足分别表示。

扫描不会启动服务、登录或发送模型请求。自定义脚本不自动执行；可识别的 npm 包装入口从包元数据读取安装版本，不将其视作运行版本。脱敏复制排除账户名称、home、路径、参数、环境值和原始错误。

## 更新与重试

1. 选择程序并预览更新，核对实际命令、参数、安装归属、关联账户和阶段。
2. 正常结束相关任务。采集／启动互斥、持久任务、原生写锁或运行中的后台服务会阻止执行，不强行结束进程。
3. 执行前保存配置快照，再更新 CLI，重新解析程序与版本，逐账户同步已有后台包。
4. 已对齐版本不重复复制，固定版本账户保留，缺少后台包的账户不会自动安装。单账户失败不阻断其他账户，按阶段保存结果，可重新预览重试。
5. Codex 更新后自动运行隔离原生检查。失败或未验证部分保留诊断；完成后重新扫描查看当前版本。

独立安装优先调用 Codex 自身 update；失败时只接受官方 GitHub release 的对应平台资源。核对 API 提供的 SHA-256、解包文件和实际版本后才切换入口。TLS／下载失败、缺少摘要、摘要或版本不符、安装锁占用及下载期间入口变化均停止。旧 release 和 previous-history 指针保留，不降低 TLS 校验。

npm 更新使用该安装所属 prefix，项目本地依赖需显式适配器；Homebrew Codex 使用 cask。未知归属和自定义入口不猜测替换命令。包切换、配置恢复和历史数据库恢复分别处理，不以旧数据库覆盖新增历史。

本机策略在私人数据目录 program-policy.json，默认不需要创建：

```json
{
  "version": 1,
  "pinned_accounts": ["connection-name"],
  "adapters": {},
  "python": "/path/to/python3"
}
```

界面可固定／取消固定账户。adapters 使用扫描结果的程序 ID，配置 program 和逐项 args；参数支持 `{program}`。内部适配器仅负责 CLI，账户同步由框架处理；全流程脚本可作为外层入口调用维护 CLI，或提供只更新 CLI 的模式。适配脚本在预览后变化需重新预览。私人路径和参数不提交仓库。

旧版配置备份仍可恢复：缺少 program-policy 的旧快照保留当前维护偏好，不改写旧备份。

```shell
python maintenance.py programs
python maintenance.py programs --redact
python maintenance.py program-plan --program-id PROGRAM_ID
python maintenance.py update-programs --program-id PROGRAM_ID --apply
python maintenance.py check-native --program-id PROGRAM_ID
```

更新记录在私人 maintenance/program-updates/，配置快照在 maintenance/config-backups/。命令退出码、账户同步和原生兼容分别记录，不把退出码 0 等同于恢复或登录成功。

## 原生兼容

复用 verify_ui_resume、verify_claude_to_codex、verify_handoff、verify_temporary_handoff、verify_single_conversation 和 verify_resume_switch。临时用户、合成历史、明确选择的 CLI 验证原生列出、导入、恢复、重启读回、单主会话和独立退出收尾，不调用模型。分项结果和日志在私人 maintenance/compatibility/；结果包含版本、平台、时间和边界，只创建一次。

2026-10-26：Windows／Codex 0.164.0 六项通过；另通过真实伪终端空闲按 ID resume、终端继续运行、正常退出释放锁和消息保留，零模型请求。真实登录、模型回合、选择器、活跃回合／后台任务和远程控制仍需独立验收。实验服务命令依据本机帮助核对，变化时保留未检查状态。官方参考：[Codex CLI](https://learn.chatgpt.com/docs/codex/cli)、[app-server](https://learn.chatgpt.com/docs/app-server)。

## 项目工作台与上下文

主窗口“项目工作台”关联已有账户、项目、历史来源、任务和 Git worktree；分页读取，选中后只读检查 worktree，可打开原项目历史并沿用启动选择。保留任务报告原始含义，旧任务缺少退出码不推断完成。

额度是可选 provider 接口：accounts.cache_quota 接收 source、limits 中的 used_percent／resets_at／window，自动记录缓存时间，忽略令牌等额外字段。缓存在私人 maintenance/quotas/；不可用不影响手动启动。本轮未接入真实额度服务，不自动换账户。

上下文按精确项目和字面关键词复用现有 FTS／原生消息分页，提供历史摘录、来源、时间、会话 ID、文件字节位置和事件号。支持更早材料游标、取消、文件变化和来源缺失；不建立第二套历史库，不恢复已删除文件。UI 显示可读材料，CLI／MCP 返回结构化数据。历史内容可能过时，不自动作为当前决定或执行任务；模型生成摘要未启用。

```shell
python maintenance.py workspace --limit 50 --offset 0
python maintenance.py project-context --project /path/to/project --query keyword
```

MCP 新增 list_projects 和 project_context，使用现有索引只读连接，分页游标来自 next_cursor。完整 OS 与真实账号边界见 [验收清单](acceptance.md)。
