"""Shared executable discovery. No shell, authentication, or long-lived path cache."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

_overrides = ContextVar('native_program_overrides', default={})
VERSION = re.compile(r'\b\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\b')


@contextmanager
def using(home, program):
    token = _overrides.set({**_overrides.get(), str(Path(home).resolve()): program})
    try:
        yield
    finally:
        _overrides.reset(token)


def for_home(home, requested=None):
    if requested:
        return requested
    identity = str(Path(home).resolve())
    if _overrides.get().get(identity):
        return _overrides.get()[identity]
    if os.environ.get('AGENT_HISTORY_CODEX_PROGRAM'):
        return os.environ['AGENT_HISTORY_CODEX_PROGRAM']
    import agent_history as ah
    import connections
    for entry in connections.load(ah.ROOT).get('connections', {}).values():
        if str((ah.ROOT / entry['home']).resolve()) == identity:
            return entry.get('program') or 'codex'
    return 'codex'


def resolve(requested='codex', *, user=None):
    """An explicit path is authoritative; default discovery follows stable installs."""
    requested = str(requested)
    path = shutil.which(requested)
    if not path and Path(requested).expanduser().is_file():
        path = str(Path(requested).expanduser().absolute())
    if not path:
        raise ValueError('未找到程序：' + requested)
    path = Path(path).absolute()
    if path.name.lower() in ('codex.cmd', 'codex.ps1', 'codex'):
        # npm shims select a native vendor binary; use that same binary for RPC.
        vendor = path.parent / 'node_modules/@openai'
        candidates = list(vendor.glob('codex*/vendor/*/codex/' + ('codex.exe' if os.name == 'nt' else 'codex')))
        if len(candidates) == 1 and candidates[0].is_file():
            path = candidates[0]
    if 'windowsapps' in {part.lower() for part in path.parts} and path.name.lower().startswith('python'):
        raise ValueError('Python 指向 WindowsApps 占位入口，请配置可运行的解释器。')
    if requested.lower() in ('codex', 'codex.exe'):
        # PATH can point into yesterday's immutable release directory.
        for parent in path.parents:
            if parent.name == 'standalone' and parent.parent.name == 'packages':
                stable = parent / 'current' / 'bin' / path.name
                if stable.is_file():
                    path = stable
                break
    if os.name != 'nt' and not os.access(path, os.X_OK):
        raise ValueError('程序没有执行权限：' + str(path))
    return str(path)


def command(program, args):
    path = resolve(program)
    import launch_runner
    return launch_runner.command(path, args)


def run(program, args, *, home=None, timeout=12):
    env = os.environ.copy()
    if home is not None:
        env['CODEX_HOME'] = str(home)
        env.pop('CODEX_SQLITE_HOME', None)
    return subprocess.run(command(program, args), env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def version(program, *, home=None):
    try:
        with tempfile.TemporaryDirectory(prefix='agent-history-version-') as scratch:
            result = run(program, ['--version'], home=scratch)
        match = VERSION.search(result.stdout) if result.returncode == 0 else None
        return dict(state='ok' if match else 'failed', version=match.group() if match else None)
    except PermissionError:
        return dict(state='permission-denied', version=None)
    except subprocess.TimeoutExpired:
        return dict(state='timeout', version=None)
    except (OSError, ValueError):
        return dict(state='unavailable', version=None)


def describe(requested, tool='codex', *, home=None, probe=True):
    result = dict(requested=str(requested), tool=tool, state='missing', path=None,
                  version=None, channel='custom', installation=None, owner_home=None)
    try:
        path = Path(resolve(requested))
        real = path.resolve()
        metadata_version = None
        result.update(path=str(path), state='found')
        for parent in path.parents:
            if parent.name == 'standalone' and parent.parent.name == 'packages':
                result.update(channel='standalone', installation=str(parent), owner_home=str(parent.parent.parent))
                break
        explicit_release = 'releases' in Path(str(requested)).parts and Path(str(requested)).is_absolute()
        if explicit_release:
            result.update(channel='custom', version_policy='explicit-release')
        if 'node_modules' in real.parts:
            index = real.parts.index('node_modules')
            package_parent = Path(*real.parts[:index])
            if (package_parent / 'package.json').is_file():
                result.update(channel='custom', reason='项目本地依赖需要显式更新适配器。')
            else:
                prefix = package_parent.parent if os.name != 'nt' and package_parent.name == 'lib' else package_parent
                result.update(channel='npm', npm_prefix=str(prefix))
        elif 'Cellar' in real.parts or 'Caskroom' in real.parts:
            result['channel'] = 'brew'
        if path.suffix.lower() in ('.cmd', '.ps1') and tool in ('codex', 'claude'):
            package = '@openai/codex' if tool == 'codex' else '@anthropic-ai/claude-code'
            try:
                value = json.loads((path.parent / 'node_modules' / package / 'package.json').read_text(encoding='utf-8'))
                if value.get('name') == package and VERSION.fullmatch(value.get('version', '')) and not (path.parent / 'package.json').is_file():
                    metadata_version = value['version']
                    result.update(channel='npm', npm_prefix=str(path.parent))
            except (OSError, ValueError, TypeError):
                pass
        result['id'] = hashlib.sha256((tool + ':' + (str(real) if explicit_release else result['installation'] or str(real))).encode()).hexdigest()[:16]
        if probe and tool != 'custom' and path.suffix.lower() not in ('.ps1', '.py', '.sh', '.bat', '.cmd'):
            result.update(version(path, home=home))
        elif probe and metadata_version:
            result.update(state='installed', version=metadata_version, version_source='package-metadata')
        elif probe:
            result.update(state='not-probed', reason='自定义入口不自动执行版本命令。')
    except (OSError, ValueError):
        result['id'] = hashlib.sha256((tool + ':' + str(requested)).encode()).hexdigest()[:16]
    return result


def fingerprint(program):
    return file_fingerprint(resolve(program))


def file_fingerprint(file):
    path = Path(file).absolute()
    stat = path.stat()
    return dict(target=str(path.resolve()), size=stat.st_size, modified=stat.st_mtime_ns)


def python(requested=None):
    path = resolve(requested or sys.executable)
    if Path(path).name.lower() == 'pythonw.exe' and Path(path).with_name('python.exe').is_file():
        path = str(Path(path).with_name('python.exe'))
    result = run(path, ['-c', 'import json,sys; print(json.dumps(list(sys.version_info[:3])))'])
    value = json.loads(result.stdout) if result.returncode == 0 else []
    if not isinstance(value, list) or len(value) != 3 or tuple(value) < (3, 11, 0):
        raise ValueError('需要经过实际运行检查的 Python 3.11+。')
    return path
