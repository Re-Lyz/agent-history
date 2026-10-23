# 诊断、配置恢复与安装维护

主窗口点击“诊断与维护”。检查和维护在后台执行，执行完成前窗口保持打开。仅查看诊断和预览不会更新历史或安装登记。

## 环境诊断

“运行诊断”检查 Python、SQLite、Markdown 依赖、程序位置、配置有效性、数据库版本、启动任务数量及安装登记。“校验数据库”另外执行只读 SQLite quick_check，大数据库可能耗时较长。不创建、迁移或重建历史库，不运行 Agent。

诊断概要同时包含固定阶段的数值性能统计和待重试索引数量。历史窗口右栏可查看当前文件的索引失败原因；“重试失败索引”只重读失败路径，成功后清除对应提示。性能统计保存在私人运行目录的 `performance/`，只含次数、耗时和数量，不含对话、查询或路径；它们属于可丢弃的诊断缓存。完整机制及测量边界见 [性能说明](performance.md)。

窗口显示本机路径，供本地排错；“复制脱敏报告”和“导出脱敏报告”仅保留结构化概要，排除路径、命令参数、环境值、错误原文、认证和对话正文。PATH 缺失不代表自定义程序一定不可用；真实登录状态仍需在对应终端确认。数据库版本未知时提示复核，不自动降级。

## 配置备份与恢复

备份包含本工具的 connections.json、sources.json、launch-profiles.json、launch-entries.json 和 ui-preferences.json，保存在私人数据目录的 maintenance/config-backups。每次生成独立编号，不覆盖旧快照。备份可能含私人路径或自定义环境值，不得提交或上传。

选择备份，点击“预览配置恢复”，核对后点击“确认恢复配置”。恢复前校验全部文件的哈希、大小、格式和所属目录，再备份当前配置。预览后出现变化时停止；中途写入失败时尝试还原本次已写内容，恢复前快照保留。备份中原本不存在的配置会移出当前配置，其当前内容仍在恢复前快照中。界面偏好在重新打开后生效。

恢复不触碰 history.db、导入映射、删除记录、原生数据库、转录、不可变修订或 Agent 认证；不能用配置备份恢复已删除的对话。启动、采集或活跃终端任务未结束时，拒绝恢复。

## 更新与卸载

选择“原安装目录”，默认是 ~/.local/share/agent-history。源码直接运行且没有 installation.json 时，无须卸载登记。

更新时选择完整的新版源码目录，点击“预览更新”，核对后“确认更新入口”。复用原先启用的集成和任务，保持自定义配置与数据位置；不下载源码或安装 Agent。预览后源码、配置或安装登记变化时停止，用户修改过的入口或集成不被覆盖。

卸载先预览，再“确认卸载（保留历史）”。只撤销仍属于本工具且未被修改的登记。历史、备份、配置和用户修改过的项目保留。中途失败时保留配置快照和安装日志；可修复后重试，也可先预览卸载来撤销已登记的部分步骤。配置恢复不等于回滚系统任务或全部第三方设置。

## 无桌面入口

从任意目录调用源码中的 maintenance.py，或在源码目录运行 python -m history_app：

```text
python maintenance.py diagnose
python maintenance.py diagnose --deep
python maintenance.py backup
python maintenance.py backups
python maintenance.py restore-config --backup-id 备份编号
python maintenance.py restore-config --backup-id 备份编号 --apply
python maintenance.py upgrade --prefix 原安装目录 --source-root 新版源码目录
python maintenance.py upgrade --prefix 原安装目录 --source-root 新版源码目录 --apply
python maintenance.py uninstall --prefix 原安装目录
python maintenance.py uninstall --prefix 原安装目录 --apply
```

支持 --shared-root 和 --data-dir。更新、卸载和恢复配置默认只预览，显式 --apply 才执行。诊断标准输出始终为脱敏概要。原有 maintenance.py --apply 修订存储维护入口保持兼容。
