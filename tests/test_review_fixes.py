"""Fault injection and growth regressions from the October project review."""
import gzip
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import agent_history as ah
import conversation_store as store
import history_ui as ui
import launch_backend as backend
import launch_jobs as jobs
import launch_runner
import maintenance
import setup_backend as setup
import trash
from tests.test_trash import Fixture, age, rollout
from tests import test_conversation_store as helpers


class ReviewFixTests(Fixture):
    add = helpers.ConversationTests.add

    def row(self):
        return dict(ah.resolve(self.db, 's1'))

    def delete(self):
        return trash.delete(self.db, [dict(tool='codex', source='a', path=str(self.path))])

    def test_cache_failure_after_native_delete_keeps_recoverable_file_and_rows(self):
        original = self.path.read_bytes()
        with patch.object(trash, 'remove_cache_lines', side_effect=OSError('cache failure')):
            with self.assertRaises(OSError):
                self.delete()
        entry = trash.entries()[0]
        self.assertTrue((trash.trash_root() / entry['id'] / entry['items'][0]['stored']).exists())
        self.assertTrue(entry['items'][0]['native']['rows']['threads'])
        self.assertEqual(trash.restore(self.db, entry['id'])['errors'], [])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.native_ids(self.a), ['s1'])

    def test_failed_recovery_journal_prevents_native_delete(self):
        write = trash.write_manifest
        def fail_before_native_delete(folder, manifest):
            if manifest['items'][0]['native']:
                raise OSError('journal failure')
            return write(folder, manifest)
        with patch.object(trash, 'write_manifest', side_effect=fail_before_native_delete):
            with self.assertRaises(OSError):
                self.delete()
        self.assertTrue(self.path.exists())
        self.assertEqual(self.native_ids(self.a), ['s1'])
        self.assertEqual(trash.entries(), [])

    def test_restore_retries_sql_and_cache_steps_without_overwriting_or_duplicates(self):
        entry = self.delete()
        with patch.object(trash, 'native_restore', side_effect=sqlite3.OperationalError('database busy')):
            self.assertEqual(trash.restore(self.db, entry['id'])['restored'], 0)
        self.assertTrue(self.path.exists())
        restore = trash.restore_cache_lines
        def fail_second_cache(path, lines):
            if Path(path).name == 'history.jsonl':
                raise OSError('cache busy')
            return restore(path, lines)
        with patch.object(trash, 'restore_cache_lines', side_effect=fail_second_cache):
            self.assertEqual(trash.restore(self.db, entry['id'])['restored'], 0)
        self.assertEqual(trash.restore(self.db, entry['id'])['errors'], [])
        self.assertEqual((self.a / 'session_index.jsonl').read_text().count('"s1"'), 1)
        self.assertEqual(self.native_ids(self.a), ['s1'])

    def test_retry_does_not_accept_a_modified_restored_file(self):
        entry = self.delete()
        with patch.object(trash, 'native_restore', side_effect=sqlite3.OperationalError('busy')):
            trash.restore(self.db, entry['id'])
        self.path.write_text('new continuation', encoding='utf-8')
        self.assertEqual(trash.restore(self.db, entry['id'])['restored'], 0)
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'new continuation')
        self.assertEqual(len(trash.entries()), 1)

    def test_equal_and_larger_rewrites_replace_index_and_search_cache(self):
        original = self.path.read_text(encoding='utf-8')
        for text in ('替换问题', '另一段更长的问题'):
            self.path.write_text(original.replace('原始问题', text), encoding='utf-8')
            stat = self.path.stat()
            os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            ah.collect(self.db)
            self.assertEqual([row['text'] for row in ah.messages(self.db, 'codex:s1')], [text])
            self.assertEqual([r['id'] for r in ui.list_rows(self.db, query=text)], ['s1'])
            self.assertEqual(ui.list_rows(self.db, query='原始问题'), [])

    def test_deselected_versions_are_not_read_or_counted(self):
        first = store.enroll(self.db, self.row())
        duplicate = self.b / 'sessions' / 's2.jsonl'
        duplicate.write_text(self.path.read_text(encoding='utf-8').replace('s1', 's2'), encoding='utf-8')
        ah.collect(self.db)
        other = store.enroll(self.db, dict(ah.resolve(self.db, 's2')), conversation=first['conversation'])
        setup.atomic_json(ah.DATA / 'sources.json', dict(version=1, codex=['a'], claude=[]))
        with patch.object(store, 'read_native', wraps=store.read_native) as read:
            self.assertEqual(store.sync_all(self.db, force=True)['failed'], [])
        self.assertEqual([call.args[0]['source'] for call in read.call_args_list], ['a'])
        self.assertEqual([m['id'] for m in store.members(self.db, first['conversation'])], [first['id']])
        self.assertEqual(store.lookup(self.db, dict(id='s2', tool='codex', source='b', path=str(duplicate)))['head'], other['head'])

    def test_old_jobs_are_reconciled_and_updates_are_serialized(self):
        prepared = backend.prepare_launch(self.db, self.row(), 'a', self.cwd)
        old = prepared['job']
        jobs.update(old, state='running', pid=123456)
        for _ in range(105):
            jobs.create(self.row(), prepared, 'a')
        with patch.object(jobs, 'process_alive', return_value=False):
            jobs.reconcile()
        self.assertEqual(jobs.read(old)['state'], 'interrupted')
        import handoff_lifecycle
        handoff_lifecycle.retry_pending(self.db)
        jobs.mark_collected(dict(failed=[]))
        self.assertEqual(jobs.read(old)['sync_state'], 'collected')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: jobs.update(old, **{f'field_{i}': i}), range(12)))
        self.assertTrue(all(jobs.read(old)[f'field_{i}'] == i for i in range(12)))
        def poll_or_update(index):
            for n in range(20):
                if index % 2:
                    jobs.read(old)
                else:
                    jobs.update(old, **{f'poll_{index}': n})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(poll_or_update, range(4)))
        jobs.update(old, expected_state='running', state='failed')
        self.assertEqual(jobs.read(old)['state'], 'interrupted')

    def test_nested_script_native_exit_code_survives_command_wrapper(self):
        if os.name != 'nt':
            self.skipTest('PowerShell wrapper')
        script = self.root / 'custom agent.ps1'
        script.write_text('cmd.exe /c exit 17\n', encoding='utf-8')
        result = subprocess.run(launch_runner.command(str(script), []), capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 17, result.stderr)

    def test_unchanged_sync_and_search_do_not_parse_full_histories(self):
        store.enroll(self.db, self.row())
        with patch.object(store, 'read_native', side_effect=AssertionError('unchanged native read')), \
                patch.object(backend, 'file_messages', side_effect=AssertionError('uncached preview read')):
            self.assertEqual(store.sync_all(self.db)['updated'], 0)
            self.assertEqual(len(ui.list_rows(self.db, query='原始问题', sort_by='turns')), 1)

    def test_targeted_delete_reindexes_duplicates_without_reading_unrelated_messages(self):
        duplicate = self.b / 'sessions' / 's1-copy.jsonl'
        duplicate.write_bytes(self.path.read_bytes())
        other = self.a / 'sessions' / 'other.jsonl'
        other.write_text(rollout('other', 'unrelated original', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        other.write_text(rollout('other', 'unrelated latest', self.cwd), encoding='utf-8')
        parse = backend.file_messages
        def affected_only(row):
            self.assertNotEqual(row['id'], 'other')
            return parse(row)
        with trash.locked_db(reindex=False) as db, patch.object(backend, 'file_messages', side_effect=affected_only):
            trash.delete_many(db, [('one', [dict(tool='codex', source='a', path=str(self.path))])])
            ah.collect(db, only_paths={str(self.path), str(duplicate)})
            self.assertEqual(ah.resolve(db, 's1')['source'], 'b')
            self.assertEqual([r['text'] for r in ah.messages(db, 'codex:other')], ['unrelated original'])
        ah.collect(self.db)
        self.assertEqual([r['text'] for r in ah.messages(self.db, 'codex:other')], ['unrelated latest'])

    def test_unindexed_new_fork_still_blocks_fast_deletion(self):
        child = self.a / 'sessions' / 'new-fork.jsonl'
        child.write_text(rollout('new-fork', 'child', self.cwd, base='s1'), encoding='utf-8')
        with trash.locked_db(reindex=False) as db:
            with self.assertRaisesRegex(ValueError, '分支'):
                trash.delete(db, [dict(tool='codex', source='a', path=str(self.path))])
        self.assertTrue(self.path.exists())

    def test_delta_snapshots_checkpoint_roundtrip_and_bound_growth(self):
        row = self.row()
        store.enroll(self.db, row)
        for index in range(80):
            self.add(row, f'growth {index} ' + 'content ' * 100)
        revisions = self.db.execute('SELECT * FROM conversation_revisions ORDER BY created').fetchall()
        total = sum(Path(item['snapshot']).stat().st_size for item in revisions)
        full_json = 0
        for item in revisions:
            with gzip.open(item['snapshot'], 'rt', encoding='utf-8') as stream:
                encoded = json.load(stream)
            self.assertLessEqual(encoded['depth'], 31)
            value = store.revision(self.db, item['id'])
            self.assertEqual(store.digest(value['records']), item['native_hash'])
            full_json += len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        self.assertLess(total, full_json // 10)
        latest = store.revision(self.db, store.lookup(self.db, row)['head'])
        self.assertEqual(sum(event['role'] == 'user' for event in latest['events']), 81)

    def test_legacy_compaction_preview_and_retry_preserve_all_revisions(self):
        row = self.row()
        store.enroll(self.db, row)
        self.add(row, 'legacy conversion')
        expected = {}
        for item in self.db.execute('SELECT * FROM conversation_revisions').fetchall():
            value = dict(store.revision(self.db, item['id']))
            value.pop('depth', None)
            expected[item['id']] = value
            old = Path(item['snapshot']).with_suffix('')
            old.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            Path(item['snapshot']).unlink()
            self.db.execute('UPDATE conversation_revisions SET snapshot=? WHERE id=?', (str(old), item['id']))
        self.db.commit()
        store.read_snapshot.cache_clear()
        write = store.write_snapshot
        calls = 0
        def interrupted(path, value, previous=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('interrupted compaction')
            return write(path, value, previous)
        with patch.object(store, 'write_snapshot', side_effect=interrupted):
            with self.assertRaises(OSError):
                maintenance.compact(self.db, apply=True)
        for rid, original in expected.items():
            actual = dict(store.revision(self.db, rid))
            actual.pop('depth', None)
            self.assertEqual(actual, original)
        projection = ah.DATA / 'handoffs' / 'projections' / 'old.jsonl'
        projection.parent.mkdir()
        projection.write_text('reproducible', encoding='utf-8')
        age(projection, seconds=8 * 86400)
        self.assertEqual(maintenance.compact(self.db)['converted'], 0)
        self.assertTrue(projection.exists())
        result = maintenance.compact(self.db, apply=True)
        self.assertEqual(result['converted'], len(expected) - 1)
        self.assertFalse(projection.exists())
        for rid, original in expected.items():
            actual = dict(store.revision(self.db, rid))
            actual.pop('depth', None)
            self.assertEqual(actual, original)
        self.assertEqual(maintenance.compact(self.db, apply=True)['converted'], 0)
