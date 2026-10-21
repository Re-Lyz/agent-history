"""Headless maintenance entry point. Mutations require an explicit --apply."""
import argparse
from dataclasses import replace
import json
from pathlib import Path

from .paths import RuntimePaths
from . import config_backup, diagnostics, installation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('diagnose', 'backup', 'backups', 'restore-config', 'upgrade', 'uninstall'))
    parser.add_argument('--shared-root', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--prefix', type=Path)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--backup-id')
    parser.add_argument('--deep', action='store_true', help='Read-only SQLite quick_check.')
    parser.add_argument('--apply', action='store_true', help='Execute a reviewed restore/update/uninstall.')
    args = parser.parse_args(argv)
    paths = RuntimePaths.current(args.prefix)
    paths = replace(paths, **{key: value.expanduser().resolve() for key, value in
        (('shared', args.shared_root), ('data', args.data_dir), ('source', args.source_root)) if value is not None})
    if args.action == 'diagnose':
        print(diagnostics.render(diagnostics.diagnose(paths, args.deep)))
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
