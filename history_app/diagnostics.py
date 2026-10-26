"""Read-only health checks with an explicit, credential-free export schema."""
from collections import Counter
from contextlib import closing
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import sys

from . import configuration
from . import performance


def diagnose(paths, deep=False):
    report = dict(version=1, application='unknown', python=platform.python_version(),
                  system=platform.system(), sqlite=sqlite3.sqlite_version, dependencies={},
                  programs={}, configuration={}, database={'state': 'missing'}, jobs={}, installation={})
    local = dict(source=str(paths.source), shared=str(paths.shared), data=str(paths.data),
                 prefix=str(paths.prefix), python=sys.executable)
    try:
        report['application'] = (paths.source / 'VERSION').read_text(encoding='utf-8').strip()
    except OSError:
        pass
    for name, package in (('markdown', 'markdown-it-py'), ('highlighting', 'Pygments')):
        try:
            report['dependencies'][name] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            report['dependencies'][name] = 'missing'
    report['dependencies']['tk'] = importlib.util.find_spec('tkinter') is not None
    for name in ('codex', 'claude', 'powershell.exe' if os.name == 'nt' else 'xterm',
                 'schtasks.exe' if os.name == 'nt' else 'systemctl'):
        from . import programs
        try:
            location = programs.resolve(name)
        except ValueError:
            location = None
        report['programs'][name] = bool(location)
        if location:
            local[name] = location
    for name, path in paths.configuration().items():
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            valid = configuration.valid(name, value)
            report['configuration'][name] = 'ok' if valid else 'invalid'
        except FileNotFoundError:
            report['configuration'][name] = 'not-configured'
        except (OSError, ValueError, TypeError, KeyError):
            report['configuration'][name] = 'invalid'
    database = paths.data / 'history.db'
    if database.is_file():
        status = report['database']
        try:
            # mode=ro never creates/migrates a database. Do not call ah.connect().
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                db.execute('PRAGMA query_only=ON')
                status.update(state='ok', bytes=database.stat().st_size,
                              schema=db.execute('PRAGMA user_version').fetchone()[0])
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                status['tables'] = len(tables)
                if deep:
                    status['integrity'] = 'ok' if db.execute('PRAGMA quick_check(1)').fetchone()[0] == 'ok' else 'failed'
                if 'index_failures' in tables:
                    status['index_failures'] = db.execute('SELECT count(*) FROM index_failures').fetchone()[0]
                from .storage.read_model import ready
                db.row_factory = sqlite3.Row
                status['read_model'] = 'ready' if ready(db) else 'upgrade-needed'
                status['compatibility'] = 'supported' if status['schema'] in (3, 4, 5, 6) else 'needs-review'
        except (OSError, sqlite3.Error):
            status['state'] = 'unreadable'
    states, unreadable = Counter(), 0
    for path in (paths.data / 'launch-jobs').glob('*.json'):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            state = value.get('state')
            states[state if state in ('prepared', 'running', 'exited', 'failed', 'interrupted', 'cancelled') else 'unknown'] += 1
        except (OSError, ValueError, AttributeError):
            unreadable += 1
    report['jobs'] = dict(states=dict(states), unreadable=unreadable)
    try:
        value = json.loads((paths.prefix / 'installation.json').read_text(encoding='utf-8'))
        report['installation'] = dict(state='recorded', files=len(value.get('files', {})),
            tasks=len(value.get('tasks', [])), changed_files=sum(
                Path(path).exists() and hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest
                for path, digest in value.get('files', {}).items()))
    except FileNotFoundError:
        report['installation'] = dict(state='not-installed')
    except (OSError, ValueError, TypeError, AttributeError):
        report['installation'] = dict(state='unreadable')
    report['performance'] = performance.summary(paths.data)
    report['local_paths'] = local
    return report


def shareable(report):
    """Only allow fixed structural fields; never export paths, commands or error text."""
    return {key: report[key] for key in ('version', 'application', 'python', 'system', 'sqlite',
            'dependencies', 'programs', 'configuration', 'database', 'jobs', 'installation', 'performance')}


def render(report, include_paths=False):
    return json.dumps(report if include_paths else shareable(report), ensure_ascii=False, indent=2)
