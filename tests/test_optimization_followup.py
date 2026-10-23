"""Follow-up regressions use synthetic files and never start a model or real login."""
import contextlib
import json
from pathlib import Path
import threading
import time
from unittest.mock import patch

import agent_history as ah
import conversation_store as store
import history_ui as ui
import launch_backend as backend
from history_app import performance
from history_app.read_workers import ReadWorkers
from history_app.reader_data import ReadCancelled
from tests.test_trash import Fixture, rollout


class FollowupTests(Fixture):
    def append_message(self, text):
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(type='response_item', payload=dict(type='message', role='assistant',
                content=[dict(type='output_text', text=text)])), ensure_ascii=False) + '\n')

    def test_append_updates_search_fragments_without_reparsing_old_messages(self):
        original = self.db.execute('SELECT rowid,search_text FROM file_view_parts WHERE path=?', (str(self.path),)).fetchone()
        self.append_message('追加中文 unique-tail')
        with patch.object(backend, 'file_messages', side_effect=AssertionError('Full native reparse')):
            result = ah.collect(self.db, only_paths={self.path})
        self.assertFalse(result.failed)
        preserved = self.db.execute('SELECT search_text FROM file_view_parts WHERE rowid=?', (original['rowid'],)).fetchone()
        self.assertEqual(preserved[0], original['search_text'])
        for query in ('unique-tail', '原始问题\n追加中文', '追加中', '中', 'UNIQUE-TAIL'):
            rows = ui.list_rows(self.db, indexed=True, query=query)
            self.assertEqual([row['path'] for row in rows], [str(self.path)], query)
        row = self.db.execute('SELECT metadata FROM file_views WHERE path=?', (str(self.path),)).fetchone()
        self.assertEqual(json.loads(row[0])['message_count'], 2)
        self.db.execute("INSERT INTO file_view_parts_fts(file_view_parts_fts,rank) VALUES('integrity-check',1)")

    def test_rewrite_rebuilds_search_and_keeps_other_histories_available(self):
        self.append_message('obsolete suffix')
        ah.collect(self.db)
        self.path.write_text(rollout('s1', 'replacement prefix', self.cwd), encoding='utf-8')
        ah.require_collected(ah.collect(self.db))
        self.assertFalse(ui.list_rows(self.db, indexed=True, query='obsolete suffix'))
        self.assertTrue(ui.list_rows(self.db, indexed=True, query='replacement prefix'))
        self.db.execute("INSERT INTO file_view_parts_fts(file_view_parts_fts,rank) VALUES('integrity-check',1)")

    def test_one_unreadable_file_preserves_old_index_without_blocking_other_updates(self):
        other = self.a / 'sessions' / 'other.jsonl'
        other.write_text(rollout('other', 'healthy history', self.cwd), encoding='utf-8')
        self.path.write_text('{broken header}\n', encoding='utf-8')
        result = ah.collect(self.db)
        self.assertEqual([item['path'] for item in result.failed], [str(self.path)])
        self.assertEqual(ah.messages(self.db, 'codex:s1')[0]['text'], '原始问题')
        self.assertTrue(ui.list_rows(self.db, indexed=True, query='healthy history'))
        with self.assertRaisesRegex(ValueError, '索引'):
            ah.require_collected(result)
        self.path.write_text(rollout('s1', 'fixed history', self.cwd), encoding='utf-8')
        with patch.object(Path, 'rglob', side_effect=AssertionError('Unrelated discovery')):
            ah.require_collected(ah.collect(self.db, only_paths={self.path}))
        self.assertFalse(self.db.execute('SELECT * FROM index_failures').fetchall())
        self.assertTrue(ui.list_rows(self.db, indexed=True, query='fixed history'))

    def test_partial_record_becomes_searchable_only_after_completion(self):
        record = json.dumps(dict(type='response_item', payload=dict(type='message', role='assistant',
                    content=[dict(type='output_text', text='completed suffix')])))
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(record[:20])
        ah.collect(self.db)
        self.assertFalse(ui.list_rows(self.db, indexed=True, query='completed suffix'))
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(record[20:] + '\n')
        ah.require_collected(ah.collect(self.db))
        self.assertTrue(ui.list_rows(self.db, indexed=True, query='completed suffix'))

    def test_pre_upgrade_searches_fall_back_safely_after_incremental_update(self):
        view = self.db.execute('SELECT rowid,search_text FROM file_views WHERE path=?', (str(self.path),)).fetchone()
        with self.db:
            self.db.execute('INSERT INTO file_views_fts(rowid,search_text) VALUES (?,?)', tuple(view))
            self.db.execute('INSERT INTO file_view_search_coverage VALUES (?)', (view['rowid'],))
        self.append_message('new compatibility marker')
        ah.require_collected(ah.collect(self.db))
        # This is the v4 candidate/fallback contract, used by an older UI that
        # was already running when a new collector migrated the shared DB.
        matches = self.db.execute('SELECT path FROM file_views WHERE '
            '(rowid IN (SELECT rowid FROM file_views_fts WHERE file_views_fts MATCH ?) OR '
            'rowid NOT IN (SELECT view_rowid FROM file_view_search_coverage)) AND instr(search_text,?)>0',
            ('"com"', 'new compatibility marker')).fetchall()
        self.assertEqual([row['path'] for row in matches], [str(self.path)])
        # A pre-upgrade collector can still populate its own full-document FTS.
        view = self.db.execute('SELECT rowid,search_text FROM file_views WHERE path=?', (str(self.path),)).fetchone()
        with self.db:
            self.db.execute('INSERT INTO file_views_fts(rowid,search_text) VALUES (?,?)', tuple(view))
            self.db.execute('INSERT INTO file_view_search_coverage VALUES (?)', (view['rowid'],))
        self.assertTrue(ui.list_rows(self.db, indexed=True, query='compatibility marker'))
        self.db.execute("INSERT INTO file_view_parts_fts(file_view_parts_fts,rank) VALUES('integrity-check',1)")

    def test_group_cache_reuses_decisions_but_invalidates_revisions_and_presence(self):
        row = dict(ah.resolve(self.db, 's1'))
        member = store.enroll(self.db, row)
        ui.list_rows(self.db, indexed=True, logical=True)
        with patch.object(store, 'group_rows', side_effect=AssertionError('Repeated branch comparison')):
            self.assertEqual(len(ui.list_rows(self.db, indexed=True, logical=True)), 1)
        self.append_message('new revision')
        ah.collect(self.db)
        store.observe(self.db, member)
        with patch.object(store, 'group_rows', wraps=store.group_rows) as grouped:
            self.assertEqual(len(ui.list_rows(self.db, indexed=True, logical=True)), 1)
            self.assertEqual(grouped.call_count, 1)
        self.path.unlink()
        self.assertEqual(len(ui.list_rows(self.db, indexed=True, logical=True)), 0)

    def test_listing_can_cancel_before_projection_and_retry_on_same_database(self):
        store.enroll(self.db, dict(ah.resolve(self.db, 's1')))
        with self.assertRaises(ReadCancelled):
            ui.list_rows(self.db, indexed=True, logical=True, cancel=lambda: True)
        self.assertTrue(ui.list_rows(self.db, indexed=True, logical=True))

    def test_read_pool_bounds_actual_concurrency_and_closes_without_waiting_on_work(self):
        pool, gate = ReadWorkers(), threading.Event()
        lock, running, maximum, finished = threading.Lock(), [0], [0], []
        def work():
            with lock:
                running[0] += 1
                maximum[0] = max(maximum[0], running[0])
            gate.wait(3)
            with lock:
                running[0] -= 1
                finished.append(True)
        try:
            for _ in range(12):
                pool.submit(work)
            deadline = time.monotonic() + 2
            while maximum[0] < 4 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(maximum[0], 4)
            gate.set()
            pool.queue.join()
            self.assertEqual(len(finished), 12)
        finally:
            gate.set()
            pool.close()

    def test_performance_export_accepts_only_fixed_numeric_fields(self):
        folder = ah.DATA / 'performance'
        folder.mkdir(exist_ok=True)
        (folder / 'synthetic.json').write_text(json.dumps({'reader': {
            'samples': 3, 'total_ms': 7, 'max_ms': 4, 'path': 'PRIVATE-PATH', 'updated': 'SECRET',
            'last_ms': 'PRIVATE-TEXT'}, 'PRIVATE-PHASE': {'samples': 1}}), encoding='utf-8')
        performance.record('reader', .002, messages=4, private_text='SECRET')
        performance.record('PRIVATE-PHASE', .2)
        performance.flush(ah.DATA, force=True)
        report = json.dumps(performance.summary(ah.DATA))
        for forbidden in ('PRIVATE', 'SECRET', 'private_text'):
            self.assertNotIn(forbidden, report)
        self.assertIn('messages', report)
