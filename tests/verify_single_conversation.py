"""One native file after each real Codex handoff; isolated, zero model turns."""
import base64
import contextlib
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
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
from tests.verify_temporary_handoff import append


def verify():
    with tempfile.TemporaryDirectory(prefix='verify-single-conversation-') as temporary:
        root = Path(temporary).resolve()
        if os.name != 'nt':
            stub = root / 'bin'
            stub.mkdir()
            program = stub / 'claude'
            program.write_text('#!/bin/sh\nexit 0\n')
            program.chmod(0o755)
            os.environ['PATH'] = str(stub) + os.pathsep + os.environ.get('PATH', '')
        homes = {name: root / name for name in ('a', 'b')}
        for home in homes.values():
            home.mkdir()
            (home / 'config.toml').write_text('model="gpt-6.1-sol"\n[features]\nmemories=false\n', encoding='utf-8')
            with CodexRPC(home):
                pass
        shared = root / 'shared'
        shared.mkdir()
        (shared / 'connections.json').write_text(json.dumps(dict(connections={
            name: dict(home=str(home)) for name, home in homes.items()})), encoding='utf-8')
        with patch.object(ah, 'ROOT', shared), patch.object(ah, 'DATA', root / 'data'), \
                patch.object(ah, 'CLAUDE_HOMES', [root / '.claude']), contextlib.closing(ah.connect()) as db:
            timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            sid = str(uuid.uuid4())
            path = homes['a'] / 'sessions' / (sid + '.jsonl')
            path.parent.mkdir(exist_ok=True)
            records = [dict(type='session_meta', timestamp=timestamp, payload=dict(
                id=sid, cwd=str(root), source='cli', history_mode='legacy', timestamp=timestamp,
                model_provider='openai', originator='codex_cli_rs', cli_version='0.0.0'))]
            path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
            append(dict(tool='codex', path=str(path), id=sid, cwd=str(root)), 'single-original', timestamp)
            ah.collect(db)
            current = dict(ah.resolve(db, sid))
            ci.register_native(homes['a'], sid, path, current, dict(history_mode='legacy'), ci.config(homes['a']))
            first = store.enroll(db, current)
            markers = ['single-original']
            for index, target in enumerate(('claude', 'b', 'a', 'claude', 'a')):
                previous = current
                prepared = backend.prepare_launch(db, current, target, str(root))
                ah.collect(db)
                current = dict(ah.resolve(db, prepared['session']))
                markers.append('single-added-' + str(index))
                append(current, markers[-1], timestamp)
                if index == 4:
                    # The independent reporter completes after the UI exits.
                    script = root / 'exit.ps1'
                    script.write_text('exit 0\n')
                    inner = ['-File', str(script)] if os.name == 'nt' else ['/bin/sh', str(script)]
                    payload = base64.b64encode(json.dumps(dict(job=prepared['job'], inner=inner)).encode()).decode()
                    result = subprocess.run([sys.executable, str(Path(runner.__file__)), payload],
                        env={**os.environ, **prepared['env'], 'USERPROFILE': str(root), 'HOME': str(root)},
                        capture_output=True, timeout=90)
                    assert result.returncode == 0, result.stderr
                else:
                    jobs.update(prepared['job'], state='exited', exit_code=0)
                    try:
                        lifecycle.finish(db, prepared['job'])
                    except ValueError:
                        if previous['tool'] == 'codex':
                            print(json.dumps(dict(step=index, expected=previous['path'],
                                native=ci.source_row(homes[previous['source']], previous['id']).get('rollout_path'))), flush=True)
                        raise
                report = jobs.read(prepared['job'])
                assert report['handoff_state'] == 'cleaned', report
                assert report['durable_session'] == current['id']
                assert not Path(previous['path']).exists()
                assert Path(current['path']).exists()
                if previous['tool'] == 'codex':
                    assert not ci.source_row(homes[previous['source']], previous['id'])
                    with CodexRPC(homes[previous['source']]) as rpc:
                        assert not any(row['id'] == previous['id'] for row in rpc.call('thread/list', dict(limit=100))['data'])
                text = native_text(homes[current['source']], current['id']) if current['tool'] == 'codex' else \
                    Path(current['path']).read_text(encoding='utf-8')
                assert all(marker in text for marker in markers), (index, markers)
                assert len({file.resolve() for _, _, file, _ in ah.discover() if file.is_file()}) == 1
                assert len(store.members(db, first['conversation'], include_merged=True)) == 1
                assert not db.execute('SELECT 1 FROM conversation_runs').fetchone()
            assert 'single-original' in json.dumps(store.revision(db, first['baseline'])['records'])
            print(json.dumps(dict(single_native_file=True, five_switches=True, native_resume=True,
                old_native_rows_removed=True, immutable_backups=True, independent_runner=True,
                claude_jsonl_only=True, model_requests=0)), flush=True)


if __name__ == '__main__':
    verify()
