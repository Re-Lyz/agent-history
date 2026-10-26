"""Read-only program/account inventory. Never read auth or start a service."""
import json
import os
from pathlib import Path
import subprocess
import math

import connections
from . import programs


def policy(paths):
    try:
        value = json.loads((paths.data / 'program-policy.json').read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('version') != 1:
            raise ValueError('程序维护配置版本无效。')
        if not isinstance(value.get('pinned_accounts', []), list) or not isinstance(value.get('adapters', {}), dict):
            raise ValueError('程序维护配置字段无效。')
        return value
    except FileNotFoundError:
        return dict(version=1, pinned_accounts=[], adapters={})


def registrations(paths):
    rows = []
    for name, entry in connections.load(paths.shared).get('connections', {}).items():
        rows.append(dict(id=name, name=name, tool='codex', home=str((paths.shared / entry['home']).resolve()),
                         requested=entry.get('program') or 'codex', history_only=bool(entry.get('history_only'))))
    try:
        source = json.loads((paths.data / 'sources.json').read_text(encoding='utf-8'))
        homes = source.get('claude', [])
    except FileNotFoundError:
        homes = [str(paths.user / '.claude')] if (paths.user / '.claude').is_dir() else []
    for index, home in enumerate(homes):
        rows.append(dict(id='claude-' + str(index), name='Claude ' + str(index + 1), tool='claude',
                         home=str(Path(home).resolve()), requested='claude', history_only=False))
    try:
        profiles = json.loads((paths.data / 'launch-profiles.json').read_text(encoding='utf-8')).get('profiles', [])
    except FileNotFoundError:
        profiles = []
    for profile in profiles:
        import launch_profiles
        try:
            profile = launch_profiles.validate(profile)
        except (ValueError, TypeError):
            continue
        if not profile.get('program') or not profile.get('enabled', True):
            continue
        parent = next((row for row in rows if row['id'] == profile.get('connection')), None)
        rows.append(dict(id='profile:' + profile['id'], name=profile.get('name', profile['id']),
                         tool=profile['type'], home=parent['home'] if parent else None,
                         requested=profile['program'], history_only=False))
    return rows


def daemon(account, program):
    home = Path(account['home']) if account.get('home') else None
    binary = home / 'packages/app-server-daemon/current/bin' / ('codex.exe' if os.name == 'nt' else 'codex') if home else None
    if account['tool'] != 'codex' or not binary or not binary.is_file():
        return dict(state='not-installed', installed=None, running=None, pin='none')
    installed = programs.version(binary, home=home)
    result = dict(state='unreachable', installed=installed['version'], running=None, pin='managed')
    if installed['state'] == 'permission-denied':
        result['state'] = 'permission-denied'
    if not program:
        result['state'] = 'not-checked'
        return result
    try:
        response = programs.run(program, ['app-server', 'daemon', 'version'], home=home)
        value = json.loads(response.stdout) if response.returncode == 0 else {}
        if not isinstance(value, dict):
            return result
        status = value.get('status')
        if status in ('running', 'stopped', 'notRunning', 'not_running', 'not-running'):
            result['state'] = 'running' if status == 'running' else 'stopped'
        for source, target in (('managedCodexVersion', 'installed'), ('appServerVersion', 'running')):
            text = str(value.get(source) or '')
            match = programs.VERSION.fullmatch(text)
            if match:
                result[target] = text
    except PermissionError:
        result['state'] = 'permission-denied'
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def inventory(paths, *, probe=True):
    rules = policy(paths)
    groups, rows = {}, []
    for account in registrations(paths):
        # Different profiles may deliberately select different programs for one home.
        info = programs.describe(account['requested'], account['tool'], home=account['home'], probe=False)
        group = groups.get(info['id'])
        if group is None:
            group = programs.describe(account['requested'], account['tool'], home=account['home'], probe=probe)
            group['accounts'] = []
            groups[info['id']] = group
        account['program_id'] = info['id']
        account['daemon'] = daemon(account, group['path'] if group['state'] == 'ok' else None) if probe else dict(state='not-checked')
        account['pinned'] = account['id'] in rules.get('pinned_accounts', [])
        state = account['daemon']
        account['version_mismatch'] = bool(state.get('installed') and group['version'] and
            (state['installed'] != group['version'] or state.get('running') and state['running'] != group['version']))
        account['quota'] = quota(paths, account['id'])
        group['accounts'].append(account['id'])
        rows.append(account)
    try:
        from .compatibility import latest
        checks = latest(paths)
    except (OSError, ValueError):
        checks = []
    return dict(version=1, programs=list(groups.values()), accounts=rows, compatibility=checks)


def quota(paths, identity):
    """Optional provider adapters publish fixed-schema local cache entries."""
    import hashlib
    filename = hashlib.sha256(identity.encode()).hexdigest() + '.json'
    try:
        value = json.loads((paths.data / 'maintenance/quotas' / filename).read_text(encoding='utf-8'))
        if value.get('version') != 1 or not isinstance(value.get('limits'), list):
            raise ValueError('Invalid quota cache')
        limits = []
        for item in value['limits'][:10]:
            used = item.get('used_percent')
            reset = item.get('resets_at')
            if isinstance(used, (int, float)) and 0 <= used <= 100 and isinstance(reset, (int, float, type(None))) and \
                    (reset is None or math.isfinite(reset)):
                limits.append(dict(used_percent=used, resets_at=reset, window=str(item.get('window', 'custom'))[:40]))
        return dict(state='cached', source=str(value.get('source', 'provider'))[:80],
                    checked_at=str(value.get('checked_at', ''))[:40], limits=limits)
    except FileNotFoundError:
        return dict(state='unavailable', reason='尚无额度 provider 缓存；不影响手动启动。')
    except (OSError, ValueError, TypeError, AttributeError):
        return dict(state='invalid', reason='额度缓存无法读取。')


def cache_quota(paths, identity, value):
    """Provider adapter boundary: persist only numeric limits and provenance."""
    import hashlib
    from datetime import datetime, timezone
    from setup_backend import atomic_json
    if identity not in {row['id'] for row in registrations(paths)}:
        raise ValueError('额度缓存必须关联已登记账户。')
    limits = []
    for item in value.get('limits', [])[:10]:
        used, reset = item['used_percent'], item.get('resets_at')
        if type(used) not in (int, float) or not 0 <= used <= 100 or \
                (reset is not None and (type(reset) not in (int, float) or not math.isfinite(reset))):
            raise ValueError('额度数值无效。')
        limits.append(dict(used_percent=used, resets_at=reset, window=str(item.get('window', 'custom'))[:40]))
    filename = hashlib.sha256(identity.encode()).hexdigest() + '.json'
    atomic_json(paths.data / 'maintenance/quotas' / filename, dict(version=1, limits=limits,
        source=str(value.get('source', 'provider'))[:80], checked_at=datetime.now(timezone.utc).isoformat()))
    return quota(paths, identity)


def shareable(report):
    return dict(version=1, programs=[{key: row[key] for key in ('tool', 'state', 'version', 'channel')}
                                    for row in report['programs']],
                accounts=[dict(tool=row['tool'], daemon={key: row['daemon'].get(key) for key in ('state', 'installed', 'running', 'pin')}, pinned=row['pinned'],
                               version_mismatch=row['version_mismatch']) for row in report['accounts']])


def set_pinned(paths, identity, enabled):
    from . import config_backup
    from .locks import file_lock
    from setup_backend import atomic_json
    if identity not in {item['id'] for item in registrations(paths)}:
        raise ValueError('账户已变化，请重新扫描。')
    with file_lock(paths.data / 'program-maintenance.lock', wait=False) as locked:
        if not locked:
            raise ValueError('程序维护正在运行。')
        value = policy(paths)
        config_backup.backup(paths)
        identities = set(value.get('pinned_accounts', []))
        identities.add(identity) if enabled else identities.discard(identity)
        value['pinned_accounts'] = sorted(identities)
        atomic_json(paths.data / 'program-policy.json', value)
