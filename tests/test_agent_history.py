import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import agent_history as ah


def codex_rollout(sid, texts, cwd='C:/项目'):
    records = [{'type': 'session_meta', 'timestamp': '2026-09-01T00:00:00Z',
                'payload': {'id': sid, 'cwd': cwd, 'source': 'cli'}}]
    for role, text in texts:
        records.append({'type': 'response_item', 'timestamp': '2026-09-01T00:00:01Z',
                        'payload': {'type': 'message', 'role': role, 'content': [{'type': 'input_text', 'text': text}]}})
    return ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records)


class AgentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.root = root
        (root / 'shared').mkdir()
        (root / 'shared' / 'connections.json').write_text(json.dumps({'connections': {
            'a': {'home': '../a'}, 'b': {'home': '../b'}}}))
        for name in ('a', 'b'):
            (root / name / 'sessions').mkdir(parents=True)
        (root / 'claude' / 'projects').mkdir(parents=True)
        self.patches = [patch.object(ah, 'ROOT', root / 'shared'), patch.object(ah, 'DATA', root / 'data'),
                        patch.object(ah, 'CLAUDE_HOMES', [root / 'claude'])]
        for p in self.patches:
            p.start()
        self.db = ah.connect()

    def tearDown(self):
        self.db.close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_incremental_partial_line_and_search(self):
        path = self.root / 'a' / 'sessions' / 'r.jsonl'
        path.write_text(codex_rollout('s1', [('user', '<environment_context>x'), ('user', '检查部署任务')]) +
                        '{"type": "response_item", "payl', encoding='utf-8')
        before = path.read_bytes()
        ah.collect(self.db)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([r['text'] for r in ah.messages(self.db, 'codex:s1')], ['检查部署任务'])
        with path.open('a', encoding='utf-8') as fh:   # finish the partial line, add one more
            fh.write('oad": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "部署完成"}]}}\n')
        ah.collect(self.db)
        self.assertEqual(len(ah.messages(self.db, 'codex:s1')), 2)
        self.assertEqual(ah.collect(self.db), 0)
        self.assertEqual(ah.search(self.db, '部署完成')[0]['sid'], 's1')
        self.assertEqual(len(ah.search(self.db, '部署')), 2)   # short term uses LIKE

    def test_duplicate_session_across_homes_indexed_once(self):
        short = codex_rollout('dup', [('user', '同一个会话')])
        longer = codex_rollout('dup', [('user', '同一个会话'), ('assistant', '后续回复')])
        (self.root / 'a' / 'sessions' / 'r.jsonl').write_text(short, encoding='utf-8')
        (self.root / 'b' / 'sessions' / 'r.jsonl').write_text(longer, encoding='utf-8')
        ah.collect(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT source FROM sessions").fetchone()[0], 'b')
        self.assertEqual(len(ah.messages(self.db, 'codex:dup')), 2)
        self.assertEqual(ah.collect(self.db), 0)
        # Catalog de-duplication must not hide the shorter copy in connection a.
        self.assertEqual(ah.codex_sessions(self.db, 'a')[0]['source'], 'a')
        selected = ah.resolve_codex(self.db, 'a', 'du')
        self.assertEqual(selected['path'], str(self.root / 'a' / 'sessions' / 'r.jsonl'))
        self.assertEqual(ah.resolve_codex(self.db, 'b', 'latest')['source'], 'b')
        self.assertEqual(len(ah.codex_sessions(self.db)), 2)

    def test_connection_picker_filters_internal_and_imported_sessions(self):
        for sid, source, marker in [('main', 'cli', None), ('guard', {'guardian': {}}, None),
                                    ('agent', {'subagent': {}}, None), ('copy', 'cli', {'source': 'codex:main'})]:
            body = [json.loads(line) for line in codex_rollout(sid, [('user', 'question')]).splitlines()]
            body[0]['payload']['source'] = source
            if marker:
                body[0]['payload']['agent_history_import'] = marker
            (self.root / 'a' / 'sessions' / (sid + '.jsonl')).write_text(
                ''.join(json.dumps(r) + '\n' for r in body), encoding='utf-8')
        ah.collect(self.db)
        # An imported copy newer than the original must not win cross-account latest.
        copy_path = self.root / 'a' / 'sessions' / 'copy.jsonl'
        os.utime(copy_path, (2000000000, 2000000000))
        ah.collect(self.db)
        self.assertEqual([r['id'] for r in ah.codex_sessions(self.db, 'a')], ['main'])
        self.assertEqual({r['id'] for r in ah.codex_sessions(self.db, 'a', include_imports=True)}, {'main', 'copy'})
        self.assertEqual(ah.resolve_codex(self.db, 'a', 'copy')['kind'], 'import')
        self.assertEqual(ah.resolve_codex(self.db, 'a', 'latest')['id'], 'copy')
        self.assertEqual(ah.resolve_codex(self.db, 'a', 'latest', include_imports=False)['id'], 'main')
        with self.assertRaises(ValueError):
            ah.resolve_codex(self.db, 'a', 'copy', include_imports=False)
        with self.assertRaises(ValueError):
            ah.codex_sessions(self.db, 'unknown')
        self.assertEqual(ah.codex_sessions(self.db, 'a', query='MAIN', limit=1)[0]['id'], 'main')

    def test_import_claude_marks_copy_and_links_messages(self):
        (self.root / 'a' / 'sessions' / 'r.jsonl').write_text(
            codex_rollout('src', [('user', '第一问'), ('assistant', '第一答'), ('user', '第二问')], cwd=self.tmp.name),
            encoding='utf-8')
        ah.collect(self.db)
        result = ah.import_claude(self.db, 'src')
        lines = [json.loads(l) for l in Path(result['path']).read_text(encoding='utf-8').splitlines()]
        chat = [l for l in lines if l['type'] in ('user', 'assistant')]
        self.assertEqual([l['type'] for l in chat], ['user', 'assistant', 'user', 'assistant'])
        self.assertIsNone(chat[0]['parentUuid'])
        for prev, cur in zip(chat, chat[1:]):
            self.assertEqual(cur['parentUuid'], prev['uuid'])
        self.assertIn(ah.IMPORT_MARK, chat[0]['message']['content'])
        ah.collect(self.db)
        row = self.db.execute("SELECT kind, imported_from FROM sessions WHERE tool='claude'").fetchone()
        self.assertEqual(tuple(row), ('import', 'codex:src'))
        self.assertEqual(len(ah.list_sessions(self.db, tool='claude')), 0)   # hidden unless --all

    def test_sync_claude_refreshes_untouched_and_forks_continued(self):
        path = self.root / 'a' / 'sessions' / 'r.jsonl'
        recent = ah.datetime.datetime.now(ah.datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        body = codex_rollout('s', [('user', '问题一'), ('user', '问题一补充')], cwd=self.tmp.name).replace('2026-09-01T00:00:01Z', recent)
        path.write_text(body, encoding='utf-8')
        ah.collect(self.db)
        self.assertEqual(ah.sync_claude(self.db, min_turns=3)['new'], [])   # below the turn threshold
        first = ah.sync_claude(self.db)
        self.assertEqual(len(first['new']), 1)
        self.assertEqual(ah.sync_claude(self.db)['unchanged'], 1)
        target = Path(self.db.execute('SELECT dst_path FROM imports').fetchone()[0])

        def grow(text):
            with path.open('a', encoding='utf-8') as fh:
                fh.write(json.dumps({'type': 'response_item', 'timestamp': recent[:-1] + '9Z', 'payload': {
                    'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}},
                    ensure_ascii=False) + '\n')
            self.db.execute("UPDATE sessions SET updated=updated || 'x'")   # force a newer timestamp
        grow('问题二')
        self.assertEqual(len(ah.sync_claude(self.db)['refreshed']), 1)
        self.assertIn('问题二', target.read_text(encoding='utf-8'))
        with target.open('a', encoding='utf-8') as fh:                     # user continues the copy in Claude
            fh.write(json.dumps({'type': 'user', 'promptId': 'p', 'message': {'role': 'user', 'content': '继续'}}) + '\n')
        grow('问题三')
        self.assertEqual(len(ah.sync_claude(self.db)['forked']), 1)
        self.assertIn('继续', target.read_text(encoding='utf-8'))         # their branch is untouched
        self.assertEqual(self.db.execute('SELECT count(*) FROM imports').fetchone()[0], 2)
        for row in self.db.execute('SELECT dst_path FROM imports'):
            Path(row[0]).unlink()
        grow('问题四')
        self.assertEqual(ah.sync_claude(self.db)['deleted'], 1)          # deleted copies stay deleted


if __name__ == '__main__':
    unittest.main()
