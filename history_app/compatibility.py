"""Version-specific native validation, always using synthetic disposable homes."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import uuid

from . import programs
from .locks import file_lock

CHECKS = ('verify_ui_resume', 'verify_claude_to_codex', 'verify_handoff',
          'verify_temporary_handoff', 'verify_single_conversation', 'verify_resume_switch')


def latest(paths):
    rows = []
    for path in sorted((paths.data / 'maintenance/compatibility').glob('*.json'), reverse=True)[:20]:
        value = json.loads(path.read_text(encoding='utf-8'))
        rows.append(value)
    return rows


def check(paths, program, *, names=None, python=None):
    selected = tuple(CHECKS if names is None else names)
    if not selected or any(name not in CHECKS for name in selected):
        raise ValueError('未知原生兼容检查。')
    executable = programs.resolve(program)
    interpreter = programs.python(python)
    stamp = programs.fingerprint(executable)
    report = dict(version=1, id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8],
                  checked_at=datetime.now(timezone.utc).isoformat(), platform=platform.system(),
                  program=executable, program_version=programs.version(executable)['version'],
                  checks=[], model_requests=0, synthetic=True,
                  boundaries=['real-login', 'model-turns', 'interactive-tui', 'remote-control'])
    folder = paths.data / 'maintenance/compatibility'
    folder.mkdir(parents=True, exist_ok=True)
    with file_lock(paths.data / 'program-maintenance.lock', wait=False) as locked:
        if not locked:
            raise ValueError('程序更新或兼容检查正在运行。')
        with tempfile.TemporaryDirectory(prefix='agent-history-compat-') as temporary:
            scratch = Path(temporary)
            env = {**os.environ, 'HOME': temporary, 'USERPROFILE': temporary,
                   'AGENT_HISTORY_CODEX_PROGRAM': executable, 'PYTHONIOENCODING': 'utf-8',
                   'PATH': str(Path(executable).parent) + os.pathsep + os.environ.get('PATH', ''),
                   'AGENT_HISTORY_HOME': str(scratch / 'data'), 'AGENT_HISTORY_SHARED_ROOT': str(scratch / 'shared')}
            for key in ('CODEX_HOME', 'CODEX_SQLITE_HOME', 'CLAUDE_CONFIG_DIR', 'OPENAI_API_KEY',
                        'OPENAI_BASE_URL', 'CODEX_API_KEY', 'AGENT_HISTORY_TERMINAL'):
                env.pop(key, None)
            for name in selected:
                item = dict(name=name, state='failed')
                try:
                    result = subprocess.run([interpreter, str(paths.source / 'tests' / (name + '.py'))],
                        env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=300,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    log = folder / (report['id'] + '-' + name + '.txt')
                    with log.open('xb') as stream:
                        stream.write((result.stdout + b'\n' + result.stderr)[-100_000:])
                    item.update(state='passed' if result.returncode == 0 else 'failed', exit_code=result.returncode)
                except subprocess.TimeoutExpired:
                    item['state'] = 'timeout'
                except OSError:
                    item['state'] = 'unavailable'
                report['checks'].append(item)
    report['state'] = 'passed' if all(item['state'] == 'passed' for item in report['checks']) else 'failed'
    if programs.fingerprint(executable) != stamp:
        report['state'] = 'program-changed'
    with (folder / (report['id'] + '.json')).open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return report
