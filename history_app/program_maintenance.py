"""Reviewed program updates with partial results; never restore history databases."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from . import accounts, programs, config_backup
from .locks import file_lock
from .operations import idle_operation
from setup_backend import atomic_json


def _stamps(paths):
    return {key: config_backup.fingerprint(path)[0] for key, path in paths.configuration().items()}


def update_command(program, rules):
    adapter = rules.get('adapters', {}).get(program['id'])
    if adapter:
        command, args = adapter.get('program'), adapter.get('args', [])
        if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError('更新适配器必须指定程序和逐项参数。')
        return command, [item.replace('{program}', program['path']) for item in args]
    if program['channel'] == 'standalone' and program['tool'] == 'codex':
        return program['path'], ['update']
    if program['channel'] == 'npm':
        package = {'codex': '@openai/codex', 'claude': '@anthropic-ai/claude-code'}.get(program['tool'])
        if not package or not program.get('npm_prefix'):
            raise ValueError('无法核对 npm 包名与安装归属。')
        return 'npm', ['install', '-g', '--prefix', program['npm_prefix'], package + '@latest']
    if program['channel'] == 'brew' and program['tool'] == 'codex':
        return 'brew', ['upgrade', '--cask', 'codex']
    raise ValueError('此安装方式需要显式更新适配器。')


def preview(paths, identity, *, probe=True):
    report = accounts.inventory(paths, probe=probe)
    program = next((item for item in report['programs'] if item['id'] == identity), None)
    if not program or not program['path']:
        raise ValueError('请选择已安装的程序。')
    rules = accounts.policy(paths)
    adapter = rules.get('adapters', {}).get(identity)
    if not adapter and program['channel'] not in ('standalone', 'npm', 'brew'):
        raise ValueError('安装来源未知；请在 program-policy.json 配置本地更新适配器。')
    steps = [dict(stage='cli', channel=program['channel'], target='latest')]
    related = [item for item in report['accounts'] if item['program_id'] == identity]
    for item in related:
        state = item['daemon']['state']
        steps.append(dict(stage='daemon', account=item['id'],
                          action='preserve-pin' if item['pinned'] else 'skip-missing' if state == 'not-installed' else 'sync-existing',
                          observed_state=state))
    command, args = update_command(program, rules)
    executable = programs.resolve(command)
    expected_files = {executable: programs.fingerprint(executable)}
    for value in args:
        if Path(value).is_file():
            expected_files[str(Path(value).absolute())] = programs.file_fingerprint(value)
    return dict(version=1, program=program, accounts=related, steps=steps,
                cli_operation=dict(program=executable, args=args), expected_files=expected_files,
                expected=programs.fingerprint(program['path']), config_expected=_stamps(paths),
                note='更新 CLI 后仅同步已有后台包并固定到该 CLI；运行中的服务不会被强制替换。')


def _update_cli(paths, program, rules):
    adapter = rules.get('adapters', {}).get(program['id'])
    command, args = update_command(program, rules)
    result = programs.run(command, args, home=program.get('owner_home'), timeout=600)
    if result.returncode and not adapter and program['channel'] == 'standalone':
        from .release_install import fallback
        return dict(state='passed', fallback=fallback(paths, program['installation']))
    if result.returncode:
        raise ValueError('CLI 更新命令失败；旧版本及诊断记录保留。')
    return dict(state='passed')


def _idle_accounts(rows):
    from .adapters.codex import writer_lock
    for row in rows:
        if row['tool'] != 'codex' or not row.get('home'):
            continue
        for path in (Path(row['home']) / 'thread-writer-locks').glob('*.lock'):
            with writer_lock(row['home'], path.stem) as locked:
                if not locked:
                    raise ValueError('此程序仍有原生会话正在使用，请正常退出后重试。')
        if row['daemon']['state'] == 'running':
            raise ValueError('后台服务仍在运行，请先正常结束后台任务并停止该服务。')


def apply(paths, plan, *, verify=True):
    identity = plan['program']['id']
    journal = dict(version=1, id=uuid.uuid4().hex, started=datetime.now(timezone.utc).isoformat(),
                   program_id=identity, stages=[], state='running')
    report_path = paths.data / 'maintenance/program-updates' / (journal['id'] + '.json')
    rules = accounts.policy(paths)
    with idle_operation(paths), file_lock(paths.data / 'program-maintenance.lock', wait=False) as locked:
        if not locked:
            raise ValueError('另一个程序维护任务正在运行。')
        if _stamps(paths) != plan['config_expected'] or programs.fingerprint(plan['program']['path']) != plan['expected']:
            raise ValueError('配置或程序在预览后变化，请重新预览。')
        for file, expected in plan.get('expected_files', {}).items():
            if programs.file_fingerprint(file) != expected:
                raise ValueError('更新程序或脚本在预览后变化，请重新预览。')
        current = accounts.inventory(paths)
        rows = [item for item in current['accounts'] if item['program_id'] == identity]
        _idle_accounts(rows)
        journal['backup'] = config_backup.backup(paths)
        atomic_json(report_path, journal)
        try:
            result = _update_cli(paths, plan['program'], rules)
            journal['stages'].append(dict(stage='cli', **result))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            journal['stages'].append(dict(stage='cli', state='failed', reason=type(error).__name__, detail=str(error)[:1200]))
            journal['state'] = 'failed'
            atomic_json(report_path, journal)
            return journal
        # Resolve again after the update; never keep a release-specific cached path.
        refreshed = programs.describe(plan['program']['requested'], plan['program']['tool'], home=plan['program']['owner_home'])
        journal['after'] = {key: refreshed[key] for key in ('path', 'version', 'state')}
        if not refreshed['path'] or refreshed['state'] not in ('ok', 'installed'):
            journal['state'] = 'needs-review'
            atomic_json(report_path, journal)
            return journal
        seen = set()
        for row in rows:
            if row.get('home') in seen:
                continue
            seen.add(row.get('home'))
            item = dict(stage='daemon', account=row['id'])
            if any(other['pinned'] for other in rows if other.get('home') == row.get('home')):
                item['state'] = 'preserved-pin'
            elif row['daemon']['state'] == 'not-installed':
                item['state'] = 'not-installed'
            else:
                state = accounts.daemon(row, refreshed['path'])
                if state['state'] != 'stopped':
                    item.update(state='blocked', reason=state['state'])
                elif state.get('installed') == refreshed['version'] and state.get('running') in (None, refreshed['version']):
                    item.update(state='already-current', after=state)
                else:
                    try:
                        result = programs.run(refreshed['path'], ['app-server', 'daemon', 'update', '--from-cli', '--yes'],
                                              home=row['home'], timeout=180)
                        after = accounts.daemon(row, refreshed['path'])
                        item.update(state='passed' if result.returncode == 0 and after.get('installed') == refreshed['version']
                                    and after.get('running') in (None, refreshed['version']) else 'failed', after=after)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        item['state'] = 'failed'
            journal['stages'].append(item)
            atomic_json(report_path, journal)
        journal['state'] = 'partial' if any(item['state'] in ('failed', 'blocked') for item in journal['stages']) else 'updated'
        atomic_json(report_path, journal)
    if verify and plan['program']['tool'] == 'codex' and refreshed['state'] == 'ok':
        from .compatibility import check
        try:
            result = check(paths, refreshed['path'], python=rules.get('python'))
            journal['compatibility'] = result['id']
            if result['state'] != 'passed':
                journal['state'] = 'needs-review'
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            journal['state'] = 'needs-review'
            journal['compatibility_error'] = type(error).__name__
        atomic_json(report_path, journal)
    elif verify and plan['program']['tool'] == 'codex':
        journal.update(state='needs-review', compatibility_error='native-program-not-verified')
        atomic_json(report_path, journal)
    return journal
