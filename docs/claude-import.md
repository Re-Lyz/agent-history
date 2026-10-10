# Codex 历史导入 Claude Code

统一入口为 `agent_history.py`；使用方法和仓库布局见 [README](../README.md)。
`import-claude ID [--cwd 目录] [--dry-run]` 将 Codex 会话转换为 Claude Code 可恢复的独立会话。

## 内容与位置

生成的 JSONL 位于 `~/.claude/projects/<cwd 对应目录>/`，标题为 `[Codex] 原标题`。在原工作目录启动 Claude，再用 `/resume` 选择。
首条消息注明来源并提醒核实当前状态。保留用户/助手文字；最近 5 轮的工具调用为截断摘要，更早轮次只记录次数；不包含隐藏推理和图片。
压缩过的会话从最后一次压缩后开始导入。默认约 20 万字符上限，超过时省略最早的轮次。
导入映射在统一库的 `imports` 表中，副本标为 `import`，默认不作为检索列表或再次导入的来源。
两边续聊各自发展。格式在 Claude Code 2.1.284 验证，升级后先导入短会话检查。

Claude 历史现在也可显式导入 Codex 原生 `/resume`，详见 [Claude 转 Codex](claude-to-codex.md)；两种工具仍可通过 MCP 阅读历史。

## 自动更新

`sync-claude [--days 30] [--min-turns 2] [--dry-run]` 收集近期 Codex 主会话：

- 新会话新建副本；未续聊的副本随来源刷新，ID 不变。
- 已续聊的副本保留，另外生成来源最新内容的副本。
- 最近 30 分钟在 Claude 打开过的副本暂缓；手动删除的副本不重新生成。

`AgentHistorySyncClaude` 每小时执行；Claude 的 SessionStart hook 启动时异步执行一次。
再次打开 `/resume` 会读取新列表。停用时移除这个计划任务和 settings.json 中对应的 SessionStart hook。

## 数据维护

统一库 `~/.agent-history/history.db` 同时保存检索索引、Claude 导入映射和 Codex 原生导入映射。
删除这个库会丢失导入映射以及用户删除副本的记录。仅需重建索引时应保留导入表；维护前先备份。
MCP 的工具输出索引默认截取 4000 字符；Codex 到 Codex 的原生快照仍保留完整原记录。
