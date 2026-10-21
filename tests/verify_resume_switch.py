"""Native writer-lock handoff and terminal/session separation; zero model turns.

This checks the actual app-server lock boundary, not the interactive TUI picker.
"""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_history as ah
import codex_import as ci
import launch_backend as backend
import launch_jobs as jobs
import launch_job_runner as runner
import single_conversation as single
from native_codex import CodexRPC
from tests.verify_handoff import native_text


def verify():
    with tempfile.TemporaryDirectory(prefix='verify-native-switch-') as temporary:
        root = Path(temporary).resolve()
        home, shared = root / 'codex', root / 'shared'
        home.mkdir()
        shared.mkdir()
        (home / 'config.toml').write_text('model="gpt-6.1-sol"\n[features]\nmemories=false\n', encoding='utf-8')
        with CodexRPC(home) as rpc:
            thread = rpc.call('thread/start', dict(cwd=str(root), model='gpt-6.1-sol'))['thread']
        sid, path = thread['id'], Path(thread['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        records = [dict(type='session_meta', timestamp=stamp, payload=dict(id=sid, cwd=str(root), source='cli',
            timestamp=stamp, history_mode='legacy', model_provider='openai', originator='codex_cli_rs', cli_version='0.0.0')),
            dict(type='response_item', timestamp=stamp, payload=dict(type='message', role='user',
                content=[dict(type='input_text', text='synthetic-native-switch-marker')])),
            dict(type='event_msg', timestamp=stamp, payload=dict(type='user_message', message='synthetic-native-switch-marker')),
            dict(type='response_item', timestamp=stamp, payload=dict(type='message', role='assistant',
                content=[dict(type='output_text', text='synthetic saved answer')])),
            dict(type='event_msg', timestamp=stamp, payload=dict(type='agent_message', message='synthetic saved answer'))]
        path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
        (shared / 'connections.json').write_text(json.dumps(dict(connections={'a': dict(home=str(home), direct=True)})), encoding='utf-8')
        with patch.object(ah, 'ROOT', shared), patch.object(ah, 'DATA', root / 'data'), \
                patch.object(ah, 'CLAUDE_HOMES', []), contextlib.closing(ah.connect()) as db:
            ah.collect(db)
            selected = dict(ah.resolve(db, sid))
            ci.register_native(home, sid, path, selected, dict(history_mode='legacy'), ci.config(home))
            prepared = backend.prepare_launch(db, selected, 'a', str(root))
            single.claim(db, prepared['job'])
            jobs.claim(prepared['job'])
            with CodexRPC(home) as rpc:
                rpc.call('thread/resume', dict(threadId=sid))
                assert runner.observe_native_session(prepared['job'], selected, home)
                try:
                    single.detach_session(db, prepared['job'])
                except ValueError:
                    pass
                else:
                    raise AssertionError('Native writer must prevent detachment.')
            before = path.read_bytes()
            single.detach_session(db, prepared['job'])
            report = jobs.read(prepared['job'])
            assert report['state'] == 'running' and report['session_detached']
            assert report['pid'] == os.getpid() and jobs.process_matches(report)
            assert report.get('exit_code') is None
            assert path.read_bytes() == before
            assert not db.execute('SELECT 1 FROM conversation_runs').fetchone()
            assert 'synthetic-native-switch-marker' in native_text(home, sid)
            newer = backend.prepare_launch(db, selected, 'a', str(root))
            jobs.update(prepared['job'], state='exited', exit_code=0)
            try:
                single.finish(db, prepared['job'])
            except ValueError:
                pass
            else:
                raise AssertionError('Old exit must not retire a newer reservation.')
            assert jobs.read(newer['job'])['state'] == 'prepared'
            single.cancel_prepared(db, newer['job'])
            assert path.exists() and ci.source_row(home, sid)
        print(json.dumps(dict(native_lock_observed=True, active_lock_blocks_release=True,
            idle_session_synced=True, terminal_still_running=True, files_preserved=True,
            late_cleanup_blocked=True, interactive_tui_tested=False, model_requests=0)))


if __name__ == '__main__':
    verify()
