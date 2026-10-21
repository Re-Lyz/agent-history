"""Recovery integrity, process ownership and request isolation on synthetic data."""
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
from unittest.mock import patch

import agent_history as ah
import handoff_lifecycle as lifecycle
import launch_backend as backend
import launch_job_runner as runner
import launch_jobs as jobs
import setup_backend as setup
import trash
from tests.test_trash import Fixture, age


class FrameworkSafetyTests(Fixture):
    def deleted(self):
        return trash.delete(self.db, [dict(tool='codex', source='a', path=str(self.path))])

    def expire(self, entry):
        entry['expires_at'] = (trash.utcnow() - datetime.timedelta(seconds=1)).isoformat()
        trash.write_manifest(trash.trash_root() / entry['id'], entry)

    def test_corrupt_bin_file_is_not_published_or_discarded(self):
        entry = self.deleted()
        folder = trash.trash_root() / entry['id']
        stored = folder / entry['items'][0]['stored']
        stored.write_bytes(b'corrupt synthetic backup\n')
        result = trash.restore(self.db, entry['id'])
        self.assertEqual((result['restored'], len(result['errors'])), (0, 1))
        self.assertFalse(self.path.exists())
        self.assertTrue(stored.exists() and (folder / 'manifest.json').exists())
        self.assertEqual(self.native_ids(self.a), [])

    def test_change_during_move_stops_native_index_restoration(self):
        entry = self.deleted()
        actual = trash.shutil.move
        def changed(source, target):
            result = actual(source, target)
            Path(target).write_bytes(b'changed during synthetic restore\n')
            return result
        with patch.object(trash.shutil, 'move', side_effect=changed):
            result = trash.restore(self.db, entry['id'])
        self.assertEqual(result['restored'], 0)
        self.assertEqual(self.native_ids(self.a), [])
        self.assertTrue((trash.trash_root() / entry['id'] / 'manifest.json').exists())

    def test_purge_cannot_race_restore_in_another_thread(self):
        entry = self.deleted()
        self.expire(entry)
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        actual = trash.shutil.move
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('synthetic restore was not released')
            return actual(*args, **kwargs)
        def restore():
            try:
                with contextlib.closing(ah.connect()) as db:
                    results.append(trash.restore(db, entry['id']))
            except Exception as error:
                errors.append(error)
        with patch.object(trash.shutil, 'move', side_effect=blocked):
            worker = threading.Thread(target=restore)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(trash.purge(), [])
                with self.assertRaisesRegex(ValueError, '回收站正在'):
                    trash.purge(entry['id'])
                self.assertTrue((trash.trash_root() / entry['id'] / entry['items'][0]['stored']).is_file())
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]['restored'], 1)
        self.assertTrue(self.path.is_file())

    def test_failed_recovery_journal_is_exempt_from_automatic_expiry(self):
        entry = self.deleted()
        self.expire(entry)
        with patch.object(trash, 'native_restore', side_effect=sqlite3.OperationalError('synthetic busy')):
            self.assertEqual(trash.restore(self.db, entry['id'])['restored'], 0)
        self.assertEqual(trash.purge(), [])
        self.assertEqual(trash.restore(self.db, entry['id'])['restored'], 1)

    def claude_file(self, home=None):
        path = (home or self.claude) / 'projects' / 'synthetic-project' / 'claude-safety.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(type='user', sessionId='claude-safety', cwd=self.cwd,
            message=dict(content='Synthetic safety question'))) + '\n', encoding='utf-8')
        age(path)
        ah.collect(self.db)
        return path

    def test_active_or_uninspectable_claude_session_is_not_moved(self):
        path = self.claude_file()
        target = dict(tool='claude', source='claude', path=str(path))
        for effect in (True, ValueError('cannot inspect synthetic process')):
            with self.subTest(effect=type(effect).__name__), \
                    patch.object(lifecycle, 'claude_running', side_effect=effect if isinstance(effect, Exception)
                                 else None, return_value=True) as busy:
                result = trash.delete_many(self.db, [('synthetic', [target])])
                self.assertEqual(result['deleted'], 0)
                self.assertEqual(len(result['skipped']), 1)
                busy.assert_called_once()
            self.assertTrue(path.exists())
        self.assertEqual(trash.entries(), [])

    def test_configured_claude_home_can_be_deleted_restored_and_tracked(self):
        custom = self.root / 'custom-claude'
        setup.atomic_json(ah.DATA / 'sources.json', dict(version=1, codex=['a'], claude=[str(custom)]))
        path = self.claude_file(custom)
        with patch.object(lifecycle, 'claude_running', return_value=False):
            entry = trash.delete(self.db, [dict(tool='claude', source='claude', path=str(path))])
        self.assertEqual(entry['items'][0]['home'], str(custom.resolve()))
        self.assertEqual(trash.restore(self.db, entry['id'])['restored'], 1)
        self.assertTrue(path.exists())
        prepared = backend.prepare_new('claude', self.cwd)
        self.assertEqual(jobs.read(prepared['job'])['history_roots'], [str(custom / 'projects')])

    def test_posix_claude_check_matches_native_process_and_session(self):
        proc = self.root / 'synthetic-proc'
        directory = proc / '1234'
        directory.mkdir(parents=True)
        command = directory / 'cmdline'
        command.write_bytes(b'/usr/bin/node\0/synthetic/claude/cli.js\0--resume\0claude-safety\0')
        self.assertTrue(lifecycle.claude_running_posix(dict(id='claude-safety'), proc))
        self.assertFalse(lifecycle.claude_running_posix(dict(id='different-session'), proc))
        command.write_bytes(b'/usr/bin/python\0claude-safety\0')
        self.assertFalse(lifecycle.claude_running_posix(dict(id='claude-safety'), proc))
        with self.assertRaises(ValueError):
            lifecycle.claude_running_posix(dict(id='claude-safety'), proc / 'absent')

    def test_new_and_legacy_job_replays_preserve_existing_reports(self):
        for legacy in (False, True):
            for state in ('running', 'cancelled', 'exited'):
                with self.subTest(legacy=legacy, state=state):
                    prepared = backend.prepare_launch(self.db, dict(ah.resolve(self.db, 's1')), 'a',
                        self.cwd, policy='legacy') if legacy else backend.prepare_new('a', self.cwd)
                    jobs.update(prepared['job'], state=state, pid=os.getpid())
                    before = jobs.read(prepared['job'])
                    with patch.object(runner.subprocess, 'run') as execute:
                        self.assertEqual(runner.run(prepared['job'], ['synthetic']), 1)
                    execute.assert_not_called()
                    self.assertEqual(jobs.read(prepared['job']), before)

    def test_atomic_job_claim_allows_only_one_contender(self):
        prepared = backend.prepare_new('a', self.cwd)
        barrier = threading.Barrier(2)
        accepted, rejected, errors = [], [], []
        def claim():
            try:
                barrier.wait(5)
                accepted.append(jobs.claim(prepared['job']))
            except jobs.LaunchRejected:
                rejected.append(True)
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for worker in threads:
            worker.start()
        for worker in threads:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual((len(accepted), len(rejected)), (1, 1))
        self.assertEqual(jobs.read(prepared['job'])['attempts'], 1)

    def test_process_identity_detects_reuse_and_preserves_unknown_legacy_owner(self):
        prepared = backend.prepare_new('a', self.cwd)
        jobs.update(prepared['job'], state='running', pid=os.getpid(), pid_started='previous-instance')
        with patch.object(jobs, 'process_alive', return_value=True), \
                patch.object(jobs, 'process_started', return_value='new-instance'):
            self.assertFalse(jobs.process_matches(jobs.read(prepared['job'])))
            jobs.reconcile(identity=prepared['job'])
        self.assertEqual(jobs.read(prepared['job'])['state'], 'interrupted')
        with patch.object(jobs, 'process_alive', return_value=True), patch.object(jobs, 'process_started', return_value=None):
            self.assertTrue(jobs.process_matches(dict(pid=1234, pid_started='unknown-but-protected')))
            self.assertTrue(jobs.process_matches(dict(pid=1234)))

    def test_preparation_identity_reuse_is_reconciled(self):
        prepared = backend.prepare_new('a', self.cwd)
        jobs.update(prepared['job'], preparation_pid_started='old-preparer')
        with patch.object(jobs, 'process_alive', return_value=True), \
                patch.object(jobs, 'process_started', return_value='new-preparer'):
            jobs.reconcile(identity=prepared['job'])
        self.assertEqual(jobs.read(prepared['job'])['state'], 'interrupted')

    def test_stale_reconcile_does_not_overwrite_new_process_owner(self):
        prepared = backend.prepare_new('a', self.cwd)
        jobs.update(prepared['job'], state='running', pid=1234, pid_started='old-owner')
        def changed(item, field='pid'):
            jobs.update(prepared['job'], state='running', pid=os.getpid(), pid_started='new-owner')
            return False
        with patch.object(jobs, 'process_matches', side_effect=changed):
            jobs.reconcile(identity=prepared['job'])
        report = jobs.read(prepared['job'])
        self.assertEqual((report['state'], report['pid_started']), ('running', 'new-owner'))

    def test_current_process_identity_is_stable(self):
        first = jobs.process_started(os.getpid())
        self.assertIsNotNone(first)
        self.assertEqual(first, jobs.process_started(os.getpid()))
        self.assertTrue(jobs.process_matches(dict(pid=os.getpid(), pid_started=first)))

    def test_posix_identity_includes_boot_and_handles_process_name_spaces(self):
        proc = self.root / 'proc-identity'
        (proc / '1234').mkdir(parents=True)
        (proc / '1234/stat').write_text('1234 (worker name with spaces) S ' + '0 ' * 18 + '5678 0 0\n')
        boot = proc / 'sys/kernel/random/boot_id'
        boot.parent.mkdir(parents=True)
        boot.write_text('synthetic-boot')
        self.assertEqual(jobs.process_started_posix(1234, proc), 'synthetic-boot:5678')
        boot.write_text('another-boot')
        self.assertEqual(jobs.process_started_posix(1234, proc), 'another-boot:5678')
        self.assertIsNone(jobs.process_started_posix(9999, proc))

    def test_mcp_filesystem_failure_does_not_terminate_next_request(self):
        requests = [dict(id=1, method='tools/call', params=dict(name='list_sessions', arguments={})),
                    dict(id=2, method='ping')]
        input_ = type('Input', (), {'buffer': io.BytesIO(('\n'.join(json.dumps(r) for r in requests) + '\n').encode())})()
        output = io.StringIO()
        with patch.object(ah.sys, 'stdin', input_), patch.object(ah.sys, 'stdout', output), \
                patch.object(ah, 'mcp_call', side_effect=PermissionError('synthetic inaccessible source')):
            ah.serve_mcp()
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(replies[0]['result']['isError'])
        self.assertEqual((replies[1]['id'], replies[1]['result']), (2, {}))

    def test_mcp_closes_database_on_success_and_collection_failure(self):
        for fail in (False, True):
            db = ah.connect()
            with patch.object(ah, 'connect', return_value=db), \
                    patch.object(ah, 'collect', side_effect=PermissionError('synthetic') if fail else None):
                if fail:
                    with self.assertRaises(PermissionError):
                        ah.mcp_call('list_sessions', {})
                else:
                    ah.mcp_call('list_sessions', {})
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')
