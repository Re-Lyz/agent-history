"""Terminal lifecycle persists outside Tk without recording credentials or transcript text."""
import contextlib
import base64
import json
import subprocess
import sys
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_history as ah
import launch_backend as backend
import launch_job_runner as runner
import launch_jobs as jobs
from tests.test_trash import Fixture


class LaunchJobTests(Fixture):
    def test_successful_exit_retries_a_transient_native_lock_without_restarting_agent(self):
        import codex_import as ci
        prepared = backend.prepare_launch(self.db, dict(ah.resolve(self.db, 's1')), 'a', self.cwd)
        held = ci.writer_lock(self.a, 's1')
        def execute_command(*args, **kwargs):
            self.assertTrue(held.__enter__())
            return subprocess.CompletedProcess([], 0)
        def release(delay):
            with ah.collect_lock(wait=False) as free:
                self.assertTrue(free, 'Backoff must release the collector lock.')
            held.__exit__(None, None, None)
        try:
            with patch.object(runner.subprocess, 'run', side_effect=execute_command) as execute, \
                    patch.object(runner.time, 'sleep', side_effect=release) as pause:
                self.assertEqual(runner.run(prepared['job'], ['fixture']), 0)
            execute.assert_called_once()
            pause.assert_called_once_with(.25)
            self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'collected')
            self.assertTrue(self.path.is_file())
        finally:
            held.__exit__(None, None, None)

    def test_persistent_native_lock_has_bounded_retries_and_retains_reservation(self):
        import codex_import as ci
        prepared = backend.prepare_launch(self.db, dict(ah.resolve(self.db, 's1')), 'a', self.cwd)
        original = self.path.read_bytes()
        held = ci.writer_lock(self.a, 's1')
        def execute_command(*args, **kwargs):
            self.assertTrue(held.__enter__())
            return subprocess.CompletedProcess([], 0)
        try:
            with patch.object(runner.subprocess, 'run', side_effect=execute_command) as execute, \
                    patch.object(runner.time, 'sleep') as pause:
                self.assertEqual(runner.run(prepared['job'], ['fixture']), 0)
        finally:
            held.__exit__(None, None, None)
        self.assertEqual(pause.call_count, 3)
        execute.assert_called_once()
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'failed')
        self.assertIsNotNone(self.db.execute('SELECT job FROM conversation_runs WHERE job=?', (prepared['job'],)).fetchone())
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_exit_does_not_automatically_retry_native_lock(self):
        import single_conversation
        import handoff_lifecycle
        prepared = backend.prepare_launch(self.db, dict(ah.resolve(self.db, 's1')), 'a', self.cwd)
        with patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 12)), \
                patch.object(single_conversation, 'finish', side_effect=handoff_lifecycle.SessionBusyError('busy')) as finish, \
                patch.object(runner.time, 'sleep') as pause:
            self.assertEqual(runner.run(prepared['job'], ['fixture']), 12)
        finish.assert_called_once()
        pause.assert_not_called()
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'failed')

    def test_content_validation_failure_does_not_automatically_retry(self):
        import single_conversation
        prepared = backend.prepare_launch(self.db, dict(ah.resolve(self.db, 's1')), 'a', self.cwd)
        with patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(single_conversation, 'finish', side_effect=ValueError('independent branch')) as finish, \
                patch.object(runner.time, 'sleep') as pause:
            self.assertEqual(runner.run(prepared['job'], ['fixture']), 0)
        finish.assert_called_once()
        pause.assert_not_called()
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'failed')

    def controller(self, selected, identity):
        from history_app.ui.launch import create_mixin
        from history_app.history_queries import recovery_job
        desktop = SimpleNamespace(tk=None, ttk=None, messagebox=None, filedialog=None, palette={})
        controller = create_mixin(SimpleNamespace(recovery_job=recovery_job), desktop)()
        controller.selected = lambda: [selected]
        controller.launch_job = identity
        controller.launch_status = controller.busy = SimpleNamespace(set=lambda value: None)
        controller.after_reindex = lambda snapshot: None
        controller.reindex = lambda: self.fail('A completed targeted sync must not reindex all conversations again.')
        controller.background = lambda work, done, **kwargs: done(work())
        return controller

    def test_targeted_ui_retry_indexes_once_and_does_not_sync_unrelated_jobs(self):
        import conversation_store as store
        selected = dict(ah.resolve(self.db, 's1'))
        prepared = backend.prepare_launch(self.db, selected, 'a', self.cwd)
        jobs.update(prepared['job'], state='exited', exit_code=0)
        controller = self.controller(selected, prepared['job'])
        with patch.object(ah, 'collect', wraps=ah.collect) as collect, \
                patch.object(store, 'sync_all', side_effect=AssertionError('Unrelated full sync')):
            controller.retry_handoff()
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'collected')

    def test_ui_retry_still_discovers_new_sessions_without_conversation_reservations(self):
        from tests.test_trash import rollout
        from history_app.ui.workers import create_mixin
        prepared = backend.prepare_new('a', self.cwd)
        jobs.update(prepared['job'], state='interrupted')
        transcript = self.a / 'sessions' / 'new-after-interruption.jsonl'
        transcript.write_text(rollout('new-after-interruption', 'saved before interruption', self.cwd), encoding='utf-8')
        controller = self.controller(None, prepared['job'])
        workers = create_mixin(None, None)
        controller.reindex = lambda: workers.reindex(controller)
        controller.retry_handoff()
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'collected')
        self.assertEqual(ah.resolve(self.db, 'new-after-interruption')['turns'], 1)

    def test_completed_runner_report_refreshes_view_without_rescanning_history(self):
        selected = dict(ah.resolve(self.db, 's1'))
        prepared = backend.prepare_launch(self.db, selected, 'a', self.cwd)
        jobs.update(prepared['job'], state='exited', exit_code=0, sync_state='collected')
        controller = self.controller(selected, prepared['job'])
        controller.last_launch_report = None
        controller.root = SimpleNamespace(after=lambda *args: None)
        with patch.object(ah, 'collect', side_effect=AssertionError('Runner already indexed')):
            controller.poll_launch_report()
        self.assertEqual(controller.last_launch_report['sync_state'], 'collected')

    def test_ui_status_read_does_not_wait_for_report_writer(self):
        prepared = backend.prepare_launch(self.db, dict(ah.resolve(self.db, 's1')), 'a', self.cwd)
        with ah.file_lock(jobs.path(prepared['job']).with_suffix('.lock'), wait=False) as locked:
            self.assertTrue(locked)
            with self.assertRaises(TimeoutError):
                jobs.read(prepared['job'], wait=False)
        self.assertEqual(jobs.read(prepared['job'])['state'], 'prepared')

    def test_independent_new_session_runner_executes_synthetic_program_and_collects_after_exit(self):
        from tests.test_trash import rollout
        import launch_profiles
        script = self.root / 'new conversation writer.py'
        script.write_text('from pathlib import Path\nimport sys, base64\n'
                          'Path(sys.argv[1]).write_bytes(base64.b64decode(sys.argv[2]))\n', encoding='utf-8')
        transcript = self.a / 'sessions' / 'new-process.jsonl'
        launch_profiles.save([dict(id='writer', type='custom', program=sys.executable,
            args=[str(script), str(transcript), base64.b64encode(
                rollout('new-process', '独立进程生成的新对话', self.cwd).encode('utf-8')).decode('ascii')])])
        prepared = backend.prepare_new('profile:writer', self.cwd)
        payload = base64.b64encode(json.dumps(dict(job=prepared['job'], inner=prepared['inner'])).encode()).decode()
        environment = {**os.environ, **prepared['env'], 'HOME': str(self.root), 'USERPROFILE': str(self.root)}
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'launch_job_runner.py'), payload], env=environment,
            capture_output=True, timeout=25, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = jobs.read(prepared['job'])
        self.assertEqual((report['state'], report['sync_state']), ('exited', 'collected'))
        self.assertEqual(ah.resolve(self.db, 'new-process')['turns'], 1)
        self.assertTrue(transcript.is_file())

    def test_new_session_exit_collects_new_history_without_a_source_session(self):
        from tests.test_trash import rollout
        prepared = backend.prepare_new('a', self.cwd)
        transcript = self.a / 'sessions' / 'brand-new.jsonl'

        def execute(*args, **kwargs):
            transcript.write_text(rollout('brand-new', '新增对话的第一条消息', self.cwd), encoding='utf-8')
            return subprocess.CompletedProcess([], 0)

        with patch.object(runner.subprocess, 'run', side_effect=execute):
            self.assertEqual(runner.run(prepared['job'], ['fixture-new-command']), 0)
        report = jobs.read(prepared['job'])
        self.assertEqual((report['state'], report['sync_state']), ('exited', 'collected'))
        self.assertIn('新会话', jobs.describe(report))
        ah.collect(self.db)
        self.assertEqual(ah.resolve(self.db, 'brand-new')['turns'], 1)
        from history_ui import list_rows
        self.assertIn('brand-new', [row['id'] for row in list_rows(self.db)])
        self.assertFalse(jobs.collection_failed(report, dict(failed=[dict(path=str(self.b / 'other.jsonl'))])))
        self.assertTrue(jobs.collection_failed(report, dict(failed=[dict(path=str(transcript))])))

    def test_pythonw_startup_failure_writes_log_and_requests_error_dialog(self):
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        if os.name != 'nt' or not pythonw.is_file():
            self.skipTest('Windows pythonw required')
        script = self.root / 'startup-failure.py'
        dialog = self.root / 'dialog.txt'
        script.write_text('''import ctypes
from pathlib import Path
import runpy
import sys
import tkinter
from unittest.mock import patch
sys.path.insert(0, str(Path(sys.argv[1]).parent))
def record_dialog(*args):
    Path(sys.argv[2]).write_text(args[1], encoding='utf-8')
    return 1
with patch.object(tkinter, 'Tk', side_effect=tkinter.TclError('synthetic startup failure')), \\
        patch.object(ctypes.windll.user32, 'MessageBoxW', side_effect=record_dialog):
    runpy.run_path(sys.argv[1], run_name='__main__')
''', encoding='utf-8')
        env = {**os.environ, 'AGENT_HISTORY_HOME': str(ah.DATA), 'AGENT_HISTORY_SHARED_ROOT': str(ah.ROOT)}
        ui = Path(__file__).resolve().parents[1] / 'history_ui.py'
        result = subprocess.run([str(pythonw), str(script), str(ui), str(dialog)], env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        import ui_support
        log = ui_support.error_log_path()
        self.assertIn('synthetic startup failure', log.read_text(encoding='utf-8'))
        self.assertIn(str(log), dialog.read_text(encoding='utf-8'))

    def test_busy_collection_lock_is_reported_as_retryable_failure(self):
        row = dict(ah.resolve(self.db, 's1'))
        prepared = backend.prepare_launch(self.db, row, 'a', self.cwd)
        @contextlib.contextmanager
        def busy(**kwargs):
            yield False
        with patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(ah, 'collect_lock', busy):
            self.assertEqual(runner.run(prepared['job'], ['-Command', 'fixture']), 1)
        report = jobs.read(prepared['job'])
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(report['sync_state'], 'failed')
        self.assertIn('TimeoutError', report['error'])

    def test_real_terminal_runner_propagates_script_exit_code_without_agent(self):
        row = dict(ah.resolve(self.db, 's1'))
        prepared = backend.prepare_launch(self.db, row, 'a', self.cwd)
        script = self.root / 'fixture exit.ps1'
        script.write_text('exit 17\n', encoding='utf-8')
        command = ['-File', str(script)] if os.name == 'nt' else ['/bin/sh', str(script)]
        inner = runner.checked_inner(command) if os.name == 'nt' else command
        result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', *inner] if os.name == 'nt' else inner, capture_output=True)
        self.assertEqual(result.returncode, 17)
        # Exercise a separate Python runner process, as used after Tk exits.
        import base64
        payload = base64.b64encode(json.dumps(dict(job=prepared['job'], inner=command)).encode()).decode()
        env = {**os.environ, **prepared['env']}
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'launch_job_runner.py'), payload], env=env, capture_output=True)
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(jobs.read(prepared['job'])['exit_code'], 17)
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'collected')

    def test_terminal_exit_failed_retry_and_collect_reports(self):
        row = dict(ah.resolve(self.db, 's1'))
        prepared = backend.prepare_launch(self.db, row, 'a', self.cwd)
        identity = prepared['job']
        with patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 12)):
            code = runner.run(identity, ['-Command', 'fixture-never-run'])
        self.assertEqual(code, 12)
        failed = jobs.read(identity)
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['sync_state'], 'collected')
        self.assertIn('退出码 12', jobs.describe(failed))
        with patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            runner.run(identity, ['-Command', 'fixture-never-run'])
        self.assertEqual(jobs.read(identity)['attempts'], 2)
        self.assertEqual(jobs.read(identity)['state'], 'exited')
        self.assertNotIn('原始问题', jobs.path(identity).read_text(encoding='utf-8'))

    def test_readonly_test_launch_never_starts_or_imports(self):
        row = dict(ah.resolve(self.db, 's1'))
        import ui_support
        with patch.object(ui_support, 'launch_readiness', return_value=dict(ready=True, message='ready')), \
                patch.object(backend.subprocess, 'Popen', side_effect=AssertionError('No terminal')):
            report = backend.test_launch(self.db, row, 'b', self.cwd)
        self.assertTrue(report['ready'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], 0)
        self.assertEqual(jobs.reports(), [])

    def test_failed_spawn_is_reported_and_sync_failure_retry_is_visible(self):
        row = dict(ah.resolve(self.db, 's1'))
        prepared = backend.prepare_launch(self.db, row, 'a', self.cwd)
        import single_conversation
        with patch.object(runner.subprocess, 'run', side_effect=OSError('fixture')), \
                patch.object(single_conversation, 'finish', side_effect=ValueError('fixture')):
            runner.run(prepared['job'], ['-Command', 'fixture'])
        report = jobs.read(prepared['job'])
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(report['sync_state'], 'failed')
        self.assertIn('可重试', jobs.describe(report))

    def test_interrupted_runner_is_reconciled_and_refresh_can_retry_collection(self):
        prepared = backend.prepare_launch(self.db, dict(ah.resolve(self.db, 's1')), 'a', self.cwd)
        jobs.update(prepared['job'], state='running', pid=123456)
        with patch.object(jobs, 'process_alive', return_value=False):
            jobs.reconcile()
        self.assertEqual(jobs.read(prepared['job'])['state'], 'interrupted')
        import handoff_lifecycle
        handoff_lifecycle.retry_pending(self.db)
        jobs.mark_collected(dict(failed=[]))
        self.assertEqual(jobs.read(prepared['job'])['sync_state'], 'collected')
