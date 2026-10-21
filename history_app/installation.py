"""Review and apply updates to an existing, explicitly selected installation."""
import json
from pathlib import Path
import subprocess
import sys

from . import config_backup, installer
from .operations import idle_operation


def _source_stamp(paths):
    required = ('install.py', 'agent_history.py', 'history_ui.py', 'VERSION', 'requirements-ui.txt')
    if any(not (paths.source / name).is_file() for name in required):
        raise ValueError('新版源码目录不完整，请选择解压后的 Agent History 源码目录。')
    files = [*paths.source.glob('*.py'), *(paths.source / 'history_app').rglob('*.py'),
             paths.source / 'VERSION', paths.source / 'requirements-ui.txt']
    return {str(path.relative_to(paths.source)): config_backup.fingerprint(path)[0] for path in files}


def _journal(paths):
    path = paths.prefix / 'installation.json'
    digest, data = config_backup.fingerprint(path)
    if data is None:
        raise ValueError('此目录没有安装记录；源码直接运行无需卸载，可先选择原安装目录。')
    value = json.loads(data.decode('utf-8'))
    if Path(value['config']).resolve() != paths.shared.resolve() or \
            Path(value['data']).resolve() != paths.data.resolve():
        raise ValueError('安装记录使用另一组配置或数据目录，未修改。')
    return digest, value


def _stamps(paths, journal):
    # Detect edits between review and execution, including changes to integrations.
    files = {str(paths.prefix / 'installation.json'), *journal.get('files', {})}
    files.update(item['path'] for item in journal.get('registrations', {}).values())
    return {name: config_backup.fingerprint(name)[0] for name in files}


def preview(paths, action, python=None):
    if action not in ('upgrade', 'uninstall'):
        raise ValueError('未知维护操作。')
    _, journal = _journal(paths)
    spec = None
    if action == 'upgrade':
        source_expected = _source_stamp(paths)
        if installer.uninstall(paths.prefix, dry_run=True)['kept']:
            raise ValueError('已登记的入口或集成被用户修改，请先核对；更新不会覆盖这些修改。')
        features = journal.get('features', {})
        spec = installer.plan(paths.prefix, Path(journal.get('user_home', paths.user)), python or sys.executable,
            shared_root=paths.shared, data_dir=paths.data, source_root=paths.source,
            desktop=journal.get('desktop', any(Path(name).parent.name == 'Desktop' for name in journal.get('files', {}))),
            **{name: bool(features.get(name)) for name in ('codex_mcp', 'claude_mcp', 'claude_hook')},
            collect_task='AgentHistoryCollect' in journal.get('tasks', []),
            sync_task='AgentHistorySyncClaude' in journal.get('tasks', []))
        changes = list(spec['files']) + spec['shortcuts']
        kept = []
    else:
        source_expected = None
        result = installer.uninstall(paths.prefix, dry_run=True)
        changes, kept = result['removed'], result['kept']
    return dict(action=action, changes=changes, kept=kept, tasks=list(journal.get('tasks', [])),
                expected=_stamps(paths, journal), spec=spec, source_expected=source_expected,
                config_expected={name: config_backup.fingerprint(path)[0]
                                 for name, path in paths.configuration().items()})


def apply(paths, plan):
    with idle_operation(paths):
        _, journal = _journal(paths)
        if plan['action'] == 'upgrade' and _source_stamp(paths) != plan['source_expected']:
            raise ValueError('新版源码在预览后发生变化，请重新预览。')
        if _stamps(paths, journal) != plan['expected'] or \
                {name: config_backup.fingerprint(path)[0] for name, path in paths.configuration().items()} != plan['config_expected']:
            raise ValueError('安装登记或配置在预览后发生变化，请重新预览。')
        identity = config_backup.backup(paths)
        # Keep the original installation journal privately; it may contain old env settings.
        folder = paths.backups / identity
        config_backup._write(folder / 'installation-before.bin', (paths.prefix / 'installation.json').read_bytes())
        try:
            result = installer.install(plan['spec']) if plan['action'] == 'upgrade' else installer.uninstall(paths.prefix)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            raise ValueError('维护未完成；已保留配置备份 ' + identity +
                             ' 和安装恢复记录。可修复错误后重试，或预览卸载撤销已登记的步骤。') from error
        if plan['action'] == 'upgrade':
            # Never return registrations/before values: they can contain private settings.
            result = dict(updated=True, files=len(result['files']), tasks=len(result.get('tasks', [])))
        return dict(result=result, backup=identity)
