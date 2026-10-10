"""Verify UI preparation against the installed Codex using synthetic history only.

Run from the repository: python tests/verify_ui_resume.py
Initializes temporary connections and lists/resumes/reads threads, without model turns.
"""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_history as ah
import codex_import as ci
import history_ui as ui
from native_codex import CodexRPC


def verify():
    with tempfile.TemporaryDirectory(prefix='verify-ui-resume-') as temporary:
        root = Path(temporary)
        shared, source, target = (root / name for name in ('shared', 'source', 'target'))
        shared.mkdir()
        model = 'gpt-6.1-sol'
        for home in (source, target):
            home.mkdir()
            config = f'model="{model}"\n[features]\nmemories=false\n'
            (home / 'config.toml').write_text(config, encoding='utf-8')
            with CodexRPC(home) as rpc:
                if home == source:
                    started = rpc.call('thread/start', dict(cwd=str(root), model=model))['thread']
        (shared / 'connections.json').write_text(json.dumps(dict(connections=dict(
            a=dict(home=str(source)), b=dict(home=str(target))))), encoding='utf-8')
        sid = started['id']
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        path = Path(started['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(id=sid, cwd=str(root), source='cli', timestamp=stamp, history_mode='legacy',
                    model_provider='openai', originator='codex_cli_rs', cli_version='0.0.0')
        records = [dict(type='session_meta', timestamp=stamp, payload=meta)]
        for role, text in (('user', 'Synthetic selected source question'), ('assistant', 'Synthetic source answer')):
            records.append(dict(type='response_item', timestamp=stamp, payload=dict(type='message', role=role,
                content=[dict(type='input_text' if role == 'user' else 'output_text', text=text)])))
            records.append(dict(type='event_msg', timestamp=stamp, payload=dict(
                type='user_message' if role == 'user' else 'agent_message', message=text)))
        path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
        baseline = path.read_bytes()
        with patch.object(ah, 'ROOT', shared), patch.object(ah, 'DATA', root / 'data'), \
                patch.object(ah, 'CLAUDE_HOMES', []), contextlib.closing(ah.connect()) as db:
            ah.collect(db)
            selected = ui.list_rows(db)[0]
            ci.register_native(source, sid, path, selected, dict(history_mode='legacy'), dict(model=model))
            plan = ui.launch_plan(db, selected, 'b', str(root))
            assert plan['kind'] == 'codex-import'
            preview = ui.file_messages(selected)
            prepared = ci.import_session(db, selected, source, 'b', target, prefer_native=False)
            assert prepared['session'] != sid
            copied = dict(selected, id=prepared['session'], source='b', path=prepared['path'], kind='import')
            assert ui.file_messages(copied) == preview
            for attempt in range(2):
                with CodexRPC(target) as rpc:
                    listed = rpc.call('thread/list', dict(limit=100))['data']
                    assert any(thread['id'] == prepared['session'] for thread in listed)
                    restored = rpc.call('thread/resume', dict(threadId=prepared['session']))
                    assert restored['thread']['id'] == prepared['session']
                    turns = restored['thread'].get('turns') or rpc.call('thread/turns/list', dict(
                        threadId=prepared['session'], limit=100, itemsView='full'))['data']
                    assert 'Synthetic selected source question' in json.dumps(turns)
                    assert turns and any(turn.get('items') for turn in turns)
            assert path.read_bytes() == baseline
            print(json.dumps(dict(synthetic=True, listed=True, resumed=True, restart_resumed=True,
                                  preview_matches=True, source_unchanged=True, model_requests=0)))


if __name__ == '__main__':
    verify()
