"""Discover and select existing local histories; never read or replace authentication."""
import json
import gzip
import io
import os
from pathlib import Path
import shutil
import tempfile
import uuid

import agent_history as ah
import connections


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, delete=False) as stream:
            staged = Path(stream.name)
            if path.suffix == '.gz':
                with io.TextIOWrapper(gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0),
                                      encoding='utf-8') as writer:
                    json.dump(value, writer, ensure_ascii=False, separators=(',', ':'))
            else:
                stream.write((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    finally:
        if staged:
            staged.unlink(missing_ok=True)


def load_sources():
    try:
        value = json.loads((ah.DATA / 'sources.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or value.get('version') != 1 or \
            not isinstance(value.get('codex'), list) or not isinstance(value.get('claude'), list) or \
            any(not isinstance(item, str) for item in value['codex'] + value['claude']):
        raise ValueError('sources.json: expected version 1 with codex and claude arrays.')
    return value


def source_enabled(row, scope):
    if scope is None:
        return True
    return row['source'] in scope['codex'] if row['tool'] == 'codex' else \
        str(Path(row['path']).parent.parent.parent) in scope['claude']


def discover(user_home=None):
    home = Path(user_home) if user_home else Path.home()
    registry = connections.load(ah.ROOT)['connections']
    candidates, seen = [], set()
    for name, entry in registry.items():
        directory = (ah.ROOT / entry['home']).resolve()
        candidates.append(dict(tool='codex', name=entry.get('name', name), connection=name,
                               home=str(directory), registered=True))
        seen.add(str(directory))
    possible = [home / '.codex', *sorted(home.glob('.codex-*'))]
    configured = os.environ.get('CODEX_HOME')
    if configured:
        possible.insert(0, Path(configured))
    for directory in possible:
        directory = directory.resolve()
        if str(directory) not in seen and directory.is_dir() and \
                ((directory / 'config.toml').exists() or (directory / 'sessions').is_dir()):
            candidates.append(dict(tool='codex', name=directory.name, connection='',
                                   home=str(directory), registered=False))
            seen.add(str(directory))
    claude_paths = list(ah.CLAUDE_HOMES) if user_home is None else [home / '.claude']
    selected = load_sources()
    if selected:
        claude_paths += [Path(path) for path in selected['claude']]
    for directory in dict.fromkeys(path.resolve() for path in claude_paths):
        if directory.is_dir():
            candidates.append(dict(tool='claude', name='Claude', connection='claude',
                                   home=str(directory), registered=True))
    return dict(candidates=candidates, programs={tool: shutil.which(tool) for tool in ('codex', 'claude')})


def configure(candidates):
    """Persist user-selected histories; direct Codex homes keep their existing config."""
    if not candidates:
        raise ValueError('请至少选择一个历史目录。')
    registry = connections.load(ah.ROOT)
    codex, claude, profiles = [], [], []
    import launch_profiles
    existing, errors = launch_profiles.load()
    if errors:
        raise ValueError('请先修复启动配置：' + '; '.join(errors))
    profiles = list(existing.values())
    for candidate in candidates:
        tool = candidate['tool']
        directory = Path(candidate['home']).expanduser().resolve()
        if not directory.is_dir():
            raise ValueError('历史目录不存在：' + str(directory))
        name = str(candidate.get('name', '')).strip()
        if not name:
            raise ValueError('显示名称不能为空。')
        if tool == 'claude':
            if claude:
                raise ValueError('请选择一个 Claude 目录；可重新运行向导切换。')
            claude.append(str(directory))
            prior = next((item for item in profiles if item['id'] == 'setup_claude'), None)
            if prior:
                prior['name'] = name
            else:
                profiles.append(dict(id='setup_claude', name=name, type='claude'))
        elif tool == 'codex':
            connection = next((key for key, entry in registry['connections'].items()
                               if (ah.ROOT / entry['home']).resolve() == directory), None)
            if connection is None:
                if not (directory / 'config.toml').is_file():
                    raise ValueError('所选 Codex 目录缺少已有的 config.toml，请先用 Codex 完成配置。')
                connection = 'local_' + uuid.uuid4().hex[:10]
                registry['connections'][connection] = dict(home=str(directory), direct=True,
                                                          name=name, sync_history=False)
            codex.append(connection)
            if (directory / 'config.toml').is_file() and not (directory / 'profile.toml').is_file():
                registry['connections'][connection]['direct'] = True
            if not registry['connections'][connection].get('history_only'):
                profile_id = 'setup_' + connection
                profile = dict(id=profile_id, name=name, type='codex', connection=connection)
                if registry['connections'][connection].get('direct'):
                    profile['program'] = 'codex'
                prior = next((item for item in profiles if item['id'] == profile_id), None)
                if prior:
                    prior['name'] = name
                else:
                    profiles.append(profile)
        else:
            raise ValueError('未知历史类型：' + str(tool))
    # Validate everything before any persistent mutation. Re-running recovers a
    # partial multi-file publication without duplicating homes or profiles.
    for profile in profiles:
        launch_profiles.validate(profile)
    atomic_json(ah.ROOT / 'connections.json', registry)
    launch_profiles.save(profiles)
    atomic_json(ah.DATA / 'sources.json', dict(version=1, codex=codex, claude=claude))
    return dict(codex=codex, claude=claude)
