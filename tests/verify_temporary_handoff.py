"""Real native resume and independent exit cleanup; synthetic data, zero model turns."""
import base64
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_history as ah
import codex_import as ci
import conversation_store as store
import handoff_lifecycle as lifecycle
import launch_backend as backend
import launch_job_runner as runner
import launch_jobs as jobs
from native_codex import CodexRPC
from tests.verify_handoff import native_text


def append(row, marker, timestamp):
    if row['tool'] == 'codex':
        records = []
        store.append_events(records, [dict(role='user', text=marker, ts=timestamp),
                                     dict(role='assistant', text=marker + '-answer', ts=timestamp)],
                            'codex', row['id'], row['cwd'])
    else:
        records = [dict(type=role, uuid=marker + role, sessionId=row['id'], cwd=row['cwd'],
                        timestamp=timestamp, message=dict(content=text, model='fixture-model'))
                   for role, text in [('user', marker), ('assistant', marker + '-answer')]]
    with Path(row['path']).open('a', encoding='utf-8') as stream:
        stream.write(''.join(json.dumps(record) + '\n' for record in records))


def verify():
    with tempfile.TemporaryDirectory(prefix='verify-temporary-handoff-') as temporary:
        root = Path(temporary).resolve()
        if os.name != 'nt':
            # This verifies Claude JSONL and lifecycle, not the Claude CLI.
            stub = root / 'claude-stub'; stub.mkdir()
            executable = stub / 'claude'
            executable.write_text('#!/bin/sh\nexit 0\n'); executable.chmod(0o755)
            os.environ['PATH'] = str(stub) + os.pathsep + os.environ.get('PATH', '')
        shared = root / 'shared'
        shared.mkdir()
        homes = {key: root / key for key in ('a', 'b')}
        for key, home in homes.items():
            home.mkdir()
            (home / 'config.toml').write_text('model="gpt-6.1-sol"\n[features]\nmemories=false\n', encoding='utf-8')
            with CodexRPC(home) as rpc:
                if key == 'a':
                    started = rpc.call('thread/start', dict(cwd=str(root), model='gpt-6.1-sol'))['thread']
        (shared / 'connections.json').write_text(json.dumps(dict(connections={key: dict(home=str(home))
                                                               for key, home in homes.items()})), encoding='utf-8')
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        path = Path(started['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        row = dict(id=started['id'], cwd=str(root), tool='codex')
        records = [dict(type='session_meta', timestamp=timestamp, payload=dict(id=row['id'], cwd=str(root), source='cli',
                   history_mode='legacy', timestamp=timestamp, model_provider='openai',
                   originator='codex_cli_rs', cli_version='0.0.0'))]
        path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
        append(dict(row, path=str(path)), 'temporary-original', timestamp)
        baseline = path.read_bytes()
        with patch.object(ah, 'ROOT', shared), patch.object(ah, 'DATA', root / 'data'), \
                patch.object(ah, 'CLAUDE_HOMES', [root / 'claude']), contextlib.closing(ah.connect()) as db:
            ah.collect(db)
            original = dict(ah.resolve(db, row['id']))
            ci.register_native(homes['a'], row['id'], path, original, dict(history_mode='legacy'), dict(model='gpt-6.1-sol'))
            for target, marker in [('claude', 'claude-added'), ('b', 'connection-added')]:
                prepared = backend.prepare_launch(db, original, target, str(root), policy='legacy')
                ah.collect(db)
                copied = dict(ah.resolve(db, prepared['session']))
                append(copied, marker, timestamp)
                jobs.update(prepared['job'], state='exited', exit_code=0)
                item = lifecycle.finish(db, prepared['job'])
                durable = json.loads(lifecycle.version(db, item['durable_version'])['row_json'])
                assert marker in native_text(homes['a'], durable['id'])
                assert not Path(copied['path']).exists()
                if target == 'b':
                    assert not ci.source_row(homes['b'], copied['id'])
                    with CodexRPC(homes['b']) as rpc:
                        assert not any(t['id'] == copied['id'] for t in rpc.call('thread/list', dict(limit=100))['data'])
                assert path.read_bytes() == baseline
                original = durable
            # The return direction starts with an original native Claude transcript.
            sid = '11111111-1111-4111-8111-111111111111'
            claude_path = ah.claude_project_dir(str(root)) / (sid + '.jsonl')
            claude_path.parent.mkdir(parents=True, exist_ok=True)
            claude_path.touch()
            append(dict(tool='claude', path=str(claude_path), id=sid, cwd=str(root)), 'claude-origin', timestamp)
            claude_baseline = claude_path.read_bytes()
            ah.collect(db)
            prepared = backend.prepare_launch(db, dict(ah.resolve(db, 'claude:' + sid)), 'b', str(root), policy='legacy')
            ah.collect(db)
            copied = dict(ah.resolve(db, prepared['session']))
            assert 'claude-origin' in native_text(homes['b'], copied['id'])
            append(copied, 'codex-return-added', timestamp)
            # A real separate runner process finishes after the UI process is gone.
            script = root / 'zero-model-exit.ps1'
            script.write_text('exit 0\n', encoding='utf-8')
            inner = ['-File', str(script)] if os.name == 'nt' else ['/bin/sh', str(script)]
            payload = base64.b64encode(json.dumps(dict(job=prepared['job'], inner=inner)).encode()).decode()
            result = subprocess.run([sys.executable, str(Path(runner.__file__)), payload],
                                    env={**os.environ, **prepared['env']}, capture_output=True, timeout=90)
            assert result.returncode == 0, result.stderr
            report = jobs.read(prepared['job'])
            assert report['handoff_state'] == 'cleaned', report
            item = lifecycle.get(db, prepared['job'])
            durable = json.loads(lifecycle.version(db, item['durable_version'])['row_json'])
            assert 'codex-return-added' in Path(durable['path']).read_text(encoding='utf-8')
            assert claude_path.read_bytes() == claude_baseline
            assert not Path(copied['path']).exists()
            assert not ci.source_row(homes['b'], copied['id'])
            print(json.dumps(dict(codex_claude_return=True, claude_codex_return=True, multi_connection=True,
                                  native_resume=True, native_cleanup=True, independent_runner=True,
                                  originals_preserved=True, model_requests=0)), flush=True)


if __name__ == '__main__':
    verify()
