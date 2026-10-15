"""Real Codex list/resume/readback of a synthetic merge; zero model turns."""
import contextlib
import datetime
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_history as ah
import codex_import as ci
import conversation_merge as merge
import conversation_store as store
from native_codex import CodexRPC


def verify():
    with tempfile.TemporaryDirectory(prefix='verify-merge-') as temporary:
        root = Path(temporary)
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
                patch.object(ah, 'CLAUDE_HOMES', []), contextlib.closing(ah.connect()) as db:
            timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            from uuid import uuid4
            sid = str(uuid4())
            path = homes['a'] / 'sessions' / (sid + '.jsonl')
            path.parent.mkdir()
            records = [dict(type='session_meta', timestamp=timestamp, payload=dict(
                id=sid, cwd=str(root), source='cli', history_mode='legacy', timestamp=timestamp,
                model_provider='openai', originator='codex_cli_rs', cli_version='0.0.0'))]
            store.append_events(records, [dict(role='user', text='merge-shared-original', ts=timestamp)],
                                'codex', sid, str(root))
            path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
            ah.collect(db)
            original = dict(ah.resolve(db, sid))
            ci.register_native(homes['a'], sid, path, original, dict(history_mode='legacy'), ci.config(homes['a']))
            source = store.enroll(db, original)
            copied = ci.import_session(db, original, homes['a'], 'b', homes['b'], fresh=True)
            ah.collect(db)
            other = dict(ah.resolve(db, copied['session']))
            store.register_target(db, source, other, 'b', str(root))
            saved = {}
            for row, marker in ((original, 'left-branch-message'), (other, 'right-branch-message')):
                added = store.append_events([], [dict(role='user', text=marker, ts=timestamp),
                    dict(role='assistant', text=marker + '-answer', ts=timestamp)], 'codex', row['id'], str(root))
                with Path(row['path']).open('a', encoding='utf-8') as stream:
                    stream.write(''.join(json.dumps(record) + '\n' for record in added))
                    stream.write(json.dumps(dict(type='compacted', timestamp=timestamp,
                        payload=dict(message='synthetic-old-compaction'))) + '\n')
                    stream.write(json.dumps(dict(type='response_item', timestamp=timestamp,
                        payload=dict(type='function_call_output', call_id='history-only',
                                     output='full-tool-' + 'z' * 9000))) + '\n')
                saved[row['path']] = Path(row['path']).read_bytes()
            store.sync_all(db)
            value = merge.plan(db, source['conversation'])
            result = merge.apply(db, value)
            assert len(store.members(db, source['conversation'])) == 1
            assert len(store.tips(db, source['conversation'])) == 1
            assert all(Path(name).read_bytes() == body for name, body in saved.items())
            home = homes[result['row']['source']]
            with CodexRPC(home) as rpc:
                assert any(row['id'] == result['row']['id'] for row in rpc.call('thread/list', dict(limit=100))['data'])
                rpc.call('thread/resume', dict(threadId=result['row']['id']))
                turns = rpc.call('thread/turns/list', dict(threadId=result['row']['id'], limit=100, itemsView='full'))
                text = json.dumps(turns['data'])
                assert all(marker in text for marker in ('merge-shared-original', 'left-branch-message',
                    'right-branch-message', 'synthetic-old-compaction', 'z' * 9000))
            print(json.dumps(dict(merged=True, listed=True, resumed=True, restarted=True,
                                  branch_text=True, originals_preserved=True, model_requests=0)), flush=True)


if __name__ == '__main__':
    verify()
