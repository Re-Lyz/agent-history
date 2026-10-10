"""Installed Codex list/resume/restart/trash verification with synthetic Claude data.

Usage: python tests/verify_claude_to_codex.py
Never starts a model turn or reads an authentication file or real conversation.
"""
import contextlib
import faulthandler
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_history as ah
import codex_import as ci
from native_codex import CodexRPC
import trash


def texts(turns):
    return json.dumps(turns, ensure_ascii=False)


def verify():
    faulthandler.dump_traceback_later(30, repeat=True)
    print('Preparing isolated native home...', flush=True)
    original = ah.ROOT, ah.DATA, ah.CLAUDE_HOMES
    try:
        with tempfile.TemporaryDirectory(prefix='verify-claude-codex-') as temp:
            root = Path(temp).resolve()
            home = root / 'codex'
            home.mkdir()
            (home / 'config.toml').write_text('model="gpt-6.1-sol"\n[features]\nmemories=false\n', encoding='utf-8')
            with CodexRPC(home):
                pass
            print('Native schemas initialized.', flush=True)
            ah.ROOT, ah.DATA, ah.CLAUDE_HOMES = root, root / 'data', [root / 'claude']
            (root / 'connections.json').write_text(json.dumps(dict(connections=dict(target=dict(home=str(home))))) )
            cwd = root / '中文 项目'
            cwd.mkdir()
            sid = str(uuid.uuid4())
            source = ah.claude_project_dir(str(cwd)) / (sid + '.jsonl')
            source.parent.mkdir(parents=True)
            records = []
            for index, (role, content) in enumerate([
                    ('user', '合成中文问题：记住 synthetic-marker-117。'),
                    ('assistant', [dict(type='text', text='合成回答：synthetic-marker-117。'),
                                   dict(type='tool_use', name='Bash', input={'command': 'must-not-execute'})]),
                    ('user', [dict(type='tool_result', content='合成工具输出'), dict(type='text', text='合成后续问题。')]),
                    ('assistant', '合成最终回答。')]):
                records.append(dict(type=role, sessionId=sid, uuid=str(uuid.uuid4()), cwd=str(cwd),
                                    timestamp=f'2026-10-01T00:00:0{index}Z', message=dict(content=content)))
            source.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
            baseline = source.read_bytes()
            with contextlib.closing(ah.connect()) as db:
                ah.collect(db)
                session = dict(ah.resolve(db, 'claude:' + sid))
                converted = ci.import_session(db, session, None, 'target', home)
                print('Synthetic Claude transcript converted.', flush=True)
                target_id = converted['session']
                assert target_id != sid
                assert ci.import_session(db, session, None, 'target', home)['status'] == 'unchanged'
                assert source.read_bytes() == baseline
                with CodexRPC(home) as rpc:
                    assert any(t['id'] == target_id for t in rpc.call('thread/list', {'limit': 100})['data'])
                    result = rpc.call('thread/resume', {'threadId': target_id})
                    assert result['model'] == 'gpt-6.1-sol'
                    turns = result['thread']['turns']
                    if not turns:
                        turns = rpc.call('thread/turns/list', {'threadId': target_id, 'limit': 100, 'itemsView': 'full'})['data']
                    assert 'synthetic-marker-117' in texts(turns), 'Converted text is missing from native resume'
                    assert '合成最终回答' in texts(turns), 'Last answer is missing'
                    assert '未重放' in texts(turns), 'Historical tool summary is missing'
                    assert all(item['type'] not in ('commandExecution', 'mcpToolCall', 'dynamicToolCall')
                               for turn in turns for item in turn.get('items', [])), 'Tool history became executable items'
                    with ci.writer_lock(home, target_id) as locked:
                        assert not locked, 'Resume did not hold native writer lock'
                print('Native list/resume/content/writer-lock passed.', flush=True)
                path = Path(converted['path'])
                old = time.time() - 3600
                os.utime(path, (old, old))
                ah.collect(db)
                deleted = trash.delete(db, [dict(tool='codex', source='target', path=str(path))], label='synthetic')
                assert ci.import_session(db, session, None, 'target', home)['status'] == 'deleted'
                with CodexRPC(home) as rpc:
                    assert not any(t['id'] == target_id for t in rpc.call('thread/list', {'limit': 100})['data'])
                restored = trash.restore(db, deleted['id'])
                assert restored['restored'] == 1 and not restored['errors']
                print('Native trash/delete/restore passed.', flush=True)
                source.unlink()
                with CodexRPC(home) as rpc:
                    result = rpc.call('thread/resume', {'threadId': target_id})
                    again = result['thread']['turns']
                    if not again:
                        again = rpc.call('thread/turns/list', {'threadId': target_id, 'limit': 100, 'itemsView': 'full'})['data']
                    assert 'synthetic-marker-117' in texts(again) and '合成最终回答' in texts(again)
                print(json.dumps(dict(version=subprocess.check_output([shutil.which('codex'), '--version'], text=True,
                                      env={**os.environ, 'CODEX_HOME': str(home)}).strip(),
                                      listed=True, resumed=True, independent=True, restarted=True,
                                      native_lock=True, trash_restored=True, tools_not_replayed=True,
                                      turns=len(again)), ensure_ascii=False))
    finally:
        faulthandler.cancel_dump_traceback_later()
        ah.ROOT, ah.DATA, ah.CLAUDE_HOMES = original


if __name__ == '__main__':
    verify()
