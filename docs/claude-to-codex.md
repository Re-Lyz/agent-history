# Claude 对话导入 Codex

显式选择 Claude 会话，转换为目标连接中独立的 Codex 原生会话；原始文件保留。使用目标连接已有的模型与 provider 配置，生成新的目标会话 ID，接入内置 resume 列表。后台自动同步仍只处理原始 Codex 会话，不自动循环转换。

```shell
python agent_history.py import-codex claude:SESSION_ID --to CONNECTION --dry-run
python agent_history.py import-codex claude:SESSION_ID --to CONNECTION --cwd "C:/My Project"
```

目标连接需先启动初始化；`--cwd` 可更改转换后的工作目录。不填写时沿用原目录。UI 选中 Claude 会话后，可选择 Codex 连接或关联该连接的自定义 Codex 配置，检查转换提示后点击“打开终端并继续”。原生导入完成后执行目标连接的 resume，使用实际生成的 ID。

## 保留内容与限制

- 保留选中文件中的主对话文字，不沿用全文检索库的截断文本。每个文本块作为历史消息保存。
- 工具调用和输出转为已完成的助手文字摘要，单个摘要最多保留 4000 字符；不会生成待执行调用，不重放命令。
- 不转换图片、文档附件、隐藏推理和工具执行状态；路径引用仍是文字，对应文件需要另行提供。
- 跳过侧链、元消息；重复 UUID 保留最后版本。只转换文件中实际保留的主记录，不重建压缩边界或已丢失的上下文。
- `--dry-run` 返回转换统计和限制，包括文字／工具摘要数量、遗漏附件与推理数量；不写入目标会话或映射。UI 的预览说明主要转换边界。

## 生命周期

复用 `codex_imports` 映射：未变化副本复用；来源更新且目标未续聊时原地刷新；目标内容变化则创建新副本，保留已续聊分支。工作目录计入转换指纹。目标副本已删除时返回 `deleted`，不自动重建；回收站恢复后可继续使用。

准备前后比较源文件的大小与修改时间，读取期间变化则返回 `busy`，不开终端。来源 Claude 文件只读；不宣称持有 Claude 的活跃会话写锁。目标写入和复用检查 Codex 每线程 writer lock，活跃目标不会覆盖。JSONL 中间损坏或会话 ID 不匹配会拒绝转换。

生成 standalone legacy JSONL，同时写入对话消息与展示事件，注册目标原生 `threads` 元数据。转换后的文字不依赖原 Claude 文件。未知原生必填字段会拒绝导入；版本升级仍需隔离验证。

## 验证

```shell
python -m unittest tests.test_claude_to_codex -v
python tests/verify_claude_to_codex.py
```

单元测试覆盖完整文字、转换统计、工具不重放、重复导入、刷新、已续聊分支、目标写锁、来源变化／缺失／错 ID、schema 失败，以及删除／恢复。原生检查使用合成 Claude 会话和临时 Codex home，不读取真实历史或认证，不发起模型请求；核对列出、恢复、目标模型、历史内容、写锁、回收站和删除来源后的重启恢复。实际发送下一条模型消息的接续行为需另行验证。

2026-10-07：Windows／Codex CLI 0.159.2，原生检查通过上述项目，恢复得到 2 轮合成对话。
