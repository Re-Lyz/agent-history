"""Shared connection registry; configuration stays outside the source checkout."""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re
import uuid


_home_snapshots = ContextVar('connection_home_snapshots', default=None)


@contextmanager
def snapshot():
    """Resolve homes once per operation; never reuse them across refreshes."""
    if _home_snapshots.get() is not None:
        yield
        return
    token = _home_snapshots.set({})
    try:
        yield
    finally:
        _home_snapshots.reset(token)


def load(root):
    try:
        return json.loads((root / 'connections.json').read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {'connections': {}}


def homes(root):
    cache = _home_snapshots.get()
    if cache is not None and root in cache:
        return dict(cache[root])
    value = {name: (root / entry['home']).resolve()
             for name, entry in load(root)['connections'].items()}
    if cache is not None:
        cache[root] = value
    return dict(value)


def register(root, name, directory, key_env=None, code_mode_host=False):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
        raise ValueError('Connection name must use letters, numbers, underscores or hyphens.')
    if key_env and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key_env):
        raise ValueError('Invalid key environment variable name.')
    data = load(root)
    if name in data['connections']:
        raise ValueError('Connection already registered; edit connections.json to change it.')
    target = Path(directory).expanduser().resolve()
    if not (target / 'profile.toml').is_file():
        raise ValueError('Create the connection directory and profile.toml first.')
    if target in homes(root).values():
        raise ValueError('This home is already registered.')
    entry = dict(home=str(target), sync_history=True,
                 launch_args=['--enable', 'code_mode_host'] if code_mode_host else [])
    if key_env:
        entry['key_env'] = key_env
    data['connections'][name] = entry
    staged = root / ('connections-' + uuid.uuid4().hex + '.tmp')
    try:
        staged.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        staged.replace(root / 'connections.json')
        if _home_snapshots.get() is not None:
            _home_snapshots.get().pop(root, None)
    finally:
        staged.unlink(missing_ok=True)
