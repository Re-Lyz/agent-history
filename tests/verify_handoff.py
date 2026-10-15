"""Verify native Codex round-trip handoffs with synthetic history and no model turns."""
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
import conversation_store as store
import launch_backend as backend
import setup_backend
import ui_support
from native_codex import CodexRPC


def native_text(home, sid):
    with CodexRPC(home) as rpc:
        assert any(item['id'] == sid for item in rpc.call('thread/list', dict(limit=100))['data'])
        result = rpc.call('thread/resume', dict(threadId=sid))
        turns = result['thread'].get('turns') or rpc.call('thread/turns/list', dict(
            threadId=sid, limit=100, itemsView='full'))['data']
        return json.dumps(turns, ensure_ascii=False)


def verify():
    with tempfile.TemporaryDirectory(prefix='verify-handoff-') as temporary:
        root = Path(temporary).resolve()
        if os.name != 'nt':
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
        sid = started['id']
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        path = Path(started['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [dict(type='session_meta', timestamp=timestamp, payload=dict(id=sid, cwd=str(root), source='cli',
                   history_mode='legacy', timestamp=timestamp, model_provider='openai',
                   originator='codex_cli_rs', cli_version='0.0.0'))]
        store.append_events(records, [dict(role='user', text='synthetic-handoff-original', ts=timestamp),
                                      dict(role='assistant', text='synthetic-original-answer', ts=timestamp)],
                            'codex', sid, str(root))
        path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
        baseline = path.read_bytes()
        with patch.object(ah, 'ROOT', shared), patch.object(ah, 'DATA', root / 'data'), \
                patch.object(ah, 'CLAUDE_HOMES', [root / 'claude']), contextlib.closing(ah.connect()) as db:
            ah.collect(db)
            original = dict(ah.resolve(db, sid))
            ci.register_native(homes['a'], sid, path, original, dict(history_mode='legacy'), dict(model='gpt-6.1-sol'))
            claude_home = root / 'claude'
            claude_home.mkdir()
            setup_backend.configure([dict(tool='codex', name=key, home=str(home)) for key, home in homes.items()] +
                                    [dict(tool='claude', name='Claude', home=str(claude_home))])
            ready = backend.test_launch(db, original, 'profile:setup_a', str(root))
            assert ready['ready']
            prepared = backend.prepare_launch(db, original, 'claude', str(root), policy='legacy')
            ah.collect(db)
            claude = dict(ah.resolve(db, prepared['session']))
            with Path(claude['path']).open('a', encoding='utf-8') as stream:
                for index, (role, text) in enumerate([('user', 'synthetic-claude-added'), ('assistant', 'synthetic-claude-answer')]):
                    stream.write(json.dumps(dict(type=role, uuid='added-' + str(index), sessionId=claude['id'],
                        cwd=str(root), timestamp=timestamp, message=dict(content=text, model='fixture-model'))) + '\n')
            returned = backend.prepare_launch(db, claude, 'a', str(root), policy='legacy')
            assert returned['session'] != sid
            text = native_text(homes['a'], returned['session'])
            assert 'synthetic-handoff-original' in text and 'synthetic-claude-added' in text
            assert ah.IMPORT_MARK not in text
            ah.collect(db)
            selected = dict(ah.resolve(db, returned['session']))
            transferred = backend.prepare_launch(db, selected, 'b', str(root), policy='legacy')
            assert transferred['session'] not in (sid, returned['session'])
            text = native_text(homes['b'], transferred['session'])
            assert 'synthetic-handoff-original' in text and 'synthetic-claude-added' in text
            assert 'synthetic-claude-answer' in native_text(homes['b'], transferred['session'])
            assert path.read_bytes() == baseline
            print(json.dumps(dict(claude_roundtrip=True, imported_copy_handoff=True,
                                  first_setup=True, launch_check=True,
                                  listed=True, resumed=True, restarted=True, originals_preserved=True,
                                  model_requests=0)), flush=True)


if __name__ == '__main__':
    verify()
