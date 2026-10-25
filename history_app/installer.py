"""Install desktop launchers and explicitly selected integrations; stdlib only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from setup_backend import atomic_json

ROOT = Path(__file__).resolve().parents[1]
CODEX_BLOCK = r'(?ms)^\[mcp_servers\.agent-history(?:\.[^\]]+)?\][^\n]*\n.*?(?=^\[(?!mcp_servers\.agent-history(?:\.|\]))|\Z)'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def json_field(path, keys):
    value = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def remember_json(prior, manifest, path, keys, action):
    registrations = prior.setdefault('registrations', {})
    key = str(path) + ':' + '.'.join(keys)
    if key not in registrations:
        registrations[key] = dict(path=str(path), keys=keys, before=json_field(path, keys))
        atomic_json(manifest, prior)
    action()
    registrations[key]['after'] = json_field(path, keys)
    atomic_json(manifest, prior)


def preflight(python):
    script = ('import sys,json,tkinter,sqlite3,markdown_it,pygments,mdit_py_cjk_friendly,mdit_py_plugins.dollarmath,ziamath,resvg_py; '
              'assert sys.version_info >= (3,11), "Python 3.11+ required"; '
              'db=sqlite3.connect(":memory:"); db.execute("CREATE VIRTUAL TABLE f USING fts5(text, tokenize=trigram)"); '
              'print(json.dumps({"python":sys.executable,"version":list(sys.version_info[:3])}))')
    result = subprocess.run([str(python), '-c', script], capture_output=True, text=True, encoding='utf-8', timeout=30)
    if result.returncode:
        raise ValueError('需要 Python 3.11+、Tk、SQLite FTS5 和 Markdown 依赖。使用所选 Python 运行 '
                         '-m pip install -r requirements-ui.txt\n' + result.stderr[-1600:])
    return json.loads(result.stdout)


def plan(prefix, user_home, python, *, shared_root=None, desktop=False, codex_mcp=False, claude_mcp=False,
         claude_hook=False, collect_task=False, sync_task=False, source_root=None, data_dir=None):
    prefix, user_home = Path(prefix).resolve(), Path(user_home).resolve()
    root = Path(source_root).resolve() if source_root else ROOT
    report = preflight(python)
    python = report['python']
    config = Path(shared_root).resolve() if shared_root else prefix / 'config'
    data = Path(data_dir).resolve() if data_dir else user_home / '.agent-history'
    files = {}
    env = dict(AGENT_HISTORY_SHARED_ROOT=str(config), AGENT_HISTORY_HOME=str(data))
    if os.name == 'nt':
        # VBS starts the UI invisibly and avoids cmd.exe interpolation of paths.
        pyw = Path(python).with_name('pythonw.exe')
        if not pyw.exists():
            pyw = Path(python)
        def vb(value):
            return '"' + value.replace('"', '""') + '"'
        files[str(prefix / 'codexui.vbs')] = '\n'.join(['Set shell = CreateObject("WScript.Shell")',
            *['shell.Environment("Process")(' + vb(key) + ') = ' + vb(value) for key, value in env.items()],
            'shell.Run ' + vb(subprocess.list2cmdline([str(pyw), str(root / 'history_ui.py')])) + ', 0, False'])
        shortcut = user_home / 'AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Agent History.lnk'
        shortcuts = [str(shortcut)] + ([str(user_home / 'Desktop/Agent History.lnk')] if desktop else [])
    else:
        files[str(prefix / 'codexui')] = '#!/bin/sh\n' + '\n'.join(
            'export ' + key + '=' + shlex.quote(value) for key, value in env.items()) + \
            '\nexec ' + shlex.join([python, str(root / 'history_ui.py')]) + '\n'
        files[str(user_home / '.local/share/applications/agent-history.desktop')] = (
            '[Desktop Entry]\nType=Application\nName=Agent History\nTerminal=false\n'
            'Exec="' + str(prefix / 'codexui').replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"\n')
        shortcuts = []
        if desktop:
            files[str(user_home / 'Desktop/agent-history.desktop')] = files[str(user_home / '.local/share/applications/agent-history.desktop')]
    if codex_mcp:
        files.setdefault(str(user_home / '.codex/config.toml'), None)
    if claude_mcp:
        files.setdefault(str(user_home / '.claude.json'), None)
    if claude_hook:
        files.setdefault(str(user_home / '.claude/settings.json'), None)
    tasks = []
    for enabled, name, cmd, minutes in [(collect_task, 'AgentHistoryCollect', 'collect', 5),
                                       (sync_task, 'AgentHistorySyncClaude', 'sync-claude', 60)]:
        if enabled:
            tasks.append(dict(name=name, command=cmd, minutes=minutes))
    return dict(prefix=str(prefix), user_home=str(user_home), python=python, config=str(config), data=str(data),
                source=str(root), desktop=desktop,
                files=files, shortcuts=shortcuts, tasks=tasks,
                features=dict(codex_mcp=codex_mcp, claude_mcp=claude_mcp, claude_hook=claude_hook))


def install(spec):
    root = Path(spec.get('source', ROOT))
    prefix = Path(spec['prefix'])
    prefix.mkdir(parents=True, exist_ok=True)
    manifest = prefix / 'installation.json'
    # Journal each successful step, so a partial install can be reviewed/retried.
    prior = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else dict(files={}, tasks=[])
    prior.update(root=str(root), python=spec['python'], config=spec['config'], data=spec['data'],
                 user_home=spec['user_home'], desktop=spec.get('desktop', False))
    for location, body in spec['files'].items():
        if body is None:
            continue
        path = Path(location)
        if path.exists() and (str(path) not in prior['files'] or digest(path) != prior['files'][str(path)]):
            raise ValueError('已有非本工具管理的文件，未覆盖：' + str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(path.name + '.tmp')
        staged.write_text(body, encoding='utf-16' if path.suffix.lower()=='.vbs' else 'utf-8', newline='\n')
        staged.replace(path)
        if os.name != 'nt':
            path.chmod(0o755 if path.name == 'codexui' else 0o644)
        prior['files'][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        atomic_json(manifest, prior)
    Path(spec['config']).mkdir(parents=True, exist_ok=True)
    for location in spec['shortcuts']:
        path = Path(location)
        if path.exists() and (location not in prior['files'] or digest(path) != prior['files'][location]):
            raise ValueError('已有快捷方式，未覆盖：' + location)
        path.parent.mkdir(parents=True, exist_ok=True)
        def ps(value):
            return "'" + str(value).replace("'", "''") + "'"
        script = ('$s=(New-Object -ComObject WScript.Shell).CreateShortcut(' + ps(path) + ');'
                  '$s.TargetPath=' + ps(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/wscript.exe') + ';'
                  '$s.Arguments=' + ps('"' + str(prefix / 'codexui.vbs') + '"') + ';$s.Save()')
        subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], check=True)
        prior['files'][location] = hashlib.sha256(path.read_bytes()).hexdigest()
        atomic_json(manifest, prior)
    features = spec['features']
    if features['claude_mcp'] or features['claude_hook']:
        from integrations.install_mcp import install as integrate
        for enabled, path, keys, flags in [
            (features['claude_mcp'], Path(spec['user_home']) / '.claude.json', ['mcpServers', 'agent-history'], dict(mcp=True, startup_hook=False)),
            (features['claude_hook'], Path(spec['user_home']) / '.claude/settings.json', ['hooks', 'SessionStart'], dict(mcp=False, startup_hook=True))]:
            if enabled:
                remember_json(prior, manifest, path, keys, lambda flags=flags: integrate(
                    root, spec['python'], Path(spec['user_home']), Path(spec['config']), data_home=spec['data'], **flags))
    if features['codex_mcp']:
        path = Path(spec['user_home']) / '.codex/config.toml'
        original = path.read_text(encoding='utf-8-sig') if path.exists() else ''
        import tomllib
        tomllib.loads(original)
        block = '[mcp_servers.agent-history]\ncommand = ' + json.dumps(spec['python']) + '\nargs = ' + \
                json.dumps([str(root / 'agent_history.py'), '--shared-root', spec['config'], '--data-dir', spec['data'], 'mcp']) + '\n'
        updated = re.sub(CODEX_BLOCK, '', original)
        registrations = prior.setdefault('registrations', {})
        key = str(path) + ':codex'
        if key not in registrations:
            registrations[key] = dict(path=str(path), before=''.join(re.findall(CODEX_BLOCK, original)), kind='toml')
            atomic_json(manifest, prior)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not (prefix / 'codex-config.before.toml').exists():
            (prefix / 'codex-config.before.toml').write_text(original, encoding='utf-8')
            (prefix / 'codex-config.before.toml').chmod(0o600)
        path.write_text(updated.rstrip() + '\n\n' + block, encoding='utf-8')
        registrations[key]['after'] = block
        atomic_json(manifest, prior)
    for task in spec['tasks']:
        args = [str(root / 'agent_history.py'), '--shared-root', spec['config'], '--data-dir', spec['data'], task['command']]
        if os.name == 'nt':
            if task['name'] not in prior['tasks']:
                found = subprocess.run(['schtasks.exe', '/Query', '/TN', task['name']], capture_output=True)
                if found.returncode == 0:
                    raise ValueError('已有同名计划任务，未覆盖：' + task['name'])
            subprocess.run(['schtasks.exe', '/Create', '/F', '/TN', task['name'], '/SC', 'MINUTE',
                '/MO', str(task['minutes']), '/TR', subprocess.list2cmdline([spec['python'], *args])], check=True)
            prior.setdefault('task_specs', {})[task['name']] = dict(command=spec['python'], arguments=subprocess.list2cmdline(args))
        else:
            folder = Path(spec['user_home']) / '.config/systemd/user'
            folder.mkdir(parents=True, exist_ok=True)
            unit = task['name'].lower()
            command = ' '.join('"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
                               for value in [spec['python'], *args])
            for suffix, body in [('.service', '[Unit]\nDescription=Agent History\n[Service]\nType=oneshot\nExecStart=' + command + '\n'),
                ('.timer', '[Unit]\nDescription=Agent History\n[Timer]\nOnBootSec=1min\nOnUnitActiveSec=' + str(task['minutes']) + 'min\n[Install]\nWantedBy=timers.target\n')]:
                location = folder / (unit + suffix)
                if location.exists() and (str(location) not in prior['files'] or digest(location) != prior['files'][str(location)]):
                    raise ValueError('已有其他用户任务，未覆盖：' + str(location))
                location.write_text(body, encoding='utf-8')
                prior['files'][str(location)] = digest(location)
                atomic_json(manifest, prior)
            if task['name'] not in prior['tasks']:
                prior['tasks'].append(task['name'])
                atomic_json(manifest, prior)
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
            subprocess.run(['systemctl', '--user', 'enable', '--now', unit + '.timer'], check=True)
        if task['name'] not in prior['tasks']:
            prior['tasks'].append(task['name'])
        atomic_json(manifest, prior)
    prior['features'] = {**prior.get('features', {}), **{k: True for k,v in features.items() if v}}
    atomic_json(manifest, prior)
    return prior


def uninstall(prefix, dry_run=False):
    prefix = Path(prefix).resolve()
    path = prefix / 'installation.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    removed, kept = [], []
    for task in value.get('tasks', []):
        if dry_run:
            continue
        if os.name == 'nt':
            # Compare the registered command before removing it.
            from xml.etree import ElementTree as ET
            report = subprocess.run(['schtasks.exe', '/Query', '/TN', task, '/XML'], capture_output=True, text=True)
            if report.returncode == 0:
                xml = ET.fromstring(report.stdout)
                ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
                arguments = xml.findtext('.//t:Exec/t:Arguments', namespaces=ns) or ''
                command = xml.findtext('.//t:Exec/t:Command', namespaces=ns) or ''
                expected = value.get('task_specs', {}).get(task, {})
                if command.strip('"') == expected.get('command') and arguments == expected.get('arguments'):
                    subprocess.run(['schtasks.exe', '/Delete', '/F', '/TN', task], check=True)
                else:
                    kept.append('task:' + task)
        else:
            owned = [location for location in value['files'] if Path(location).name == task.lower() + '.service']
            if owned and Path(owned[0]).exists() and digest(owned[0]) == value['files'][owned[0]]:
                subprocess.run(['systemctl', '--user', 'disable', '--now', task.lower() + '.timer'], check=True)
            elif owned:
                kept.append('task:' + task)
    for registration in value.get('registrations', {}).values():
        item = Path(registration['path'])
        if 'after' not in registration or not item.exists():
            continue
        if registration.get('kind') == 'toml':
            text = item.read_text(encoding='utf-8-sig')
            current = ''.join(re.findall(CODEX_BLOCK, text))
            if current.rstrip() != registration['after'].rstrip():
                kept.append(str(item)); continue
            if not dry_run:
                item.write_text(re.sub(CODEX_BLOCK, '', text).rstrip() + '\n\n' + registration['before'], encoding='utf-8')
        else:
            keys = registration['keys']
            if json_field(item, keys) != registration['after']:
                kept.append(str(item)); continue
            if not dry_run:
                data = json.loads(item.read_text(encoding='utf-8-sig'))
                node = data
                for key in keys[:-1]:
                    node = node[key]
                if registration['before'] is None:
                    node.pop(keys[-1], None)
                else:
                    node[keys[-1]] = registration['before']
                atomic_json(item, data)
        removed.append(str(item) + ':registration')
    for location, expected in value['files'].items():
        item = Path(location)
        if not item.exists():
            continue
        if hashlib.sha256(item.read_bytes()).hexdigest() == expected:
            if not dry_run:
                item.unlink()
            removed.append(location)
        else:
            kept.append(location)
    if not dry_run and os.name != 'nt' and value.get('tasks'):
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    return dict(removed=removed, kept=kept, integrations=value.get('features'), tasks=value.get('tasks'),
                message='仅撤销未被修改的安装登记；历史、来源配置、备份和修改过的设置保留。')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prefix', type=Path, default=Path.home() / '.local/share/agent-history')
    p.add_argument('--user-home', type=Path, default=Path.home())
    p.add_argument('--python', default=sys.executable)
    p.add_argument('--shared-root', type=Path)
    p.add_argument('--data-dir', type=Path)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--uninstall', action='store_true')
    for name in ('desktop', 'codex-mcp', 'claude-mcp', 'claude-hook', 'collect-task', 'sync-task'):
        p.add_argument('--' + name, action='store_true')
    args = vars(p.parse_args())
    dry, remove = args.pop('dry_run'), args.pop('uninstall')
    result = uninstall(args['prefix'], dry_run=dry) if remove else plan(**args)
    if not dry and not remove:
        print(json.dumps({key: result[key] for key in ('prefix','config','data','shortcuts','tasks','features')}, ensure_ascii=False))
        result = install(result)
    if not dry and not remove:
        # Previous registrations can contain private env values; keep them in
        # the private recovery journal, never echo them to the console.
        result = {key: result[key] for key in ('root','python','config','data','files','tasks','features')}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
