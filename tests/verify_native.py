"""Verify installed Codex list/resume/history in disposable homes; no model calls.

Usage: python tests/verify_native.py --source-session UUID_OR_PREFIX
Source originals are read only. Temporary artifacts are removed on completion.
"""
import argparse
import contextlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

# Allow direct invocation by path, including from outside the repository.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_history as ah
import codex_import as ci
from native_codex import CodexRPC


def verify(ref):
    with contextlib.closing(ci.readonly(ah.DATA / 'history.db')) as history:
        original = dict(ah.resolve(history, ref))
    homes = dict(ah.codex_homes())
    origin_row = ci.source_row(homes[original['source']], original['id'])
    scratch = ah.DATA / 'verification'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='verify-native-', dir=scratch) as temp, contextlib.ExitStack() as resources:
        root = Path(temp)
        db = sqlite3.connect(root / 'catalog.db')
        resources.callback(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(ci.SCHEMA)
        for mode in ('legacy', 'paginated'):
            source, target = root / (mode + '-source'), root / (mode + '-target')
            for home in (source, target):
                home.mkdir()
                (home / 'config.toml').write_text('model="gpt-6.1-sol"\n[features]\nmemories=false\n', encoding='utf-8')
                with CodexRPC(home):
                    pass  # Initialize native schemas without starting a conversation.
            path = source / 'sessions' / Path(original['path']).name
            path.parent.mkdir()
            chain = ci.ancestry(homes[original['source']], original['path'])
            ci.snapshot(chain, path, original['id'], original, 'openai', 'gpt-6.1-sol')
            # The source fixture is an original, not an imported copy.
            records = [json.loads(line) for line in path.read_bytes().splitlines()]
            records[0]['payload'].pop('agent_history_import', None)
            records[0]['payload']['history_mode'] = mode
            if mode == 'legacy':
                # Genuine legacy rollouts use user_message/agent_message display
                # events; modern item_completed events cannot substitute for them.
                legacy = []
                for record in records:
                    legacy.append(record)
                    p = record.get('payload') or {}
                    if record.get('type') == 'response_item' and p.get('type') == 'message' and p.get('role') in ('user', 'assistant'):
                        text = '\n'.join(c.get('text', '') for c in p.get('content', []))
                        if text and not ah.injected(text):
                            legacy.append({'type': 'event_msg', 'timestamp': record.get('timestamp'), 'payload': {
                                'type': 'user_message' if p['role'] == 'user' else 'agent_message', 'message': text}})
                records = legacy
            path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
            session = dict(original, path=str(path), source=mode, kind='main')
            origin = dict(origin_row, history_mode=mode)
            ci.register_native(source, original['id'], path, session, origin, {'model': 'gpt-6.1-sol'})
            baseline = path.read_bytes()
            imported = ci.import_session(db, session, source, mode + '-target', target)
            sid = imported['session']
            with CodexRPC(target) as rpc:
                listed = rpc.call('thread/list', {'limit': 100})['data']
                assert any(t['id'] == sid for t in listed), 'Imported session absent from default native list'
                result = rpc.call('thread/resume', {'threadId': sid, 'excludeTurns': mode == 'paginated'})
                assert result['thread']['id'] == sid
                assert result['model'] == 'gpt-6.1-sol', result['model']
                turns = (rpc.call('thread/turns/list', {'threadId': sid, 'limit': 100, 'itemsView': 'full'})['data']
                         if mode == 'paginated' else result['thread']['turns'])
                if not turns:
                    turns = rpc.call('thread/turns/list', {'threadId': sid, 'limit': 100, 'itemsView': 'full'})['data']
                assert turns and any(t.get('items') for t in turns), 'No restored history items'
                with ci.writer_lock(target, sid) as locked:
                    assert not locked, 'Native resumed thread is not protected by importer lock'
            assert path.read_bytes() == baseline, 'Source fixture was modified'
            fork_import = None
            if mode == 'paginated':
                with CodexRPC(source) as rpc:
                    fork = rpc.call('thread/fork', {'threadId': original['id'], 'excludeTurns': True,
                                                  'deferGoalContinuation': True})['thread']
                fork_session = dict(session, id=fork['id'], key='codex:' + fork['id'], path=fork['path'])
                fork_import = ci.import_session(db, fork_session, source, mode + '-target', target)
                assert 'history_base' not in ci.first_meta(fork_import['path']), 'Native fork dependency not flattened'
            # Verify independence from the source connection and survival across restart.
            path.unlink()
            with CodexRPC(target) as rpc:
                restored = rpc.call('thread/resume', {'threadId': sid, 'excludeTurns': mode == 'paginated'})
                again = (rpc.call('thread/turns/list', {'threadId': sid, 'limit': 100, 'itemsView': 'full'})['data']
                         if mode == 'paginated' else restored['thread']['turns'])
                if not again:
                    again = rpc.call('thread/turns/list', {'threadId': sid, 'limit': 100, 'itemsView': 'full'})['data']
                assert len(again) == len(turns)
                if fork_import:
                    rpc.call('thread/resume', {'threadId': fork_import['session'], 'excludeTurns': True})
                    fork_turns = rpc.call('thread/turns/list', {'threadId': fork_import['session'],
                                          'limit': 100, 'itemsView': 'full'})['data']
                    assert sum(len(t.get('items', [])) for t in fork_turns) >= sum(len(t.get('items', [])) for t in turns)
            print(json.dumps({'mode': mode, 'listed': True, 'resumed': True, 'turns': len(turns),
                              'items': sum(len(t.get('items', [])) for t in turns),
                              'independent': True, 'native_lock': True,
                              'materialized_native_fork': bool(fork_import)}))
        db.close()


def verify_trash(ref):
    """Recycle-bin round trip against the installed Codex: hidden after delete, resumable after restore."""
    import shutil
    import trash
    with contextlib.closing(ci.readonly(ah.DATA / 'history.db')) as history:
        original = dict(ah.resolve(history, ref))
    homes = dict(ah.codex_homes())
    scratch = ah.DATA / 'verification'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='verify-trash-', dir=scratch) as temp:
        home = Path(temp) / 'home'
        home.mkdir()
        (home / 'config.toml').write_text('model="gpt-6.1-sol"\n[features]\nmemories=false\n', encoding='utf-8')
        with CodexRPC(home):
            pass
        # Native listing scans the dated layout, as for real sessions.
        path = home / 'sessions' / '2026' / '01' / '01' / Path(original['path']).name
        path.parent.mkdir(parents=True)
        ci.snapshot(ci.ancestry(homes[original['source']], original['path']), path, original['id'],
                    original, 'openai', 'gpt-6.1-sol')
        origin = ci.source_row(homes[original['source']], original['id'])
        ci.register_native(home, original['id'], path, dict(original, path=str(path)), origin, {'model': 'gpt-6.1-sol'})
        sid = original['id']
        with CodexRPC(home) as rpc:
            listed = rpc.call('thread/list', {'limit': 100})['data']
            assert any(t['id'] == sid for t in listed), 'Fixture session absent from native list'
            rpc.call('thread/resume', {'threadId': sid, 'excludeTurns': True})
            before = rpc.call('thread/turns/list', {'threadId': sid, 'limit': 100, 'itemsView': 'full'})['data']
        held = Path(temp) / 'held.jsonl'
        shutil.move(str(path), str(held))
        native = trash.native_remove(home, sid)
        assert native.get('rows', {}).get('threads'), 'Native thread row was not captured'
        with CodexRPC(home) as rpc:
            listed = rpc.call('thread/list', {'limit': 100})['data']
            assert not any(t['id'] == sid for t in listed), 'Deleted session still listed'
        shutil.move(str(held), str(path))
        trash.native_restore(home, native)
        with CodexRPC(home) as rpc:
            listed = rpc.call('thread/list', {'limit': 100})['data']
            assert any(t['id'] == sid for t in listed), 'Restored session absent from native list'
            rpc.call('thread/resume', {'threadId': sid, 'excludeTurns': True})
            after = rpc.call('thread/turns/list', {'threadId': sid, 'limit': 100, 'itemsView': 'full'})['data']
        assert len(after) == len(before) and after, 'Restored history differs'
        print(json.dumps({'trash': True, 'hidden_after_delete': True, 'restored_turns': len(after),
                          'tables': sorted(native['rows'])}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-session', required=True)
    parser.add_argument('--trash', action='store_true', help='verify recycle-bin delete/restore instead')
    args = parser.parse_args()
    (verify_trash if args.trash else verify)(args.source_session)
