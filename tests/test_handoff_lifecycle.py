"""Temporary launch copies may retire only after a recoverable native publication."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import agent_history as ah
import codex_import as ci
import conversation_store as store
import handoff_lifecycle as lifecycle
import launch_backend as backend
import launch_jobs as jobs
import launch_job_runner as runner
import trash
from tests.test_trash import Fixture, age
from tests import test_conversation_store as conversation_tests


class LifecycleTests(Fixture):
    add = conversation_tests.ConversationTests.add
    text = conversation_tests.ConversationTests.text

    def setUp(self):
        super().setUp()
        self.verified = []
        def verify(db, child, final):
            row = json.loads(child['row_json'])
            self.assertTrue(Path(row['path']).is_file())
            self.assertEqual(store.revision(db, child['head'])['events'], final['events'])
            if row['tool'] == 'codex':
                self.assertIn(row['id'], self.native_ids(dict(ah.codex_homes())[row['source']]))
            self.verified.append(row['id'])
            return child
        self.verifier = patch.object(lifecycle, 'verify_native', side_effect=verify)
        self.verifier.start()
        self.addCleanup(self.verifier.stop)
        self.busy = patch.object(lifecycle, 'claude_running', return_value=False)
        self.busy.start()
        self.addCleanup(self.busy.stop)

    def prepare(self, row=None, target='b'):
        row = row or dict(ah.resolve(self.db, 's1'))
        prepared = backend.prepare_launch(self.db, row, target, self.cwd, policy='legacy')
        ah.collect(self.db)
        return prepared, dict(ah.resolve(self.db, prepared['session']))

    def finish(self, prepared, **kwargs):
        jobs.update(prepared['job'], state='exited', exit_code=0)
        item = lifecycle.finish(self.db, prepared['job'], **kwargs)
        return json.loads(lifecycle.version(self.db, item['durable_version'])['row_json'])

    def test_codex_claude_return_then_cleanup_keeps_original_and_latest_native_text(self):
        baseline = self.path.read_bytes()
        prepared, temporary = self.prepare(target='claude')
        self.add(temporary, 'Claude 新增问题')
        durable = self.finish(prepared)
        self.assertEqual(durable['tool'], 'codex')
        self.assertEqual(durable['source'], 'a')
        self.assertNotEqual(durable['id'], 's1')
        self.assertEqual(self.text(durable).count('Claude 新增问题'), 1)
        self.assertEqual(self.text(durable).count('原始问题'), 1)
        self.assertEqual(self.path.read_bytes(), baseline)
        self.assertFalse(Path(temporary['path']).exists())
        self.assertEqual(jobs.read(prepared['job'])['handoff_state'], 'cleaned')
        self.assertEqual(store.lookup(self.db, temporary)['state'], 'retired')
        store.sync_all(self.db)
        self.assertEqual(store.lookup(self.db, temporary)['state'], 'retired')
        import history_ui
        groups = store.group_rows(self.db, history_ui.list_rows(self.db))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['id'], durable['id'])
        self.assertEqual(len(store.tips(self.db, groups[0]['conversation'])), 1)

    def test_claude_codex_return_removes_native_temporary_and_preserves_original_uuids(self):
        original_path = ah.claude_project_dir(self.cwd) / 'origin.jsonl'
        original_path.parent.mkdir(parents=True)
        original_path.write_text(json.dumps(dict(type='user', sessionId='origin', uuid='original-uuid', cwd=self.cwd,
                                                 message=dict(content='原始 Claude')))+'\n', encoding='utf-8')
        ah.collect(self.db)
        original = dict(ah.resolve(self.db, 'claude:origin'))
        baseline = original_path.read_bytes()
        prepared, temporary = self.prepare(original)
        self.add(temporary, 'Codex 新增问题')
        durable = self.finish(prepared)
        self.assertEqual(durable['tool'], 'claude')
        self.assertEqual(self.text(durable).count('Codex 新增问题'), 1)
        self.assertEqual(json.loads(Path(durable['path']).read_text(encoding='utf-8').splitlines()[0])['uuid'], 'original-uuid')
        self.assertEqual(original_path.read_bytes(), baseline)
        self.assertNotIn(temporary['id'], self.native_ids(self.b))
        self.assertNotIn(temporary['id'], (self.b/'session_index.jsonl').read_text(encoding='utf-8'))

    def test_deleted_previous_target_is_replaced_by_new_id_without_resurrection(self):
        first, old = self.prepare()
        age(Path(old['path']))
        trash.delete(self.db, [dict(tool='codex', source='b', path=old['path'])])
        second, temporary = self.prepare()
        self.assertNotEqual(temporary['id'], old['id'])
        self.assertFalse(Path(old['path']).exists())
        self.add(temporary, '跨连接新增')
        durable = self.finish(second)
        third, new = self.prepare(durable)
        self.assertNotIn(new['id'], (temporary['id'], old['id']))
        self.assertFalse(Path(old['path']).exists())
        self.assertIn('跨连接新增', self.text(new))

    def test_repeat_completion_does_not_publish_or_delete_another_session(self):
        prepared, temporary = self.prepare()
        self.add(temporary, '重复完成测试')
        durable = self.finish(prepared)
        before = self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0]
        self.finish(prepared)
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], before)
        self.assertTrue(Path(durable['path']).exists())
        self.assertTrue(self.path.exists())

    def test_original_writer_lock_and_parallel_branch_keep_temporary_until_explicit_choice(self):
        prepared, temporary = self.prepare()
        self.add(temporary, '临时分支')
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with ci.writer_lock(self.a, 's1') as locked:
            self.assertTrue(locked)
            with self.assertRaisesRegex(ValueError, '原平台会话正在使用'):
                lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(Path(temporary['path']).exists())
        self.add(dict(ah.resolve(self.db, 's1')), '原平台并行分支')
        with self.assertRaisesRegex(ValueError, '原平台也有新增'):
            lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(Path(temporary['path']).exists())
        durable = self.finish(prepared, allow_branch=True)
        self.assertIn('临时分支', self.text(durable))
        self.assertNotIn('原平台并行分支', self.text(durable))
        self.assertIn('原平台并行分支', self.text(dict(ah.resolve(self.db, 's1'))))
        self.assertEqual(len(store.tips(self.db, store.lookup(self.db, durable)['conversation'])), 2)

    def test_temporary_writer_lock_and_unfinished_record_prevent_retirement(self):
        prepared, temporary = self.prepare()
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with ci.writer_lock(self.b, temporary['id']) as locked:
            self.assertTrue(locked)
            with self.assertRaisesRegex(ValueError, '临时目标仍在使用'):
                lifecycle.finish(self.db, prepared['job'])
        path = Path(temporary['path'])
        complete = path.read_bytes()
        path.write_bytes(complete+b'{"type":')
        with self.assertRaisesRegex(ValueError, '未完成记录'):
            lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(path.exists())
        path.write_bytes(complete)
        self.finish(prepared)
        self.assertFalse(path.exists())

    def test_verification_failure_keeps_copy_and_reuses_published_durable_on_retry(self):
        prepared, temporary = self.prepare()
        self.add(temporary, '要验证的内容')
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with patch.object(lifecycle, 'verify_native', side_effect=ValueError('Native verification failed')):
            outcome = lifecycle.retry_pending(self.db, identity=prepared['job'])
        self.assertEqual(outcome['pending'], 1)
        self.assertTrue(Path(temporary['path']).exists())
        before = self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0]
        self.finish(prepared)
        self.assertFalse(Path(temporary['path']).exists())
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], before)

    def test_abnormal_exit_is_preserved_then_manual_retry_is_safe(self):
        prepared, temporary = self.prepare()
        self.add(temporary, '异常退出前已保存')
        with patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 12)):
            self.assertEqual(runner.run(prepared['job'], ['-Command', 'fixture']), 12)
        self.assertTrue(Path(temporary['path']).exists())
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'failed')
        lifecycle.retry_pending(self.db, manual=True, identity=prepared['job'])
        self.assertFalse(Path(temporary['path']).exists())

    def test_successful_runner_finishes_without_ui(self):
        prepared, temporary = self.prepare()
        self.add(temporary, '无 UI 自动同步')
        with patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(runner.run(prepared['job'], ['-Command', 'fixture']), 0)
        self.assertFalse(Path(temporary['path']).exists())
        self.assertEqual(jobs.read(prepared['job'])['handoff_state'], 'cleaned')

    def test_crash_between_move_and_native_cleanup_recovers_same_durable(self):
        prepared, temporary = self.prepare()
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with patch.object(lifecycle, 'retire', side_effect=RuntimeError('crash before cleanup')):
            with self.assertRaises(RuntimeError):
                lifecycle.finish(self.db, prepared['job'])
        item = lifecycle.get(self.db, prepared['job'])
        before = item['durable_version']
        folder = ah.DATA/'handoffs'/'cleanup'/prepared['job']
        folder.mkdir(parents=True)
        saved = folder/Path(temporary['path']).name
        os.replace(temporary['path'], saved)
        lifecycle.update(self.db, prepared['job'], state='cleaning', cleanup_path=str(saved))
        durable = self.finish(prepared)
        self.assertEqual(lifecycle.get(self.db, prepared['job'])['durable_version'], before)
        self.assertFalse(saved.exists())
        self.assertNotIn(temporary['id'], self.native_ids(self.b))
        self.assertTrue(Path(durable['path']).exists())

    def test_unowned_import_and_native_session_are_never_automatically_retired(self):
        copied = ci.import_session(self.db, ah.resolve(self.db, 's1'), self.a, 'b', self.b)
        prepared = backend.prepare_launch(self.db, dict(ah.resolve(self.db, 's1')), 'a', self.cwd, policy='legacy')
        jobs.update(prepared['job'], state='exited', exit_code=0)
        self.assertIsNone(lifecycle.finish(self.db, prepared['job']))
        self.assertTrue(Path(copied['path']).exists())
        self.assertTrue(self.path.exists())

    def test_malformed_complete_record_is_not_silently_dropped(self):
        prepared, temporary = self.prepare()
        path = Path(temporary['path'])
        with path.open('ab') as stream:
            stream.write(b'{invalid-json}\n')
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with self.assertRaisesRegex(ValueError, '损坏记录'):
            lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(path.exists())

    def test_native_cleanup_failure_restores_file_and_recollects_new_content(self):
        prepared, temporary = self.prepare()
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with patch.object(trash, 'native_remove', side_effect=RuntimeError('native database busy')):
            with self.assertRaises(RuntimeError):
                lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(Path(temporary['path']).exists())
        previous = lifecycle.get(self.db, prepared['job'])['durable_version']
        self.add(temporary, '清理失败后新增内容')
        durable = self.finish(prepared)
        self.assertIn('清理失败后新增内容', self.text(durable))
        self.assertNotEqual(lifecycle.get(self.db, prepared['job'])['durable_version'], previous)
        self.assertFalse(Path(temporary['path']).exists())

    def test_crash_after_durable_publication_recovers_without_duplicate_import(self):
        prepared, temporary = self.prepare()
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with patch.object(store, 'register_target', side_effect=RuntimeError('crash after native publication')):
            with self.assertRaises(RuntimeError):
                lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(Path(temporary['path']).exists())
        before = self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0]
        self.finish(prepared)
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], before)

    def test_cleanup_does_not_touch_claude_attachment_or_subagent_sidecars(self):
        prepared, temporary = self.prepare(target='claude')
        sidecar = Path(temporary['path']).with_suffix('') / 'subagents' / 'owned-data.txt'
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text('attachment remains available', encoding='utf-8')
        self.finish(prepared)
        self.assertTrue(sidecar.is_file())

    def test_prepared_process_crash_is_reconciled_and_requires_manual_recovery(self):
        prepared, temporary = self.prepare()
        with patch.object(jobs, 'process_alive', return_value=False):
            jobs.reconcile()
        self.assertEqual(jobs.read(prepared['job'])['state'], 'interrupted')
        lifecycle.retry_pending(self.db, identity=prepared['job'])
        self.assertTrue(Path(temporary['path']).exists())
        lifecycle.retry_pending(self.db, manual=True, identity=prepared['job'])
        self.assertFalse(Path(temporary['path']).exists())

    def test_metadata_only_origin_update_is_not_a_parallel_continuation(self):
        prepared, temporary = self.prepare()
        self.add(temporary, '新增内容')
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(type='turn_context', payload=dict(model='metadata-only'))) + '\n')
        durable = self.finish(prepared)
        self.assertIn('新增内容', self.text(durable))
        self.assertFalse(Path(temporary['path']).exists())

    def test_unrelated_unreadable_version_does_not_block_own_runner_completion(self):
        from tests.test_trash import rollout
        unrelated = self.a / 'sessions' / 'unrelated.jsonl'
        unrelated.write_text(rollout('unrelated', '其他对话', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        store.enroll(self.db, dict(ah.resolve(self.db, 'unrelated')))
        prepared, temporary = self.prepare()
        self.add(temporary, '自己的新增内容')
        with unrelated.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(type='turn_context', payload={})) + '\n')
        actual = store.read_native
        def read(row):
            if row['path'] == str(unrelated):
                raise OSError('unrelated unavailable')
            return actual(row)
        with patch.object(store, 'read_native', side_effect=read), \
                patch.object(store, 'sync_all', side_effect=ValueError('unrelated synchronization failure')), \
                patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(runner.run(prepared['job'], ['-Command', 'fixture']), 0)
        self.assertEqual(jobs.read(prepared['job'])['handoff_state'], 'cleaned')
        self.assertFalse(Path(temporary['path']).exists())

    def test_disabled_origin_keeps_temporary_until_reenabled(self):
        import setup_backend
        prepared, temporary = self.prepare()
        jobs.update(prepared['job'], state='exited', exit_code=0)
        setup_backend.atomic_json(ah.DATA / 'sources.json', dict(version=1, codex=['b'], claude=[]))
        with self.assertRaisesRegex(ValueError, '来源已停用'):
            lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(Path(temporary['path']).exists())
        setup_backend.atomic_json(ah.DATA / 'sources.json', dict(version=1, codex=['a', 'b'], claude=[]))
        lifecycle.finish(self.db, prepared['job'])
        self.assertFalse(Path(temporary['path']).exists())
