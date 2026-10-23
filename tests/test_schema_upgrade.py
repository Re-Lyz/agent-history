"""Upgrade old/intermediate databases without touching native or logical history."""
import contextlib
import json
import threading
from unittest.mock import patch

import agent_history as ah
import conversation_store as store
import history_ui as ui
from history_app.storage import read_model
from tests.test_trash import Fixture


class SchemaUpgradeTests(Fixture):
    def intermediate_v5(self):
        # The first v5 prototype indexed parts through file_views_fts and
        # lacked both part coverage and the logical revision clock.
        for name in read_model.REQUIRED_TRIGGERS:
            self.db.execute('DROP TRIGGER IF EXISTS ' + name)
        self.db.executescript('''
            DROP TABLE file_view_parts_fts;
            DROP TABLE file_views_fts;
            DROP TABLE file_view_part_coverage;
            DROP TABLE logical_clock;
            CREATE VIRTUAL TABLE file_views_fts USING fts5(search_text,
                content='file_view_parts',content_rowid='rowid',tokenize='trigram',detail='none');
            INSERT INTO file_views_fts(file_views_fts) VALUES('rebuild');
            CREATE TRIGGER file_view_parts_ai AFTER INSERT ON file_view_parts BEGIN
                INSERT INTO file_views_fts(rowid,search_text) VALUES(new.rowid,new.search_text); END;
            CREATE TRIGGER file_view_parts_ad AFTER DELETE ON file_view_parts BEGIN
                INSERT INTO file_views_fts(file_views_fts,rowid,search_text)
                    VALUES('delete',old.rowid,old.search_text); END;
            PRAGMA user_version=5;
        ''')

    def logical_state(self, db):
        names = ('conversations', 'conversation_versions', 'conversation_revisions', 'conversation_runs',
                 'conversation_retirements', 'conversation_merges', 'temporary_handoffs',
                 'handoff_intents', 'handoff_publications', 'imports', 'codex_imports', 'conversation_titles')
        return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY rowid')] for name in names}

    def test_intermediate_v5_is_repaired_before_listing_and_collection(self):
        member = store.enroll(self.db, dict(ah.resolve(self.db, 's1')))
        before, native = self.logical_state(self.db), self.path.read_bytes()
        snapshot = store.revision(self.db, member['head'])
        self.intermediate_v5()
        self.assertFalse(read_model.ready(self.db))
        with contextlib.closing(ah.connect()) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], ah.SCHEMA_VERSION)
            self.assertTrue(read_model.ready(db))
            self.assertEqual(self.logical_state(db), before)
            self.assertEqual(store.revision(db, member['head']), snapshot)
            self.assertEqual([row['id'] for row in ui.list_rows(db, indexed=True, logical=True)], ['s1'])
            ah.require_collected(ah.collect(db, only_paths={self.path}))
            self.assertEqual([row['id'] for row in ui.list_rows(db, indexed=True, query='原始问题')], ['s1'])
        self.assertEqual(self.path.read_bytes(), native)

    def test_complete_v5_keeps_existing_search_fragments(self):
        old = [tuple(row) for row in self.db.execute('SELECT * FROM file_view_parts')]
        self.db.execute('PRAGMA user_version=5')
        with patch.object(ah, 'upgrade', wraps=ah.upgrade) as upgrade, contextlib.closing(ah.connect()) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], ah.SCHEMA_VERSION)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM file_view_parts')], old)
            self.assertEqual(upgrade.call_count, 1)

    def test_current_version_with_missing_objects_is_also_repaired(self):
        self.db.executescript('DROP TABLE logical_clock;')
        with contextlib.closing(ah.connect()) as db:
            self.assertTrue(read_model.ready(db))
            self.assertTrue(ui.list_rows(db, indexed=True))

    def test_simultaneous_openers_wait_for_one_complete_upgrade(self):
        self.intermediate_v5()
        started, release, second_started, second_finished = (threading.Event() for _ in range(4))
        actual = ah.upgrade
        errors, results = [], []
        def upgrade(db, version):
            started.set()
            if not release.wait(5):
                raise RuntimeError('Synthetic upgrade gate timed out')
            return actual(db, version)
        def open_database(second=False):
            if second:
                second_started.set()
            try:
                with contextlib.closing(ah.connect()) as db:
                    results.append(read_model.ready(db))
            except Exception as error:
                errors.append(error)
            finally:
                if second:
                    second_finished.set()
        threads = [threading.Thread(target=open_database), threading.Thread(target=open_database, args=(True,))]
        with patch.object(ah, 'upgrade', side_effect=upgrade) as upgrades:
            try:
                threads[0].start()
                self.assertTrue(started.wait(3))
                threads[1].start()
                self.assertTrue(second_started.wait(3))
                self.assertFalse(second_finished.wait(.1))
            finally:
                release.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(10)
            self.assertEqual(errors, [])
            self.assertEqual(results, [True, True])
            self.assertEqual(upgrades.call_count, 1)

    def test_newer_unknown_database_is_not_modified(self):
        before = self.logical_state(self.db)
        self.db.execute('PRAGMA user_version=999')
        with self.assertRaisesRegex(ValueError, '更新'):
            ah.connect()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 999)
        self.assertEqual(self.logical_state(self.db), before)
