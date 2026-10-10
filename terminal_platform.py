"""Platform-specific terminal discovery and safe argument-array launch."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sys


def linux_terminal():
    configured = os.environ.get('AGENT_HISTORY_TERMINAL')
    if configured:
        # JSON prefix supports user wrappers without evaluating shell text.
        prefix = json.loads(configured)
        if not isinstance(prefix, list) or not prefix or any(not isinstance(v, str) for v in prefix):
            raise ValueError('AGENT_HISTORY_TERMINAL 必须是终端参数数组 JSON。')
        if not shutil.which(prefix[0]) and not Path(prefix[0]).is_file():
            raise ValueError('指定终端程序不存在。')
        return prefix
    for program, switch in [('gnome-terminal', '--'), ('konsole', '-e'),
                             ('xfce4-terminal', '-x'), ('x-terminal-emulator', '-e'), ('xterm', '-e')]:
        if shutil.which(program):
            return [program, switch]
    raise ValueError('未找到桌面终端；安装 xterm 或通过 AGENT_HISTORY_TERMINAL 配置终端参数数组。')


def issues():
    if os.name == 'nt':
        return [] if shutil.which('powershell.exe') else ['未找到 Windows PowerShell。']
    result = []
    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        result.append('没有图形会话；可使用历史 CLI，桌面启动需要 DISPLAY／WAYLAND_DISPLAY。')
    try:
        linux_terminal()
    except (ValueError, OSError) as error:
        result.append(str(error))
    return result


def invocation(program, args):
    return shlex.join([program, *args])
