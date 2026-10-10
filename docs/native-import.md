# Codex 原生 /resume 导入

`agent_history.py` 负责统一索引、导入映射、CLI 和 MCP；`codex_import.py` 负责原生快照和同步；`native_codex.py` 提供隔离验证用的本地 app-server JSON-RPC 客户端。

Codex 原始会话可跨连接导入完整快照；显式 Claude 来源使用独立的文字与工具摘要转换，见 [Claude 转 Codex](claude-to-codex.md)。自动同步仍仅处理原始 Codex 主会话。

## 存储

统一搜索库不是 Codex 原生库。导入器发布独立 JSONL 快照，再将会话元数据注册到目标 `state_*.sqlite` 的 `threads` 表。字段按目标版本实际 schema 取交集；未知必填字段会拒绝导入。
新版分页历史的 `thread_history_1.sqlite` 是本地投影缓存。刷新副本时按该会话 ID 清除缓存；Codex 恢复时从完整 JSONL 重建。不会复制其他线程的缓存或整个运行库。

原生分页 fork 使用 `history_base` 引用祖先，直接搬运 fork 文件会丢失上下文。本实现按 fork 的 ordinal/byte-offset 截止位置递归展平祖先，并去掉历史基引用，得到独立根会话。缺失祖先、循环引用、损坏 JSONL 均报告失败。

目标模型/provider 从该连接的 config/profile 读取；不读取或复制 auth.json。当前不支持将原生 SQLite 存储放到另一个 `sqlite_home` 的连接。

## 并发与更新

统一索引操作持有 collect.lock。目标副本写入期间持有 Codex 的每线程 writer lock；正在恢复或运行的线程会跳过刷新。
快照前后比较源文件 size/mtime，源文件变化则暂缓。目标副本 hash/size 与导入时一致才能原地更新；否则另建分支。
来源未更新但目标副本已续聊时，也另建副本；不会将该续聊版本作为来源会话的相同快照复用。未变化副本复用时也检查 writer lock。
写入使用临时文件和原子替换；原生索引注册失败会恢复旧会话文件。各数据库并非一个跨库的崩溃原子事务，异常断电后应检查导入映射与原生索引。

## 回退

1. 在 connections.json 中将 sync_history 设为 false；启动使用 -NoSync，可停用自动同步。
2. 停用同步不会删除已导入的副本；仍可使用原生 resume。旧实现源码保存在 Git 历史中。
3. 已导入副本可以在 Codex 内删除/归档。原始会话仍留在原连接中。
4. 首次导入前的 native-backups 保存原生数据库备份。只有在关闭对应 Codex/daemon、确认不会丢掉备份之后的新会话时，才可由人工恢复整库。

## 验证记录

2026-10-04：Windows / Codex 0.159.2。
自动测试覆盖重复导入、源文件不变、完整工具结果、分页副本标记、增量采集、防循环、刷新缓存、续聊保留、删除保留、预览、祖先展平、writer lock、未知 schema 失败回滚。
本机 app-server 隔离验证对 legacy 和 paginated 两种格式通过列出、恢复、读回历史以及脱离来源后的再次恢复。未发起付费模型请求。

2026-10-07：Windows / Codex 0.159.2。`python tests/verify_ui_resume.py` 使用完全合成的 legacy 会话及临时连接，验证 UI 的准备计划、选中文件与目标快照一致、原生列表、恢复、历史读取、重启后恢复和来源字节不变；未发送模型请求。新版 UI 对同 ID 不同文件、祖先边界、变动来源和禁用恢复组合另有单元及隐形窗口验证。本次新增原生脚本只覆盖 legacy；paginated 原生验证沿用此前脚本，不将合成祖先测试等同于新增 paginated 原生验收。
