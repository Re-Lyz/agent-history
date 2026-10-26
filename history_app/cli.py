"""Headless maintenance entry point. Mutations require an explicit --apply."""
import argparse
from dataclasses import replace
import json
from pathlib import Path

from .paths import RuntimePaths
from . import config_backup, diagnostics, installation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('diagnose', 'backup', 'backups', 'restore-config', 'upgrade', 'uninstall',
                                         'programs', 'program-plan', 'update-programs', 'check-native', 'workspace', 'project-context'))
    parser.add_argument('--shared-root', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--prefix', type=Path)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--backup-id')
    parser.add_argument('--program-id')
    parser.add_argument('--project')
    parser.add_argument('--query', default='')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--cursor', help='JSON cursor returned by project-context.')
    parser.add_argument('--redact', action='store_true')
    parser.add_argument('--deep', action='store_true', help='Read-only SQLite quick_check.')
    parser.add_argument('--apply', action='store_true', help='Execute a reviewed restore/update/uninstall.')
    args = parser.parse_args(argv)
    paths = RuntimePaths.current(args.prefix)
    paths = replace(paths, **{key: value.expanduser().resolve() for key, value in
        (('shared', args.shared_root), ('data', args.data_dir), ('source', args.source_root)) if value is not None})
    if args.action == 'diagnose':
        print(diagnostics.render(diagnostics.diagnose(paths, args.deep)))
        return
    if args.action in ('programs', 'program-plan', 'update-programs', 'check-native', 'workspace', 'project-context'):
        from . import accounts, program_maintenance, compatibility, workspace
        if args.action == 'programs':
            result = accounts.inventory(paths)
            if args.redact:
                result = accounts.shareable(result)
        elif args.action == 'workspace':
            result = workspace.snapshot(paths, limit=args.limit, offset=args.offset)
        elif args.action == 'project-context':
            result = workspace.context(paths, args.project, args.query, limit=args.limit, offset=args.offset,
                                       cursor=json.loads(args.cursor) if args.cursor else None)
        else:
            if not args.program_id:
                parser.error('--program-id is required')
            if args.action == 'check-native':
                entry = next((item for item in accounts.inventory(paths)['programs'] if item['id'] == args.program_id), None)
                if not entry or entry['tool'] != 'codex' or entry['state'] != 'ok':
                    parser.error('请选择已安装的 Codex 程序。')
                result = compatibility.check(paths, entry['path'], python=accounts.policy(paths).get('python'))
            else:
                plan = program_maintenance.preview(paths, args.program_id)
                result = program_maintenance.apply(paths, plan) if args.action == 'update-programs' and args.apply else plan
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.action == 'backup':
        result = dict(backup=config_backup.backup(paths))
    elif args.action == 'backups':
        result = config_backup.inventory(paths)
    elif args.action == 'restore-config':
        if not args.backup_id:
            parser.error('--backup-id is required for restore-config')
        plan = config_backup.preview_restore(paths, args.backup_id)
        result = config_backup.restore(paths, plan) if args.apply else dict(changes=plan['changes'], applied=False)
    else:
        plan = installation.preview(paths, args.action)
        result = installation.apply(paths, plan) if args.apply else {
            key: plan[key] for key in ('action', 'changes', 'kept', 'tasks')}
    print(json.dumps(result, ensure_ascii=False, indent=2))
