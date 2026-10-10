"""Register this checkout's stdio MCP and Claude startup hook; preserve other settings."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


def update(path, change):
    data = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    change(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        staged = Path(stream.name)
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def install(root, python, user_home, shared_root=None, *, mcp=True, startup_hook=True, data_home=None):
    script = str(root / 'agent_history.py')
    common_args = [script, '--shared-root', str(shared_root or root.parent)]
    if data_home:
        common_args += ['--data-dir', str(data_home)]
    if mcp:
        update(user_home / '.claude.json', lambda d: d.setdefault('mcpServers', {}).update({
            'agent-history': {'type': 'stdio', 'command': python, 'args': [*common_args, 'mcp'], 'env': {}}}))

    def hook(data):
        groups = data.setdefault('hooks', {}).setdefault('SessionStart', [])
        for group in groups:
            group['hooks'] = [h for h in group.get('hooks', [])
                              if not ('agent_history.py' in json.dumps(h) and 'sync-claude' in json.dumps(h))]
        groups[:] = [group for group in groups if group.get('hooks')]
        command_args = [python, *common_args, 'sync-claude', '--quiet']
        command = subprocess.list2cmdline(command_args) if os.name == 'nt' else shlex.join(command_args)
        groups.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 60, 'async': True}]})
    if startup_hook:
        update(user_home / '.claude' / 'settings.json', hook)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--shared-root', type=Path)
    parser.add_argument('--user-home', type=Path, default=Path.home())
    args = parser.parse_args()
    install(args.root.resolve(), args.python, args.user_home.resolve(),
            args.shared_root.resolve() if args.shared_root else None)
