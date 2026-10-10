# Linux 使用与验证范围

历史采集、检索、来源配置、版本关系、导入、回收站和接续状态使用共同的 Python 实现。Linux 启动传递参数数组，不调用 PowerShell／Windows Terminal；CODEX_HOME 指向所选连接。原生 writer lock 和缓存互斥使用 flock，活跃文件不能删除或覆盖。

## 安装

按 [首次使用](first-use.md) 配置 Python 3.11+、Tk 和 requirements-ui.txt；`sh integrations/install.sh --dry-run` 预览，去掉 --dry-run 安装。可通过 AGENT_HISTORY_PYTHON 指定 Python，或直接运行 `python install.py`。默认不安装 Agent 或用户级后台任务。

Linux 来源直接使用该 Agent home 的 config.toml。旧 Windows 的 profile.toml／common.toml 配置模式需要迁移为 Linux CLI 可读取的 config.toml；不能把 Windows 包装脚本直接拿来运行。界面支持自定义程序完整路径、参数和环境变量引用。

终端自动发现顺序为 gnome-terminal、konsole、xfce4-terminal、x-terminal-emulator、xterm。自定义终端可设置 JSON 参数前缀，例如：

```sh
export AGENT_HISTORY_TERMINAL='["xterm", "-e"]'
```

前缀后的内容作为独立参数执行，不会 eval。选择的终端需要把后续参数当作要运行的程序；它必须保留调用环境和工作目录。权限、桌面环境和终端缺失会显示在启动检查中。Windows 的 .cmd／.bat／.ps1／.exe 入口不作为 Linux 原生程序使用。

Linux 的 MouseWheel、Button-4／Button-5 在阅读区、右栏和工具详情中各自滚动。没有 DISPLAY／WAYLAND_DISPLAY 的环境可运行 agent_history.py collect／sessions／search／show、导入、来源配置和回收站 CLI；不能启动 Tk 查看器或桌面终端。

## 后台任务与卸载

--collect-task／--sync-task 安装用户级 systemd service 和 timer，支持 systemctl --user 的环境可用。没有用户级 systemd 会明确返回安装错误，已经完成的步骤保存在 installation.json，可修复后重试。

--uninstall 关闭本工具登记且未被修改的 timer，移除对应单位文件和桌面入口，恢复本工具负责的 Agent 配置项。历史、认证、来源配置和备份保留；修改过的配置项跳过并报告。桌面入口不会写入 shell 的 rc 文件。

## 已实测与边界

2026-10-10 在 Ubuntu 20.04／WSL2／WSLg，Python 3.12.2、Tk 8.6.14、SQLite 3.46、markdown-it-py 4.2.0、Pygments 2.21.0、xterm 353、Linux Codex CLI 0.159.2 实测。验证依赖、xterm 和 Codex 二进制放入专用缓存，测试中的 home、项目目录、安装位置和历史全部独立临时创建。

通过完整合成回归、真实 Tk Markdown 阅读和控件交互、真实 xterm 特殊参数／中文目录／环境／退出码、重复安装／卸载保留数据，以及原生 Codex list／resume／读回／重启、写锁、导入和删除恢复。临时接续在独立进程退出后持久保存并回收验证通过，不发送模型请求。

Claude JSONL 与接续关系已验证，Claude CLI 实际登录／交互续聊尚未验证。其他发行版、Wayland 原生桌面和自动发现列表中除 xterm 外的终端尚未实测；WSL 环境未做真实 systemd timer 调度，单位文件登记和卸载由隔离测试验证。GitHub Actions 的 Ubuntu 22.04／xvfb 与 Windows 回归、源码包、临时用户首次使用及零请求原生检查已经全部通过，见 [实际 CI](https://github.com/Re-Lyz/agent-history/actions/runs/38031469719)。完整干净系统装机演练仍归 OSS-08。
