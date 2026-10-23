"""Single current transcript, exclusive launch, and recoverable retirement."""
import json
import contextlib
import subprocess
import os
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import agent_history as ah
import codex_import as ci
import conversation_store as store
import handoff_lifecycle as lifecycle
import launch_backend as backend
import launch_job_runner as runner
import launch_jobs as jobs
import single_conversation as single
import trash
from tests.test_trash import Fixture, rollout
from tests import test_conversation_store as conversation_tests


class SingleConversationTests(Fixture):
    text = conversation_tests.ConversationTests.text

    def test_same_connection_exit_does_not_walk_or_index_unrelated_histories(self):
        original = dict(ah.resolve(self.db, 's1'))
        other = self.a / 'sessions' / 'unrelated.jsonl'
        other.write_text(rollout('unrelated', 'old content', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        prepared = backend.prepare_launch(self.db, original, 'a', self.cwd)
        jobs.update(prepared['job'], state='exited', exit_code=0)
        other.write_text(rollout('unrelated', 'new content', self.cwd), encoding='utf-8')
        with patch.object(trash, 'fork_index', side_effect=AssertionError('No files are being retired')), \
                patch.object(Path, 'rglob', side_effect=AssertionError('Unrelated directory walk')), \
                patch.object(Path, 'glob', side_effect=AssertionError('Unrelated directory walk')):
            single.finish(self.db, prepared['job'])
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'collected')
        self.assertEqual([r['text'] for r in ah.messages(self.db, 'codex:unrelated')], ['old content'])

    def test_completion_waits_for_affected_index_and_keeps_reservation_when_indexing_fails(self):
        prepared, current = self.prepare()
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with patch.object(ah, 'collect', side_effect=OSError('synthetic index failure')):
            with self.assertRaises(OSError):
                single.finish(self.db, prepared['job'])
        self.assertNotEqual(jobs.read(prepared['job'])['sync_state'], 'collected')
        self.assertNotEqual(lifecycle.get(self.db, prepared['job'])['state'], 'cleaned')
        self.assertIsNotNone(self.db.execute('SELECT job FROM conversation_runs WHERE job=?', (prepared['job'],)).fetchone())
        self.assertTrue(Path(current['path']).is_file())
        single.finish(self.db, prepared['job'], manual=True)
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'collected')
        self.assertIsNone(self.db.execute('SELECT job FROM conversation_runs WHERE job=?', (prepared['job'],)).fetchone())
        self.assertEqual(ah.resolve(self.db, current['id'])['path'], current['path'])

    def test_retirement_reindexes_existing_same_id_copies_in_other_connections(self):
        duplicate = self.b / 'sessions' / 'independent-copy.jsonl'
        duplicate.write_bytes(self.path.read_bytes())
        ah.collect(self.db)
        prepared, current = self.prepare()
        self.finish(prepared)
        self.assertFalse(self.path.exists())
        self.assertTrue(duplicate.is_file())
        self.assertEqual(ah.resolve(self.db, 's1')['path'], str(duplicate))
        self.assertEqual(ah.resolve(self.db, current['id'])['path'], current['path'])

    def add(self, row, question, answer='新回答'):
        if row['tool'] == 'codex':
            return conversation_tests.ConversationTests.add(self, row, question, answer)
        with Path(row['path']).open('a', encoding='utf-8') as stream:
            for role, text in [('user', question), ('assistant', answer)]:
                stream.write(json.dumps(dict(type=role, sessionId=row['id'], cwd=self.cwd,
                    uuid=str(uuid.uuid4()), message=dict(content=text, model='fixture-model')),
                    ensure_ascii=False) + '\n')
        ah.collect(self.db)
        store.sync_all(self.db)

    def setUp(self):
        super().setUp()
        def verify(db, child, final):
            row = json.loads(child['row_json'])
            self.assertTrue(Path(row['path']).is_file())
            self.assertEqual(store.revision(db, child['head'])['events'], final['events'])
            if row['tool'] == 'codex':
                self.assertIsNotNone(ci.source_row(dict(ah.codex_homes())[row['source']], row['id']))
            return child
        verifier = patch.object(lifecycle, 'verify_native', side_effect=verify)
        verifier.start()
        self.addCleanup(verifier.stop)
        busy = patch.object(lifecycle, 'claude_running', return_value=False)
        busy.start()
        self.addCleanup(busy.stop)

    def prepare(self, row=None, target='b'):
        row = row or dict(ah.resolve(self.db, 's1'))
        prepared = backend.prepare_launch(self.db, row, target, self.cwd)
        ah.collect(self.db)
        return prepared, dict(ah.resolve(self.db, prepared['session']))

    def finish(self, prepared):
        jobs.update(prepared['job'], state='exited', exit_code=0)
        lifecycle.finish(self.db, prepared['job'])

    def test_roundtrips_promote_target_and_keep_one_native_file_with_full_canonical_history(self):
        current = dict(ah.resolve(self.db, 's1'))
        first = store.enroll(self.db, current)
        original = self.path.read_bytes()
        markers = []
        for index, target in enumerate(('claude', 'b', 'a', 'claude', 'a')):
            previous = current
            prepared, current = self.prepare(current, target)
            self.assertEqual(lifecycle.get(self.db, prepared['job'])['policy'], 'single')
            markers.append('往返新增问题 ' + str(index))
            self.add(current, markers[-1])
            self.finish(prepared)
            self.assertFalse(Path(previous['path']).exists())
            self.assertTrue(Path(current['path']).exists())
            version = store.lookup(self.db, current)
            self.assertEqual(len(store.members(self.db, version['conversation'], include_merged=True)), 1)
            self.assertEqual(self.db.execute('SELECT active_version FROM conversations WHERE id=?',
                                            (version['conversation'],)).fetchone()[0], version['id'])
            self.assertEqual(lifecycle.get(self.db, prepared['job'])['durable_version'], version['id'])
            self.assertIsNone(self.db.execute('SELECT 1 FROM conversation_runs').fetchone())
            for marker in markers:
                self.assertEqual(self.text(current).count(marker), 1)
            self.assertEqual(len({path.resolve() for _, _, path, _ in ah.discover() if path.is_file()}), 1)
        snapshot = store.revision(self.db, first['baseline'])
        self.assertIn('原始问题', json.dumps(snapshot['records'], ensure_ascii=False))
        self.assertTrue(original)

    def test_same_connection_reuses_id_and_does_not_publish_a_copy(self):
        prepared, current = self.prepare(target='a')
        self.assertEqual(current['id'], 's1')
        self.assertIsNone(lifecycle.get(self.db, prepared['job']))
        self.add(current, '同连接继续')
        jobs.update(prepared['job'], state='exited', exit_code=0)
        single.finish(self.db, prepared['job'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], 0)
        self.assertTrue(self.path.exists())
        self.assertIsNone(self.db.execute('SELECT 1 FROM conversation_runs').fetchone())

    def test_prepared_launch_blocks_all_other_targets_before_publication(self):
        prepared, current = self.prepare()
        count = self.db.execute('SELECT count(*) FROM conversation_versions').fetchone()[0]
        for row, target in ((dict(ah.resolve(self.db, 's1')), 'a'), (current, 'claude')):
            with self.assertRaisesRegex(ValueError, '已准备启动'):
                self.prepare(row, target)
        self.assertEqual(self.db.execute('SELECT count(*) FROM conversation_versions').fetchone()[0], count)
        self.finish(prepared)
        self.prepare(current, 'a')

    def test_cancel_unclaimed_launch_preserves_all_files_and_rejects_late_runner(self):
        prepared, current = self.prepare()
        files = {path: path.read_bytes() for path in (self.path, Path(current['path']))}
        result = single.cancel_prepared(self.db, prepared['job'])
        self.assertTrue(result['files_preserved'])
        self.assertEqual(jobs.read(prepared['job'])['state'], 'cancelled')
        self.assertIsNone(self.db.execute('SELECT 1 FROM conversation_runs').fetchone())
        self.assertEqual(lifecycle.retry_pending(self.db, manual=True), dict(completed=0, pending=0))
        with patch.object(runner.subprocess, 'run') as execute:
            self.assertEqual(runner.run(prepared['job'], ['fixture']), 1)
        execute.assert_not_called()
        self.assertTrue(all(path.read_bytes() == data for path, data in files.items()))
        with self.assertRaisesRegex(ValueError, '已取消'):
            single.finish(self.db, prepared['job'], manual=True)

    def test_native_busy_prepared_launch_is_reported_accurately_and_cannot_be_cancelled(self):
        prepared, current = self.prepare()
        member = store.lookup(self.db, current)
        with ci.writer_lock(self.b, current['id']) as locked:
            self.assertTrue(locked)
            self.assertIn('原生会话正在 Agent 中使用', single.reservation_reason(self.db, member))
            with self.assertRaisesRegex(ValueError, '仍在 Agent 中使用'):
                single.cancel_prepared(self.db, prepared['job'])
        self.assertEqual(jobs.read(prepared['job'])['state'], 'prepared')
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM conversation_runs').fetchone())

    def test_cancel_commit_before_sql_release_recovers_without_cleaning_native_files(self):
        prepared, current = self.prepare()
        jobs.cancel_prepared(prepared['job'])
        result = lifecycle.retry_pending(self.db, manual=True, identity=prepared['job'])
        self.assertEqual(result, dict(completed=0, pending=0))
        self.assertIsNone(self.db.execute('SELECT 1 FROM conversation_runs').fetchone())
        self.assertTrue(self.path.exists() and Path(current['path']).exists())

    def test_resume_switch_releases_only_idle_observed_session_and_keeps_terminal_running(self):
        prepared, current = self.prepare()
        jobs.update(prepared['job'], state='running', pid=os.getpid(), attempts=1)
        with ci.writer_lock(self.b, current['id']) as locked:
            self.assertTrue(locked)
            self.assertTrue(runner.observe_native_session(prepared['job'], current, self.b))
            with self.assertRaisesRegex(ValueError, '原生写锁'):
                single.detach_session(self.db, prepared['job'])
        self.add(current, '原生切换前已保存的内容')
        result = single.detach_session(self.db, prepared['job'])
        report = jobs.read(prepared['job'])
        self.assertEqual(report['state'], 'running')
        self.assertTrue(report['session_detached'])
        self.assertNotIn('exit_code', report)
        self.assertTrue(result['files_preserved'])
        self.assertIsNone(self.db.execute('SELECT 1 FROM conversation_runs').fetchone())
        self.assertIn('原生切换前已保存的内容', self.text(current))
        self.assertTrue(self.path.exists() and Path(current['path']).exists())
        newer, next_row = self.prepare(current, 'b')
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with self.assertRaisesRegex(ValueError, '另一启动任务'):
            single.finish(self.db, prepared['job'])
        self.assertEqual(jobs.read(newer['job'])['state'], 'prepared')
        self.assertTrue(Path(next_row['path']).exists())

    def test_startup_gap_and_old_reports_are_not_mistaken_for_native_resume_switch(self):
        prepared, _ = self.prepare()
        jobs.update(prepared['job'], state='running', pid=os.getpid(), attempts=1)
        with self.assertRaisesRegex(ValueError, '尚未记录原生会话接管'):
            single.detach_session(self.db, prepared['job'])
        with self.assertRaisesRegex(ValueError, '终端已接管'):
            single.cancel_prepared(self.db, prepared['job'])
        self.assertEqual(jobs.read(prepared['job'])['state'], 'running')

    def test_connection_aliases_do_not_make_an_idle_native_session_lock_itself(self):
        prepared, current = self.prepare(target='a')
        member = dict(store.lookup(self.db, current))
        alias = dict(member, row_json=json.dumps(dict(current, source='old')))
        with patch.object(single, 'versions', return_value=[member, alias]):
            single.cancel_prepared(self.db, prepared['job'])
        self.assertEqual(jobs.read(prepared['job'])['state'], 'cancelled')
        self.assertTrue(Path(current['path']).exists())

    def test_finished_job_report_repair_does_not_release_a_new_reservation(self):
        prepared, current = self.prepare()
        self.finish(prepared)
        newer, _ = self.prepare(current, 'b')
        jobs.update(prepared['job'], sync_state='failed', sync_error='synthetic interrupted report')
        single.finish(self.db, prepared['job'], manual=True)
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'collected')
        self.assertEqual(self.db.execute('SELECT job FROM conversation_runs').fetchone()[0], newer['job'])
        self.assertTrue(Path(current['path']).exists())

    def test_native_writer_lock_prevents_switch_before_creating_a_target(self):
        with ci.writer_lock(self.a, 's1') as locked:
            self.assertTrue(locked)
            with self.assertRaisesRegex(ValueError, '正在 Agent 中使用'):
                self.prepare()
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], 0)
        self.assertEqual(jobs.reports(), [])

    def test_parallel_external_messages_are_preserved_and_not_retired(self):
        original = dict(ah.resolve(self.db, 's1'))
        prepared, current = self.prepare()
        self.add(original, '外部窗口并行新增')
        self.add(current, '主窗口新增')
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with self.assertRaisesRegex(ValueError, '未合入的新增内容'):
            lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(self.path.exists())
        self.assertTrue(Path(current['path']).exists())
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM conversation_runs').fetchone())

    def test_verification_failure_preserves_both_files_and_retry_creates_no_return_copy(self):
        prepared, current = self.prepare()
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with patch.object(lifecycle, 'verify_native', side_effect=ValueError('readback failed')):
            with self.assertRaisesRegex(ValueError, 'readback failed'):
                lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(self.path.exists())
        self.assertTrue(Path(current['path']).exists())
        count = self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0]
        self.finish(prepared)
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], count)
        self.assertFalse(self.path.exists())

    def test_cache_cleanup_crash_recovers_quarantine_and_same_native_target(self):
        prepared, current = self.prepare()
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with patch.object(trash, 'remove_cache_lines', return_value=None):
            with self.assertRaisesRegex(ValueError, '缓存正在使用'):
                lifecycle.finish(self.db, prepared['job'])
        self.assertFalse(self.path.exists())
        journal = self.db.execute('SELECT * FROM conversation_retirements').fetchone()
        self.assertTrue(Path(journal['saved']).is_file())
        self.assertTrue(Path(current['path']).is_file())
        self.finish(prepared)
        self.assertFalse(Path(journal['saved']).exists())
        self.assertNotIn('s1', self.native_ids(self.a))
        self.assertNotIn('"s1"', (self.a / 'session_index.jsonl').read_text())
        self.assertEqual(self.db.execute('SELECT state FROM conversation_retirements').fetchone()[0], 'done')

    def test_native_cleanup_failure_restores_source_and_retry_preserves_new_messages(self):
        prepared, current = self.prepare()
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with patch.object(trash, 'native_remove', side_effect=OSError('native SQL unavailable')):
            with self.assertRaises(OSError):
                lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(self.path.is_file())
        self.add(current, '失败后继续写入')
        self.finish(prepared)
        self.assertIn('失败后继续写入', self.text(current))
        self.assertFalse(self.path.exists())

    def test_abnormal_exit_requires_manual_recovery_and_keeps_original_until_verified(self):
        prepared, current = self.prepare()
        self.add(current, '异常退出前保存')
        with patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 12)):
            self.assertEqual(runner.run(prepared['job'], ['fixture']), 12)
        self.assertTrue(self.path.exists())
        self.assertTrue(Path(current['path']).exists())
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'failed')
        result = lifecycle.retry_pending(self.db, manual=True, identity=prepared['job'])
        self.assertEqual(result['completed'], 1)
        self.assertFalse(self.path.exists())
        self.assertTrue(Path(current['path']).exists())

    def test_old_native_runner_cannot_execute_after_another_launch_reserves_conversation(self):
        old, current = self.prepare(target='a')
        jobs.update(old['job'], state='cancelled', sync_state='not-needed')
        new, _ = self.prepare(current, 'b')
        with patch.object(runner.subprocess, 'run') as execute:
            self.assertEqual(runner.run(old['job'], ['fixture']), 1)
        execute.assert_not_called()
        self.assertEqual(jobs.read(new['job'])['state'], 'prepared')
        self.assertEqual(self.db.execute('SELECT job FROM conversation_runs').fetchone()[0], new['job'])

    def test_successful_runner_promotes_used_id_without_ui(self):
        prepared, current = self.prepare()
        self.add(current, '退出进程自动收尾')
        with patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(runner.run(prepared['job'], ['fixture']), 0)
        self.assertFalse(self.path.exists())
        self.assertTrue(Path(current['path']).exists())
        self.assertEqual(jobs.read(prepared['job'])['durable_session'], current['id'])

    def test_missing_source_is_not_resurrected_and_missing_latest_is_not_deleted_further(self):
        prepared, current = self.prepare()
        self.path.unlink()
        self.finish(prepared)
        self.assertFalse(self.path.exists())
        self.assertTrue(Path(current['path']).exists())
        prepared, newer = self.prepare(current, 'a')
        Path(newer['path']).unlink()
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with self.assertRaisesRegex(ValueError, '最新会话已删除'):
            lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(Path(current['path']).exists())

    def test_external_native_fork_reference_prevents_parent_retirement(self):
        prepared, current = self.prepare()
        child = self.a / 'sessions' / 'external-fork.jsonl'
        child.write_text(rollout('external', '外部原生分支', self.cwd, base='s1'), encoding='utf-8')
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with self.assertRaisesRegex(ValueError, '原生分支引用'):
            lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(self.path.exists())
        self.assertTrue(Path(current['path']).exists())

    def test_half_record_preserves_both_files(self):
        prepared, current = self.prepare()
        with Path(current['path']).open('ab') as stream:
            stream.write(b'{"type":')
        jobs.update(prepared['job'], state='exited', exit_code=0)
        with self.assertRaisesRegex(ValueError, '未完成记录'):
            lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(self.path.exists())
        self.assertTrue(Path(current['path']).exists())

    def test_previously_merged_branches_are_retired_using_saved_merge_signatures(self):
        import conversation_merge as merge
        original = dict(ah.resolve(self.db, 's1'))
        first = backend.prepare_launch(self.db, original, 'b', self.cwd, policy='legacy')
        ah.collect(self.db)
        copied = dict(ah.resolve(self.db, first['session']))
        self.add(original, '合并前左侧内容')
        self.add(copied, '合并前右侧内容')
        cid = store.lookup(self.db, original)['conversation']
        merged = merge.apply(self.db, merge.plan(self.db, cid))['row']
        self.assertEqual(len(store.members(self.db, cid, include_merged=True)), 3)
        prepared, current = self.prepare(merged, 'claude')
        self.add(current, '合并后续聊')
        self.finish(prepared)
        self.assertEqual(len(store.members(self.db, cid, include_merged=True)), 1)
        for row in (original, copied, merged):
            self.assertFalse(Path(row['path']).exists())
        for marker in ('合并前左侧内容', '合并前右侧内容', '合并后续聊'):
            self.assertIn(marker, self.text(current))

    def test_running_job_cannot_be_spawned_twice_or_have_its_report_overwritten(self):
        prepared, current = self.prepare()
        jobs.update(prepared['job'], state='running', pid=os.getpid())
        with patch.object(runner.subprocess, 'run') as execute:
            self.assertEqual(runner.run(prepared['job'], ['fixture']), 1)
        execute.assert_not_called()
        self.assertEqual(jobs.read(prepared['job'])['state'], 'running')
        self.assertTrue(self.path.exists())
        self.assertTrue(Path(current['path']).exists())

    def test_disabled_old_source_is_not_retired_until_reenabled(self):
        from setup_backend import atomic_json
        prepared, current = self.prepare()
        jobs.update(prepared['job'], state='exited', exit_code=0)
        atomic_json(ah.DATA / 'sources.json', dict(version=1, codex=['b'], claude=[]))
        with self.assertRaisesRegex(ValueError, '旧会话来源已停用'):
            lifecycle.finish(self.db, prepared['job'])
        self.assertTrue(self.path.exists())
        atomic_json(ah.DATA / 'sources.json', dict(version=1, codex=['a', 'b'], claude=[]))
        self.finish(prepared)
        self.assertFalse(self.path.exists())
        self.assertTrue(Path(current['path']).exists())

    def test_explicit_old_version_cannot_create_a_new_branch(self):
        original = dict(ah.resolve(self.db, 's1'))
        old = backend.prepare_launch(self.db, original, 'b', self.cwd, policy='legacy')
        ah.collect(self.db)
        copied = dict(ah.resolve(self.db, old['session']))
        self.add(copied, '较新版本内容')
        count = self.db.execute('SELECT count(*) FROM conversation_versions').fetchone()[0]
        with self.assertRaisesRegex(ValueError, '不是最新主对话'):
            self.prepare(original, 'claude')
        self.assertEqual(self.db.execute('SELECT count(*) FROM conversation_versions').fetchone()[0], count)

    def test_schema_upgrade_keeps_existing_handoff_cleanup_policy(self):
        original = dict(ah.resolve(self.db, 's1'))
        old = backend.prepare_launch(self.db, original, 'b', self.cwd, policy='legacy')
        self.db.execute('ALTER TABLE temporary_handoffs DROP COLUMN policy')
        self.db.execute('DROP TABLE conversation_runs')
        self.db.execute('DROP TABLE conversation_retirements')
        self.db.commit()
        store.ensure(self.db)
        self.assertEqual(lifecycle.get(self.db, old['job'])['policy'], 'legacy')
        self.assertEqual(self.db.execute('SELECT count(*) FROM conversation_runs').fetchone()[0], 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows extended-length paths')
    def test_windows_extended_native_path_is_equivalent_for_resume_and_retirement(self):
        alias = '\\\\?\\' + str(self.path)
        with contextlib.closing(sqlite3.connect(ci.state_path(self.a))) as db:
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?', (alias, 's1'))
            db.commit()
        prepared, current = self.prepare(target='a')
        self.assertEqual(current['id'], 's1')
        jobs.update(prepared['job'], state='exited', exit_code=0)
        single.finish(self.db, prepared['job'])
        prepared, current = self.prepare(current, 'b')
        self.finish(prepared)
        self.assertFalse(self.path.exists())
        self.assertTrue(Path(current['path']).is_file())
        self.assertNotIn('s1', self.native_ids(self.a))

    def test_completed_handoff_retry_repairs_status_without_republishing(self):
        prepared, current = self.prepare()
        self.finish(prepared)
        jobs.update(prepared['job'], handoff_state='pending', sync_state='failed', sync_error='interrupted report')
        count = self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0]
        lifecycle.retry_pending(self.db, manual=True, identity=prepared['job'])
        self.assertEqual(jobs.read(prepared['job'])['handoff_state'], 'cleaned')
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'collected')
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], count)
        self.assertTrue(Path(current['path']).exists())
        with patch.object(runner.subprocess, 'run') as execute:
            self.assertEqual(runner.run(prepared['job'], ['fixture']), 1)
        execute.assert_not_called()

    def test_manual_retry_reconciles_dead_runner_without_prior_global_refresh(self):
        for target in ('a', 'b'):
            with self.subTest(target=target):
                prepared, current = self.prepare(target=target)
                jobs.update(prepared['job'], state='running', pid=999999)
                version = store.lookup(self.db, current)
                with patch.object(jobs, 'process_alive', return_value=False):
                    self.assertIn('重试同步', single.reservation_reason(self.db, version))
                    result = lifecycle.retry_pending(self.db, manual=True, identity=prepared['job'])
                self.assertEqual(result, dict(completed=1, pending=0))
                self.assertEqual(jobs.read(prepared['job'])['state'], 'interrupted')
                self.assertIsNone(self.db.execute('SELECT 1 FROM conversation_runs').fetchone())
                self.assertTrue(Path(current['path']).exists())

    def test_manual_retry_keeps_live_runner_and_counts_it_as_pending(self):
        prepared, current = self.prepare()
        jobs.update(prepared['job'], state='running', pid=os.getpid())
        before = Path(current['path']).read_bytes()
        result = lifecycle.retry_pending(self.db, manual=True, identity=prepared['job'])
        self.assertEqual(result, dict(completed=0, pending=1))
        self.assertEqual(jobs.read(prepared['job'])['state'], 'running')
        self.assertIn('正在使用', single.reservation_reason(self.db, store.lookup(self.db, current)))
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM conversation_runs').fetchone())
        self.assertEqual(Path(current['path']).read_bytes(), before)

    def test_selected_conversation_retry_does_not_fall_back_to_unrelated_job(self):
        from history_ui import recovery_job
        prepared, current = self.prepare()
        unrelated = self.a / 'sessions' / 'unrelated.jsonl'
        unrelated.write_text(rollout('unrelated', '另一个问题', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        row = dict(ah.resolve(self.db, 'unrelated'))
        self.assertIsNone(recovery_job(self.db, row, prepared['job']))
        store.enroll(self.db, row)
        self.assertIsNone(recovery_job(self.db, row, prepared['job']))
        self.assertEqual(recovery_job(self.db, current, None), prepared['job'])

    def test_crash_between_native_reservation_and_report_write_recovers_on_start_or_retry(self):
        from setup_backend import atomic_json
        for via_runner in (True, False):
            prepared, current = self.prepare(target='a')
            report = jobs.read(prepared['job'])
            for key in ('single_head', 'conversation', 'version_id'):
                report.pop(key)
            atomic_json(jobs.path(prepared['job']), report)
            if via_runner:
                with patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                    self.assertEqual(runner.run(prepared['job'], ['fixture']), 0)
            else:
                jobs.update(prepared['job'], state='interrupted')
                result = lifecycle.retry_pending(self.db, manual=True, identity=prepared['job'])
                self.assertEqual(result['completed'], 1)
            self.assertTrue(jobs.read(prepared['job'])['single_head'])
            self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'collected')
            self.assertIsNone(self.db.execute('SELECT 1 FROM conversation_runs').fetchone())
            self.assertTrue(Path(current['path']).exists())
