import contextlib
import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import agent_history as ah
import codex_import as ci


def native_db(home):
    db = sqlite3.connect(home / 'state_5.sqlite')
    db.execute('''CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT NOT NULL,
        created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,source TEXT NOT NULL,
        model_provider TEXT NOT NULL,cwd TEXT NOT NULL,title TEXT NOT NULL,
        sandbox_policy TEXT NOT NULL,approval_mode TEXT NOT NULL,
        first_user_message TEXT DEFAULT '',history_mode TEXT DEFAULT 'legacy',name TEXT,
        model TEXT,archived INTEGER DEFAULT 0,thread_source TEXT)''')
    db.commit()
    db.close()
    db = sqlite3.connect(home / 'thread_history_1.sqlite')
    db.execute('CREATE TABLE thread_items(thread_id TEXT,item_json TEXT)')
    db.execute('CREATE TABLE thread_history_projection_state(thread_id TEXT,next_rollout_byte_offset INTEGER)')
    db.commit()
    db.close()


class NativeImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.a, self.b = self.root / 'a', self.root / 'b'
        for home in (self.a, self.b):
            (home / 'sessions').mkdir(parents=True)
            (home / 'profile.toml').write_text('model="target-model"\nmodel_provider="target-provider"\n')
            native_db(home)
        shared = self.root / 'shared'
        shared.mkdir()
        (shared / 'connections.json').write_text(json.dumps({'connections': {
            'a': {'home': '../a'}, 'b': {'home': '../b'}}}))
        self.patches = [patch.object(ah, 'ROOT', shared), patch.object(ah, 'DATA', self.root / 'data'),
                        patch.object(ah, 'CLAUDE_HOMES', [])]
        for p in self.patches:
            p.start()
        self.db = ah.connect()
        self.path = self.a / 'sessions' / 'rollout-source.jsonl'
        self.body = [
            {'type': 'session_meta', 'timestamp': '2026-10-01T00:00:00Z', 'payload': {
                'id': 'source', 'session_id': 'source', 'cwd': str(self.a), 'source': 'cli',
                'timestamp': '2026-10-01T00:00:00Z', 'history_mode': 'paginated',
                'creator_user_id': 'original-owner', 'creator_account_id': 'original-account'}},
            {'type': 'turn_context', 'payload': {'model': 'source-model'}},
            {'type': 'response_item', 'timestamp': '2026-10-01T00:00:01Z', 'payload': {
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '中文问题'}]}},
            {'type': 'response_item', 'timestamp': '2026-10-01T00:00:02Z', 'payload': {
                'type': 'function_call_output', 'call_id': 'call1', 'output': 'full tool output'}},
            {'type': 'response_item', 'timestamp': '2026-10-01T00:00:03Z', 'payload': {
                'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '中文答案'}]}}]
        self.write_source()

    def tearDown(self):
        self.db.close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write_source(self):
        self.path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in self.body), encoding='utf-8')
        ah.collect(self.db)

    def import_one(self, **kwargs):
        return ci.import_session(self.db, ah.resolve(self.db, 'source'), self.a, 'b', self.b, **kwargs)

    def test_original_unchanged_complete_native_snapshot_and_no_loop(self):
        original = self.path.read_bytes()
        result = self.import_one()
        self.assertEqual(self.path.read_bytes(), original)
        records = [json.loads(l) for l in Path(result['path']).read_bytes().splitlines()]
        self.assertNotEqual(records[0]['payload']['id'], 'source')
        self.assertEqual(records[0]['payload']['model_provider'], 'target-provider')
        self.assertNotIn('creator_user_id', records[0]['payload'])
        self.assertEqual(records[1]['payload']['model'], 'target-model')
        self.assertEqual(records[3]['payload']['output'], 'full tool output')
        ah.collect(self.db)
        row = ah.resolve(self.db, result['session'])
        self.assertEqual((row['kind'], row['imported_from']), ('import', 'codex:source'))
        # Incremental collection must not reset imported copies to main sessions.
        with Path(result['path']).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'later-model'}}) + '\n')
        ah.collect(self.db)
        self.assertEqual(ah.resolve(self.db, result['session'])['kind'], 'import')
        self.db.execute("UPDATE sessions SET kind='main' WHERE key=?", (row['key'],))
        self.db.commit()
        ah.collect(self.db)
        self.assertEqual(ah.resolve(self.db, result['session'])['kind'], 'import')
        report = ci.sync(self.db, 'a', {'a': self.a, 'b': self.b}, days=365, min_turns=1, settle_seconds=0)
        self.assertEqual(report['new'], [])
        self.assertEqual(report['failed'], [])
        self.assertTrue(list((self.root / 'data' / 'native-backups' / 'b').rglob('state_5.sqlite')))

    def test_idempotence_refresh_cache_invalidation_and_continued_copy(self):
        first = self.import_one()
        self.assertEqual(self.import_one()['status'], 'unchanged')
        target = Path(first['path'])
        with contextlib.closing(sqlite3.connect(self.b / 'thread_history_1.sqlite')) as db:
            db.execute('INSERT INTO thread_items VALUES (?,?)', (first['session'], 'stale'))
            db.commit()
        self.body.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                          'content': [{'type': 'input_text', 'text': '追加问题'}]}})
        self.write_source()
        refreshed = self.import_one()
        self.assertEqual(refreshed['status'], 'refreshed')
        self.assertEqual(refreshed['session'], first['session'])
        with contextlib.closing(sqlite3.connect(self.b / 'thread_history_1.sqlite')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM thread_items').fetchone()[0], 0)
        with target.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'continued'}}) + '\n')
        preserved = target.read_bytes()
        self.body.append({'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'updated'}})
        self.write_source()
        forked = self.import_one()
        self.assertEqual(forked['status'], 'forked')
        self.assertNotEqual(forked['session'], first['session'])
        self.assertEqual(target.read_bytes(), preserved)

    def test_deleted_copy_stays_deleted_and_dry_run_does_not_publish(self):
        report = self.import_one(dry_run=True)
        self.assertEqual(report['status'], 'new')
        self.assertFalse(Path(report['path']).exists())
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], 0)
        first = self.import_one()
        Path(first['path']).unlink()
        self.assertEqual(self.import_one()['status'], 'deleted')

    def test_materializes_fork_ancestry_and_respects_boundary(self):
        parent = self.a / 'sessions' / 'rollout-parent.jsonl'
        parent_records = [dict(self.body[0], payload=dict(self.body[0]['payload'], id='parent', session_id='parent')),
                          self.body[2], self.body[3],
                          {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'excluded'}}]
        parent.write_text(''.join(json.dumps(r) + '\n' for r in parent_records), encoding='utf-8')
        self.body = [self.body[0], self.body[-1]]
        self.body[0]['payload']['history_base'] = {'thread_id': 'parent', 'end_ordinal_exclusive': 3}
        self.body[0]['payload']['forked_from_id'] = 'parent'
        self.write_source()
        result = self.import_one()
        records = [json.loads(l) for l in Path(result['path']).read_bytes().splitlines()]
        self.assertEqual(len(records), 4)
        self.assertNotIn('history_base', records[0]['payload'])
        self.assertNotIn('excluded', Path(result['path']).read_text(encoding='utf-8'))
        parent.unlink()  # Imported history no longer depends on the ancestor.
        self.assertIn('full tool output', Path(result['path']).read_text(encoding='utf-8'))

    def test_native_writer_lock_prevents_refresh(self):
        first = self.import_one()
        original = Path(first['path']).read_bytes()
        self.body.append({'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'change'}})
        self.write_source()
        with ci.writer_lock(self.b, first['session']) as locked:
            self.assertTrue(locked)
            self.assertEqual(self.import_one()['status'], 'busy')
        self.assertEqual(Path(first['path']).read_bytes(), original)

    def test_unchanged_source_forks_a_continued_target_and_respects_its_lock(self):
        first = self.import_one()
        with ci.writer_lock(self.b, first['session']) as locked:
            self.assertTrue(locked)
            self.assertEqual(self.import_one()['status'], 'busy')
        with Path(first['path']).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(self.body[-1]) + '\n')
        branch = Path(first['path']).read_bytes()
        forked = self.import_one(prefer_native=False)
        self.assertEqual(forked['status'], 'forked')
        self.assertNotEqual(forked['session'], first['session'])
        self.assertEqual(Path(first['path']).read_bytes(), branch)
        self.assertEqual(len(list(ci.records(forked['path']))), len(self.body))

    def test_bad_schema_leaves_no_session_file(self):
        with contextlib.closing(sqlite3.connect(self.b / 'state_5.sqlite')) as db:
            db.execute('ALTER TABLE threads ADD COLUMN future_required TEXT NOT NULL')
            db.commit()
        with self.assertRaisesRegex(ValueError, 'Unsupported required'):
            self.import_one()
        self.assertEqual(list((self.b / 'sessions').rglob('*.jsonl')), [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], 0)

    def test_explicit_import_copies_selected_source_even_when_native_id_exists(self):
        with contextlib.closing(sqlite3.connect(self.b / 'state_5.sqlite')) as db:
            db.execute('''INSERT INTO threads(id,rollout_path,created_at,updated_at,source,
                model_provider,cwd,title,sandbox_policy,approval_mode)
                VALUES ('source','old-copy',0,0,'cli','openai','cwd','old','{}','on-request')''')
            db.commit()
        self.assertEqual(self.import_one()['status'], 'native')
        result = self.import_one(prefer_native=False)
        self.assertEqual(result['status'], 'new')
        self.assertNotEqual(result['session'], 'source')
        self.assertIn('full tool output', Path(result['path']).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
