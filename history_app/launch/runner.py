"""Run a JSON argument array from an independent terminal without shell interpolation."""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def invocation(program, args):
    if os.name != 'nt':
        import terminal_platform
        return terminal_platform.invocation(program, args)
    def argument(value):
        return value if Path(program).suffix.lower() == '.ps1' and \
            re.fullmatch(r'-[A-Za-z_][A-Za-z0-9_-]*', value) else quote(value)
    return '& ' + quote(program) + ' ' + ' '.join(argument(value) for value in args)


def command(program, args):
    resolved = shutil.which(program) or str(Path(program).resolve())
    extension = Path(resolved).suffix.lower()
    if os.name != 'nt':
        if extension in ('.cmd', '.bat', '.ps1', '.exe'):
            raise ValueError('Linux 入口需要可执行程序或具有执行权限的脚本。')
        if not Path(resolved).is_file() or not os.access(resolved, os.X_OK):
            raise ValueError('程序不存在或无执行权限：' + str(program))
        return [resolved, *args]
    if extension in ('.cmd', '.bat', '.ps1'):
        # Batch argument parsing cannot preserve arbitrary quotes/metacharacters.
        # Refuse ambiguous arguments rather than execute or silently change them.
        if extension != '.ps1' and any(re.search(r'[&|<>^%!"\r\n]', arg) for arg in args):
            raise ValueError('Batch scripts with special characters in arguments require a .ps1 or executable wrapper.')
        script = invocation(resolved, args)
        script += '\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }'
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        return ['powershell.exe', '-NoLogo', '-NoProfile', '-EncodedCommand', encoded]
    return [resolved, *args]


def main():
    payload = json.loads(base64.b64decode(sys.argv[1]).decode('utf-8'))
    result = subprocess.run(command(payload['program'], payload['args']), shell=False)
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
