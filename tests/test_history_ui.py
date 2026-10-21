"""Exercise viewer interactions in an invisible window with synthetic local history."""
import contextlib
import datetime as dt
import json
import itertools
import os
from pathlib import Path
import tempfile
import threading
import time
import sys
import unittest
from unittest.mock import patch

import agent_history as ah
import history_ui as ui
import trash
import ui_support as support
import markdown_preview as md


class ViewerInteractionTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
            from tkinter import ttk, messagebox
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.attributes('-alpha', 0.0)
            self.root.overrideredirect(True)
        except ImportError as error:
            self.skipTest('Tk desktop unavailable: ' + str(error))
        except tk.TclError as error:
            self.skipTest('Tk desktop unavailable: ' + str(error))
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name).resolve()
        from tests.platform_fixture import programs
        programs(self, self.path)
        self.shared = self.path / 'shared'
        self.shared.mkdir()
        self.data = self.path / 'data'
        for name in ('a', 'b'):
            (self.path / name / 'sessions').mkdir(parents=True)
        (self.shared / 'connections.json').write_text(json.dumps({'connections': {
            'a': {'home': '../a'}, 'b': {'home': '../b'}}}))
        self.patches = [patch.object(ah, 'ROOT', self.shared), patch.object(ah, 'DATA', self.data),
                        patch.object(ah, 'CLAUDE_HOMES', [])]
        for item in self.patches:
            item.start()
        for sid in ('one', 'two'):
            records = [{'type': 'session_meta', 'payload': {'id': sid, 'cwd': str(self.path), 'source': 'cli'}}]
            for index in range(450 if sid == 'one' else 1):
                records.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': f'原始问题 {sid} {index}\n更多内容\n'}]}})
            (self.path / 'a' / 'sessions' / (sid + '.jsonl')).write_text(
                ''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records), encoding='utf-8')
        with contextlib.closing(ah.connect()) as db:
            ah.collect(db)
        self.workers = []
        thread_type = threading.Thread

        def thread(*args, **kwargs):
            worker = thread_type(*args, **kwargs)
            self.workers.append(worker)
            return worker

        self.thread_patch = patch.object(ui.threading, 'Thread', side_effect=thread)
        self.thread_patch.start()

    def tearDown(self):
        with contextlib.suppress(self.tk.TclError):
            for callback in self.root.tk.call('after', 'info'):
                self.root.tk.call('after', 'cancel', callback)
            self.root.destroy()
        # A failed assertion can leave a restore/index worker holding collect.lock.
        # Drain it before unpatching its temporary Agent homes or removing files.
        deadline = time.monotonic() + 30
        for worker in self.workers:
            if worker.ident is not None:
                worker.join(max(0, deadline - time.monotonic()))
        self.thread_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        if any(worker.is_alive() for worker in self.workers):
            self.fail('Viewer worker did not stop; temporary files retained for diagnosis.')
        self.temp.cleanup()

    def widgets(self):
        found, pending = [], [self.root]
        while pending:
            widget = pending.pop()
            found.append(widget)
            pending.extend(widget.winfo_children())
        return found

    def button(self, text):
        return next(w for w in self.widgets() if isinstance(w, self.ttk.Button) and w.cget('text') == text)

    def target_widget(self):
        return next(w for w in self.widgets() if isinstance(w, self.ttk.Menubutton) and w.winfo_name() == 'launch_target')

    def target_value(self):
        return self.root.getvar(self.target_widget().cget('textvariable'))

    def choose_target(self, command):
        menu = self.root.nametowidget(self.target_widget().cget('menu'))
        for index in range(menu.index('end') + 1):
            if menu.entrycget(index, 'label').startswith(command + ' ·'):
                self.assertEqual(menu.entrycget(index, 'state'), 'normal')
                menu.invoke(index)
                return
        self.fail('Missing launch command: ' + command)

    def statuses(self):
        return [str(self.root.getvar(w.cget('textvariable'))) for w in self.widgets()
                if isinstance(w, self.ttk.Label) and w.cget('textvariable')] + [
                    w.get('1.0', 'end-1c') for w in self.widgets() if isinstance(w, ui.SelectableText)]

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail('Viewer operation did not finish: ' + repr(self.statuses()))

    def wait_layout(self):
        # Startup restores sashes asynchronously; resize assertions must begin
        # after those changes and the resulting deferred layout have settled.
        self.wait(lambda: not any(
            name in str(self.root.tk.call('after', 'info', job))
            for job in self.root.tk.call('after', 'info')
            for name in ('place_sashes', 'finish', 'sync_region', '_fit_height')))

    def wait_reader(self):
        # update() can return between the viewer's scheduled rendering batches.
        # Wait for completion and startup geometry before reading or scrolling.
        self.wait(lambda: not any(
            'batch' in str(self.root.tk.call('after', 'info', job))
            for job in self.root.tk.call('after', 'info')), timeout=30)
        self.wait_layout()

    def exercise(self, interact, expect_launch=False):
        errors = []
        ready = dict(ready=True, issues=[], message='fixture ready')
        with patch.object(self.tk, 'Tk', return_value=self.root), \
                patch.object(type(self.root), 'mainloop', interact), \
                patch.object(support, 'launch_readiness', return_value=ready), \
                patch.object(ui, 'start_terminal') as start, \
                patch.object(self.messagebox, 'showerror', side_effect=lambda *args: errors.append(args)):
            ui.run_ui()
        self.assertEqual(errors, [])
        if expect_launch:
            self.assertEqual(start.call_count, int(expect_launch))
        else:
            start.assert_not_called()
        return start

    @contextlib.contextmanager
    def new_dialog_fixture(self, readiness=None):
        dialogs = []
        original = ui.new_conversation_ui.NewConversationDialog

        def create(*args, **kwargs):
            dialog = original(*args, **kwargs)
            dialog.window.attributes('-alpha', 0.0)
            dialog.window.overrideredirect(True)
            dialogs.append(dialog)
            return dialog

        ready = dict(ready=True, message='fixture ready', issues=[])
        with patch.object(ui.new_conversation_ui, 'NewConversationDialog', side_effect=create), \
                patch.object(ui.launcher, 'new_readiness', side_effect=readiness or (lambda target: ready)):
            yield dialogs

    def test_new_conversation_works_without_history_and_starts_selected_builtin_once(self):
        for path in (self.path / 'a' / 'sessions').glob('*.jsonl'):
            path.unlink()
        with contextlib.closing(ah.connect()) as db:
            db.executescript('DELETE FROM messages; DELETE FROM files; DELETE FROM sessions;')
        with self.new_dialog_fixture() as dialogs, patch.object(ui.settings_ui, 'SetupWizard') as setup:
            def interact(root):
                self.wait(lambda: setup.called)
                self.button('新增对话').invoke()
                dialog = dialogs[-1]
                self.button('新增对话').invoke()
                self.assertEqual(len(dialogs), 1)
                dialog.cwd.set(str(self.path))
                dialog.menu.invoke(list(dialog.options).index('claude'))
                self.assertEqual(dialog.target, 'claude')
                self.assertEqual(dialog.plan['args'], [])
                dialog.start_button.invoke()
                dialog.start()
                self.wait(lambda: not dialog.alive())
                report = ui.launch_jobs.reports()[0]
                self.assertEqual((report['target'], report['kind'], report['source_id']), ('claude', 'new', ''))
                self.assertEqual(report['cwd'], str(self.path))
                root.tk.call(root.protocol('WM_DELETE_WINDOW'))
            start = self.exercise(interact, expect_launch=True)
        self.assertTrue(start.call_args.kwargs['env']['AGENT_HISTORY_JOB_ID'])
        self.assertEqual(support.load_preferences()['default_target'], 'claude')

    def test_new_only_profile_preview_directory_validation_and_cancel_are_read_only(self):
        ui.launcher.profiles.save([dict(id='new-only', name='仅新会话', type='custom', program=sys.executable,
                                       args=['--version'])])
        ready = dict(ready=True, message='fixture ready', issues=[])
        def readiness(key):
            return dict(ready=False, message='未找到程序', issues=['missing']) if key == 'b' else ready
        with self.new_dialog_fixture(readiness) as dialogs:
            def interact(root):
                self.wait(lambda: len(next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview)
                                          and 'check' in w.cget('columns')).get_children()) == 2)
                self.button('新增对话').invoke()
                dialog = dialogs[-1]
                self.assertEqual(dialog.menu.entrycget(list(dialog.options).index('b'), 'state'), 'disabled')
                dialog.menu.invoke(list(dialog.options).index('profile:new-only'))
                dialog.cwd.set(str(self.path / 'missing'))
                self.assertIsNone(dialog.plan)
                self.assertTrue(dialog.start_button.instate(['disabled']))
                self.assertIn('工作目录不存在', dialog.message.get())
                dialog.cwd.set(str(self.path))
                self.assertEqual(dialog.plan['args'], ['--version'])
                self.assertNotIn('--resume', dialog.plan['preview'])
                dialog.copy_button.invoke()
                self.assertEqual(root.clipboard_get(), dialog.plan['preview'])
                root.tk.call(dialog.window.protocol('WM_DELETE_WINDOW'))
                self.assertEqual(ui.launch_jobs.reports(), [])
                root.tk.call(root.protocol('WM_DELETE_WINDOW'))
            self.exercise(interact)

    def test_cancel_new_conversation_during_preparation_does_not_open_terminal(self):
        started, release = threading.Event(), threading.Event()
        original = ui.launcher.prepare_new
        def prepare(*args):
            result = original(*args)
            started.set()
            if not release.wait(5):
                raise ValueError('fixture timeout')
            return result
        with self.new_dialog_fixture() as dialogs, patch.object(ui.launcher, 'prepare_new', side_effect=prepare):
            def interact(root):
                self.button('新增对话').invoke()
                dialog = dialogs[-1]
                dialog.cwd.set(str(self.path))
                dialog.start_button.invoke()
                self.wait(started.is_set)
                dialog.close()
                release.set()
                self.wait(lambda: ui.launch_jobs.reports()[0]['state'] == 'cancelled')
                root.tk.call(root.protocol('WM_DELETE_WINDOW'))
            try:
                self.exercise(interact)
            finally:
                release.set()

    def test_failed_new_terminal_is_reported_and_can_be_retried(self):
        with self.new_dialog_fixture() as dialogs:
            def interact(root):
                self.button('新增对话').invoke()
                dialog = dialogs[-1]
                dialog.cwd.set(str(self.path))
                ui.start_terminal.side_effect = [OSError('fixture terminal missing'), None]
                dialog.start_button.invoke()
                self.wait(lambda: not dialog.launching)
                self.assertTrue(dialog.alive())
                self.assertIn('fixture terminal missing', dialog.message.get())
                self.assertEqual(ui.launch_jobs.reports()[0]['state'], 'failed')
                dialog.start_button.invoke()
                self.wait(lambda: not dialog.alive())
                root.tk.call(root.protocol('WM_DELETE_WINDOW'))
            self.exercise(interact, expect_launch=2)

    def test_close_cancels_native_tcl_and_python_timers_and_destroys_window(self):
        fired = []

        def interact(root):
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            # Tk itself schedules Tcl list scripts; these are not Python command names.
            native = root.tk.call('after', 60_000, ('set', '::history_close_timer', 'fired'))
            script = root.tk.splitlist(root.tk.call('after', 'info', native))[0]
            self.assertEqual(tuple(root.tk.splitlist(script)), ('set', '::history_close_timer', 'fired'))
            root.after(60_000, lambda: fired.append('python'))
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
            self.assertFalse(root.tk.call('info', 'commands', '.'))
            self.assertFalse(root.tk.call('after', 'info'))

        self.exercise(interact)
        self.assertEqual(fired, [])
        self.assertTrue((self.data / 'ui-preferences.json').is_file())

    def test_column_header_click_sorts_toggles_and_preserves_selection_checks_and_preferences(self):
        def interact(root):
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            iid = next(item for item in tree.get_children() if 'one.jsonl' in item)
            tree.selection_set(iid)
            root.update()
            self.button('全选').invoke()
            root.tk.call(tree.heading('turns')['command'])
            root.update()
            self.assertIn('two.jsonl', tree.get_children()[0])
            self.assertEqual(tree.heading('turns')['text'], '轮 ▲')
            self.assertEqual(tree.selection(), (iid,))
            self.assertTrue(all(tree.set(item, 'check') == ui.CHECK_ON for item in tree.get_children()))
            root.tk.call(tree.heading('turns')['command'])
            root.update()
            self.assertIn('one.jsonl', tree.get_children()[0])
            self.assertEqual(tree.heading('turns')['text'], '轮 ▼')
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)
        self.assertEqual(support.load_preferences()['sort'], dict(column='turns', descending=True))

    def test_claude_to_codex_ui_prepares_native_id_before_opening_terminal(self):
        from tests.test_codex_import import native_db
        claude_home = self.path / 'claude'
        with patch.object(ah, 'CLAUDE_HOMES', [claude_home]):
            source = ah.claude_project_dir(str(self.path)) / 'claude-ui.jsonl'
            source.parent.mkdir(parents=True)
            records = [dict(type=role, sessionId='claude-ui', cwd=str(self.path),
                            timestamp='2026-10-01T00:00:00Z', message=dict(content=value))
                       for role, value in [('user', 'Claude 来源中文问题'), ('assistant', 'Claude 来源完整回答')]]
            source.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
            original = source.read_bytes()
            for name in ('a', 'b'):
                native_db(self.path / name)
                (self.path / name / 'profile.toml').write_text('model="fixture-model"\nmodel_provider="openai"\n')
            def interact(root):
                tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
                self.wait(lambda: len(tree.get_children()) == 3)
                iid = next(item for item in tree.get_children() if 'claude-ui.jsonl' in item)
                tree.selection_set(iid)
                root.update()
                self.choose_target('codexc b')
                self.assertTrue(any('保留最新主会话' in value for value in self.statuses()))
                self.button('打开终端并继续').invoke()
                self.wait(lambda: '终端已打开；请在终端确认恢复结果' in self.statuses())
                root.tk.call(root.protocol('WM_DELETE_WINDOW'))
            import handoff_lifecycle
            with patch.object(handoff_lifecycle, 'claude_running', return_value=False):
                start = self.exercise(interact, expect_launch=True)
            _, args = start.call_args.args
            sid = args[args.index('-Session' if os.name == 'nt' else 'resume') + 1]
            self.assertNotEqual(sid, 'claude-ui')
            self.assertEqual(source.read_bytes(), original)
            with contextlib.closing(ah.connect()) as db:
                imported = ah.resolve(db, 'codex:' + sid)
                self.assertEqual(imported['imported_from'], 'claude:claude-ui')
                self.assertIn('Claude 来源完整回答', Path(imported['path']).read_text(encoding='utf-8'))

    def test_sort_reuses_loaded_rows_and_selected_messages(self):
        def interact(root):
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            tree.selection_set(tree.get_children()[0])
            root.update()
            with patch.object(ui, 'list_rows', side_effect=AssertionError('sort rescanned histories')), \
                    patch.object(ui, 'file_messages', side_effect=AssertionError('sort reread preview')):
                root.tk.call(tree.heading('turns')['command'])
                root.update()
                root.tk.call(tree.heading('title')['command'])
                root.update()
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)

    def test_large_preview_loads_off_thread_and_discards_stale_selection(self):
        path = self.path / 'a' / 'sessions' / 'one.jsonl'
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(type='diagnostic', padding='x' * 1_100_000)) + '\n')
        started, release = threading.Event(), threading.Event()
        actual = ui.file_messages
        main_thread = threading.get_ident()
        def read(row):
            if row['id'] == 'one':
                self.assertNotEqual(threading.get_ident(), main_thread)
                started.set()
                release.wait(5)
            return actual(row)
        def interact(root):
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            one = next(i for i in tree.get_children() if 'one.jsonl' in i)
            two = next(i for i in tree.get_children() if 'two.jsonl' in i)
            tree.selection_set(one)
            self.wait(started.is_set)
            tree.selection_set(two)
            root.update()
            release.set()
            self.wait(lambda: not any('读取对话' in status for status in self.statuses()))
            self.assertEqual(tree.selection(), (two,))
            self.assertTrue(any('two.jsonl' in status for status in self.statuses()))
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        with patch.object(ui, 'file_messages', side_effect=read):
            try:
                self.exercise(interact)
            finally:
                release.set()

    def test_list_refresh_stays_responsive_and_coalesces_filter_changes(self):
        actual = ui.list_rows
        started, release = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        calls = []

        def slow(db, *args, **kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            calls.append(args[0] if args else '')
            if len(calls) == 1:
                started.set()
                release.wait(5)
            return actual(db, *args, **kwargs)

        def interact(root):
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            source = next(w for w in self.widgets() if isinstance(w, self.ttk.Combobox)
                          and '全部来源' in w.cget('values'))
            with patch.object(ui, 'list_rows', side_effect=slow):
                source.set('b')
                source.event_generate('<<ComboboxSelected>>')
                self.wait(started.is_set)
                heartbeat = []
                root.after(1, lambda: heartbeat.append(True))
                self.wait(lambda: heartbeat)
                for choice in ('a', 'b', 'a'):
                    source.set(choice)
                    source.event_generate('<<ComboboxSelected>>')
                    root.update()
                self.assertEqual(len(calls), 1)
                release.set()
                self.wait(lambda: len(calls) == 2 and not any('正在读取历史' in s for s in self.statuses()))
                self.assertEqual(calls, ['b', 'a'])
                self.assertEqual(len(tree.get_children()), 2)
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))

        try:
            self.exercise(interact)
        finally:
            release.set()

    def test_stale_reservation_disables_launch_with_recovery_instruction(self):
        import launch_jobs
        with contextlib.closing(ah.connect()) as db:
            row = dict(ah.resolve(db, 'one'))
            prepared = ui.launcher.prepare_launch(db, row, 'a', str(self.path))
            launch_jobs.update(prepared['job'], state='interrupted', sync_state='failed')

        def interact(root):
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            one = next(i for i in tree.get_children() if 'one.jsonl' in i)
            tree.selection_set(one)
            self.wait(lambda: any('请点击“重试同步' in s for s in self.statuses()))
            self.assertTrue(self.button('打开终端并继续').instate(['disabled']))
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))

        # Startup's automatic same-home collection could otherwise settle it.
        with patch('handoff_lifecycle.retry_pending', return_value=dict(completed=0, pending=1)):
            self.exercise(interact)

    def test_close_still_destroys_window_when_preferences_cannot_be_saved(self):
        def interact(root):
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
            self.assertFalse(root.tk.call('info', 'commands', '.'))
            self.assertFalse(root.tk.call('after', 'info'))

        with patch.object(support, 'save_preferences', side_effect=PermissionError('fixture save denied')):
            self.exercise(interact)
        self.assertIn('fixture save denied', support.error_log_path().read_text(encoding='utf-8'))

    @unittest.skipUnless(md.available(), 'Install requirements-ui.txt for Markdown UI verification.')
    def test_markdown_raw_switch_search_copy_and_link_actions_preserve_history(self):
        path = self.path / 'a' / 'sessions' / 'two.jsonl'
        code = 'def hello():\n    return "中文答案"\n'
        source = '# 标题 **粗体**\n\n- 项目一\n- *项目二*\n\n> 引用内容\n\n' \
                 '| 中文列 | 数值 |\n| :--- | ---: |\n| 表格内容 | 42 |\n\n' \
                 '[点击官网](https://example.com)\n\n![图](https://example.com/image.png)\n\n' \
                 '<script>alert(1)</script>\n\n```python\n' + code + '```\n'
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        records[1]['payload']['content'][0]['text'] = source
        path.write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records), encoding='utf-8')
        original = path.read_bytes()

        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            iid = next(item for item in tree.get_children() if 'two.jsonl' in item)
            tree.selection_set(iid)
            root.update()
            reader = next(w for w in self.widgets() if isinstance(w, self.tk.Text) and 'body' in w.tag_names())
            mode = next(w for w in self.widgets() if w.winfo_name() == 'preview_mode')
            self.assertEqual(mode.get(), 'Markdown')
            rendered = reader.get('1.0', 'end')
            self.assertIn('标题 粗体', rendered)
            self.assertNotIn('**粗体**', rendered)
            self.assertIn('• 项目一', rendered)
            self.assertIn('表格内容', rendered)
            self.assertIn('<script>alert(1)</script>', rendered)
            self.assertIn('图片：图 · 未加载', rendered)
            self.assertTrue(reader.tag_ranges('md_code'))
            self.assertTrue(reader.tag_ranges('md_table_header'))
            self.assertTrue(reader.tag_ranges('md_syntax_keyword'))
            self.button('复制代码').invoke()
            self.assertEqual(root.clipboard_get(), code)
            find = next(w for w in self.widgets() if w.winfo_name() == 'conversation_find')
            find.insert(0, '中文答案')
            self.button('下一处').invoke()
            self.assertTrue(reader.tag_ranges('find_current'))
            # Parse/render alone must not open any resource; only clicking a link does.
            browser.assert_not_called()
            link = next(tag for tag in reader.tag_names() if tag.startswith('md_href_'))
            start = reader.tag_ranges(link)[0]
            reader.see(start)
            root.update()
            box = reader.bbox(start)
            self.assertIsNotNone(box)
            reader.event_generate('<Motion>', x=box[0] + 2, y=box[1] + 2)
            reader.event_generate('<Button-1>', x=box[0] + 2, y=box[1] + 2)
            root.update()
            browser.assert_called_once_with('https://example.com')
            mode.set('原文')
            mode.event_generate('<<ComboboxSelected>>')
            root.update()
            self.assertIn(source.rstrip(), reader.get('1.0', 'end'))
            self.assertFalse(any(isinstance(w, self.ttk.Button) and w.cget('text') == '复制代码' for w in self.widgets()))
            self.assertEqual(tree.selection(), (iid,))
            self.assertTrue(reader.tag_ranges('find'))
            mode.set('Markdown')
            mode.event_generate('<<ComboboxSelected>>')
            root.update()
            self.assertEqual(len([w for w in self.widgets() if isinstance(w, self.ttk.Button) and
                                 w.cget('text') == '复制代码']), 1)
            self.assertEqual(path.read_bytes(), original)
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        with patch.object(ui.webbrowser, 'open') as browser, patch('urllib.request.urlopen') as fetch:
            self.exercise(interact)
            fetch.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(support.load_preferences()['preview_mode'], 'markdown')

    def test_missing_markdown_dependencies_fall_back_to_raw_text(self):
        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            tree.selection_set(next(item for item in tree.get_children() if 'two.jsonl' in item))
            root.update()
            mode = next(w for w in self.widgets() if w.winfo_name() == 'preview_mode')
            self.assertEqual(mode.get(), '原文')
            self.assertEqual(mode.cget('values'), ('原文',))
            reader = next(w for w in self.widgets() if isinstance(w, self.tk.Text) and 'body' in w.tag_names())
            self.assertIn('原始问题 two', reader.get('1.0', 'end'))
            self.assertTrue(any('依赖未安装' in status for status in self.statuses()))
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        with patch.object(md, 'available', return_value=False):
            self.exercise(interact)

    def test_maintenance_window_exports_redacted_report_and_restores_preferences_without_overwriting(self):
        from history_app.ui import maintenance
        windows = []
        original = maintenance.MaintenanceWindow
        def create(*args, **kwargs):
            window = original(*args, **kwargs)
            windows.append(window)
            return window
        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            self.button('诊断与维护').invoke()
            self.wait(lambda: windows and not windows[0].running)
            dialog = windows[0]
            dialog.prefix.set(str(self.path / 'synthetic-install'))
            self.button('诊断与维护').invoke()
            self.assertEqual(len(windows), 1)
            dialog.diagnose()
            self.wait(lambda: dialog.report is not None and not dialog.running)
            self.assertEqual(json.loads(dialog.outputs['health'].get('1.0', 'end'))['local_paths']['shared'], str(self.shared))
            dialog.copy_report()
            exported = json.loads(root.clipboard_get())
            self.assertNotIn('local_paths', exported)
            self.assertNotIn(str(self.path), root.clipboard_get())
            dialog.backup()
            self.wait(lambda: not dialog.running and bool(dialog.backup_picker['values']))
            preferences = self.data / 'ui-preferences.json'
            preferences.write_text(json.dumps(dict(version=1, default_target='b')), encoding='utf-8')
            dialog.preview_restore()
            self.wait(lambda: not dialog.running and dialog.plan is not None)
            self.assertFalse(dialog.restore_button.instate(['disabled']))
            dialog.restore()
            self.wait(lambda: not dialog.running and dialog.plan is None)
            self.assertFalse(preferences.exists())
            dialog.running = True
            dialog.close()
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
            self.assertTrue(root.winfo_exists())
            dialog.running = False
            dialog.close()
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        with patch.object(maintenance, 'MaintenanceWindow', side_effect=create):
            self.exercise(interact)
        self.assertFalse((self.data / 'ui-preferences.json').exists())

    def test_unclaimed_launch_can_be_cancelled_from_selected_conversation_without_running_agent(self):
        prepared_jobs = []
        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            iid = next(item for item in tree.get_children() if 'two.jsonl' in item)
            tree.selection_set(iid)
            root.update()
            with contextlib.closing(ah.connect()) as db:
                prepared = ui.launcher.prepare_launch(db, dict(ah.resolve(db, 'two')), 'a', str(self.path))
                prepared_jobs.append(prepared['job'])
            self.button('取消未接管启动').invoke()
            self.wait(lambda: ui.launch_jobs.read(prepared['job'])['state'] == 'cancelled')
            self.assertTrue((self.path / 'a/sessions/two.jsonl').is_file())
            self.wait(lambda: any('已取消未接管启动' in text for text in self.statuses()))
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)
        self.assertEqual(ui.launch_jobs.read(prepared_jobs[0])['state'], 'cancelled')

    @unittest.skipUnless(md.available(), 'Install requirements-ui.txt for Markdown UI verification.')
    def test_clean_titles_local_links_compact_paragraphs_and_tool_rows(self):
        import conversation_store
        document = self.path / 'docs' / 'local note.md'
        document.parent.mkdir()
        document.write_text('Synthetic local documentation', encoding='utf-8')
        executable = self.path / 'inert-fixture.exe'
        executable.write_bytes(b'not an executable; synthetic link target')
        source = 'First paragraph.\n\nSecond paragraph.\n\n' \
                 '[本地说明](<' + document.as_uri() + ':12>)\n\n[相对说明](<docs/local note.md>)'
        path = self.path / 'a' / 'sessions' / 'two.jsonl'
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        records[1]['payload']['content'][0]['text'] = '简洁会话标题'
        records.extend([
            dict(type='turn_context', payload=dict(model='synthetic-model')),
            dict(type='response_item', payload=dict(type='message', role='assistant',
                 content=[dict(type='output_text', text=source)])),
            dict(type='response_item', payload=dict(type='function_call', name='synthetic_tool',
                 call_id='local-demo', arguments='{}')),
            dict(type='response_item', payload=dict(type='function_call_output', call_id='local-demo', output='fixture result'))])
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records), encoding='utf-8')
        original_bytes = path.read_bytes()
        with contextlib.closing(ah.connect()) as db:
            ah.collect(db)
            conversation_store.enroll(db, dict(ah.resolve(db, 'two')))
        panels = []
        panel_type = ui.ToolPanel
        def panel(*args, **kwargs):
            value = panel_type(*args, **kwargs)
            panels.append(value)
            return value
        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            iid = next(item for item in tree.get_children() if 'two.jsonl' in item)
            self.assertEqual(tree.set(iid, 'title'), '简洁会话标题')
            tree.selection_set(iid)
            self.wait(lambda: panels)
            app = panels[0].app
            self.assertIn('模型：synthetic-model', app.info.get())
            self.assertIn('版本：1 个 · 分支：1 个', app.info.get())
            self.assertIn('First paragraph.\nSecond paragraph.', app.text.get('1.0', 'end'))
            self.assertEqual(app.text.tag_cget('tool', 'background'), '')
            self.assertFalse(panels[0].expanded)
            browser.assert_not_called()
            opener.assert_not_called()
            links = [tag for tag in app.text.tag_names() if tag.startswith('md_href_')]
            self.assertEqual(len(links), 2)
            start = app.text.tag_ranges(links[0])[0]
            app.text.see(start)
            root.update()
            box = app.text.bbox(start)
            self.assertIsNotNone(box)
            app.text.event_generate('<Motion>', x=box[0] + 2, y=box[1] + 2)
            app.text.event_generate('<Button-1>', x=box[0] + 2, y=box[1] + 2)
            root.update()
            self.assertEqual(opener.call_count, 1)
            self.assertEqual(opener.call_args.args[0][-1], str(document if os.name == 'nt' else document.parent))
            app.open_link('docs/local%20note.md:4')
            self.assertEqual(opener.call_count, 2)
            app.open_link(executable.as_uri())
            self.assertEqual(opener.call_args.args[0][0], 'explorer.exe' if os.name == 'nt' else 'xdg-open')
            before = opener.call_count
            app.open_link('docs/missing-file.md')
            self.assertEqual(opener.call_count, before)
            self.assertIn('本地路径不存在', app.render_info.get())
            mode = next(w for w in self.widgets() if w.winfo_name() == 'preview_mode')
            mode.set('原文')
            mode.event_generate('<<ComboboxSelected>>')
            root.update()
            self.assertIn(source, app.text.get('1.0', 'end'))
            self.assertEqual(path.read_bytes(), original_bytes)
            browser.assert_not_called()
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        with patch.object(ui, 'ToolPanel', side_effect=panel), patch.object(ui.subprocess, 'Popen') as opener, \
                patch.object(ui.webbrowser, 'open') as browser:
            self.exercise(interact)

    @unittest.skipUnless(md.available(), 'Install requirements-ui.txt for Markdown UI verification.')
    def test_markdown_long_code_unclosed_fence_and_prepending_preserve_reader(self):
        path = self.path / 'a' / 'sessions' / 'one.jsonl'
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        # The oldest message is outside the initial 400-message page.
        records[1]['payload']['content'][0]['text'] = '```python\n# 早期代码\nx = 1\n```'
        records[451 - 2]['payload']['content'][0]['text'] = '```python\nvalue = "' + '中文' * 800 + '"\n'  # no closing fence
        records[-1]['payload']['content'][0]['text'] = '# 后一条标题\n\n正常文字\n\n| A | B |\n|---|---|\n| 中文 | ' + '长内容' * 60 + ' |'
        path.write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records), encoding='utf-8')

        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            tree.selection_set(next(item for item in tree.get_children() if 'one.jsonl' in item))
            self.wait_reader()
            reader = next(w for w in self.widgets() if isinstance(w, self.tk.Text) and 'body' in w.tag_names())
            rendered = reader.get('1.0', 'end')
            self.assertIn('后一条标题', rendered)
            self.assertNotIn('# 后一条标题', rendered)
            self.assertIn('正常文字', rendered)
            self.assertLess(reader.xview()[1] - reader.xview()[0], 1.0)
            self.assertEqual(len([w for w in self.widgets() if isinstance(w, self.ttk.Button) and
                                 w.cget('text') == '复制代码']), 1)
            reader.yview_moveto(0.5)
            self.wait_reader()
            anchor = reader.index('@0,0')
            visible = reader.get(anchor, f'{anchor}+30c')
            self.button('加载更早消息').invoke()
            self.wait_reader()
            anchor = reader.index('@0,0')
            self.assertEqual(reader.get(anchor, f'{anchor}+30c'), visible)
            self.assertIn('早期代码', reader.get('1.0', 'end'))
            self.assertEqual(len([w for w in self.widgets() if isinstance(w, self.ttk.Button) and
                                 w.cget('text') == '复制代码']), 2)
            mode = next(w for w in self.widgets() if w.winfo_name() == 'preview_mode')
            mode.set('原文')
            mode.event_generate('<<ComboboxSelected>>')
            self.wait_reader()
            self.assertIn('```python', reader.get('1.0', 'end'))
            # Switching format keeps the reader in the same message, not at the end.
            self.assertLess(reader.yview()[0], 0.9)
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        # Force a yield after every render step, even on fast local machines.
        with patch.object(ui.time, 'perf_counter', side_effect=itertools.count(0, 0.009)):
            self.exercise(interact)

    def test_reader_search_expands_older_messages_and_paging_preserves_selection(self):
        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            iid = next(item for item in tree.get_children() if 'one.jsonl' in item)
            tree.selection_set(iid)
            self.wait_reader()
            reader = next(w for w in self.widgets() if isinstance(w, self.tk.Text) and 'body' in w.tag_names())
            self.assertNotIn('原始问题 one 0\n', reader.get('1.0', 'end'))
            self.assertTrue(any('400 / 450' in status for status in self.statuses()))
            reader.yview_moveto(0.4)
            self.wait_reader()
            anchor = reader.index('@0,0')
            visible = reader.get(anchor, f'{anchor}+30c')
            self.button('加载更早消息').invoke()
            self.wait_reader()
            anchor = reader.index('@0,0')
            self.assertEqual(reader.get(anchor, f'{anchor}+30c'), visible)
            self.assertIn('原始问题 one 0\n', reader.get('1.0', 'end'))
            self.assertTrue(self.button('加载更早消息').instate(['disabled']))
            # Reselecting a different transcript resets the page size.
            tree.selection_set(next(item for item in tree.get_children() if 'two.jsonl' in item))
            self.wait_reader()
            tree.selection_set(iid)
            self.wait_reader()
            root.event_generate('<Control-f>')
            self.wait_reader()
            find = next(w for w in self.widgets() if w.winfo_name() == 'conversation_find')
            find.insert(0, '原始问题 one 0\n')
            self.wait_reader()
            self.assertIn('原始问题 one 0\n', reader.get('1.0', 'end'))
            self.assertEqual(len(reader.tag_ranges('find')), 2)
            self.button('下一处').invoke()
            self.assertEqual(len(reader.tag_ranges('find_current')), 2)
            self.button('上一处').invoke()
            self.assertEqual(tree.selection(), (iid,))
            find.delete(0, 'end')
            self.assertEqual(reader.tag_ranges('find'), ())
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        # Force a yield after every render step, even on fast local machines.
        with patch.object(ui.time, 'perf_counter', side_effect=itertools.count(0, 0.009)):
            self.exercise(interact)

    def test_more_rows_reaches_older_transcripts_and_filters_reset_page(self):
        # Real complete files survive the startup collector and exercise its list boundary.
        for index in range(501):
            path = self.path / 'b' / 'sessions' / f'older-{index}.jsonl'
            path.write_text(json.dumps(dict(type='session_meta', payload=dict(
                id=f'older-{index}', cwd=str(self.path), source='cli'))) + '\n', encoding='utf-8')
            os.utime(path, (1000 + index, 1000 + index))

        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            # Initial indexing of hundreds of files on a shared Windows runner can
            # exceed 5 seconds; this checks pagination, not indexing speed.
            self.wait(lambda: len(tree.get_children()) == 500, timeout=30)
            iid = next(item for item in tree.get_children() if 'one.jsonl' in item)
            tree.selection_set(iid)
            root.update()
            self.button('全选').invoke()
            self.button('加载更多对话').invoke()
            self.wait(lambda: len(tree.get_children()) == 503)
            self.assertEqual(len(tree.get_children()), 503)
            self.assertEqual(tree.selection(), (iid,))
            self.assertEqual(sum(tree.set(item, 'check') == ui.CHECK_ON for item in tree.get_children()), 500)
            self.assertTrue(self.button('加载更多对话').instate(['disabled']))
            search = next(w for w in self.widgets() if w.winfo_name() == 'global_search')
            search.focus_force()
            root.update()
            search.delete(0, 'end')
            search.insert(0, 'one')
            search.event_generate('<Return>')
            self.wait(lambda: len(tree.get_children()) == 1)
            self.assertEqual(len(tree.get_children()), 1)
            search.delete(0, 'end')
            search.event_generate('<Return>')
            self.wait(lambda: len(tree.get_children()) == 500)
            self.assertEqual(len(tree.get_children()), 500)
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)

    def test_branch_handoff_and_changed_source_are_visible_before_launch(self):
        path = self.path / 'a' / 'sessions' / 'two.jsonl'
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        records[0]['payload']['agent_history_import'] = {'source': 'codex:one'}
        path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')

        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 1)
            imports = next(w for w in self.widgets() if isinstance(w, self.ttk.Checkbutton) and w.cget('text') == '导入副本')
            imports.invoke()
            self.wait(lambda: any('two.jsonl' in item for item in tree.get_children()))
            tree.selection_set(next(item for item in tree.get_children() if 'two.jsonl' in item))
            root.update()
            menu = root.nametowidget(self.target_widget().cget('menu'))
            option = next(i for i in range(menu.index('end') + 1) if menu.entrycget(i, 'label').startswith('codexc b ·'))
            self.assertEqual(menu.entrycget(option, 'state'), 'normal')
            self.assertIn('保留最新主会话', menu.entrycget(option, 'label'))
            self.choose_target('codexc a')
            self.assertTrue(self.button('打开终端并继续').instate(['!disabled']))
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(records[-1]) + '\n')
            cwd = next(w for w in self.widgets() if type(w) is self.ttk.Entry and w.get() == str(self.path))
            root.setvar(cwd.cget('textvariable'), str(self.path))
            root.update()
            self.assertTrue(self.button('打开终端并继续').instate(['disabled']))
            labels = [w.get('1.0', 'end-1c') for w in self.widgets()
                      if isinstance(w, ui.SelectableText)]
            self.assertTrue(any('会话已变化' in label for label in labels))
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)

    def test_explicit_branch_sync_button_preserves_both_branches_and_retires_only_temporary(self):
        import handoff_lifecycle as lifecycle
        import launch_backend as backend
        import launch_jobs as jobs
        import conversation_store as store
        from tests.test_trash import native_db
        for name in ('a', 'b'):
            native_db(self.path / name)
        with contextlib.closing(ah.connect()) as db:
            original = dict(ah.resolve(db, 'one'))
            prepared = backend.prepare_launch(db, original, 'b', str(self.path), policy='legacy')
            ah.collect(db)
            temporary = dict(ah.resolve(db, prepared['session']))
            for row, marker in [(original, '原平台并行'), (temporary, '临时平台新增')]:
                record = dict(type='response_item', payload=dict(type='message', role='user',
                              content=[dict(type='input_text', text=marker)]))
                with Path(row['path']).open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            jobs.update(prepared['job'], state='exited', exit_code=0)

        def interact(root):
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            imports = next(w for w in self.widgets() if isinstance(w, self.ttk.Checkbutton) and w.cget('text') == '导入副本')
            imports.invoke()
            self.wait(lambda: any(prepared['session'] in item for item in tree.get_children()))
            tree.selection_set(next(item for item in tree.get_children() if prepared['session'] in item))
            root.update()
            self.button('保留分支并完成同步').invoke()
            self.wait(lambda: jobs.read(prepared['job']).get('handoff_state') == 'cleaned' and
                      tree.selection() and prepared['session'] not in tree.selection()[0])
            self.assertFalse(Path(temporary['path']).exists())
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))

        # Unit UI checks use fixture schemas; real resume is checked in the native verifier.
        with patch.object(lifecycle, 'verify_native', side_effect=lambda db, child, final: child):
            self.exercise(interact)
        with contextlib.closing(ah.connect()) as db:
            item = lifecycle.get(db, prepared['job'])
            durable = json.loads(lifecycle.version(db, item['durable_version'])['row_json'])
            self.assertIn('临时平台新增', Path(durable['path']).read_text(encoding='utf-8'))
            self.assertIn('原平台并行', Path(original['path']).read_text(encoding='utf-8'))
            self.assertEqual(len(store.tips(db, store.lookup(db, durable)['conversation'])), 2)

    def test_logical_conversation_groups_versions_and_explicit_branch_choice(self):
        from tests.test_codex_import import native_db
        import launch_backend
        import conversation_store
        for name in ('a', 'b'):
            native_db(self.path / name)
            (self.path / name / 'profile.toml').write_text('model="fixture"\n', encoding='utf-8')
        with contextlib.closing(ah.connect()) as db:
            original = dict(ah.resolve(db, 'two'))
            prepared = launch_backend.prepare_launch(db, original, 'b', str(self.path))
            ah.collect(db)
            copied = dict(ah.resolve(db, prepared['session']))
            for row, text in [(original, '原分支新增问题'), (copied, '目标分支新增问题')]:
                with Path(row['path']).open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(dict(type='response_item', payload=dict(type='message', role='user',
                        content=[dict(type='input_text', text=text)])), ensure_ascii=False) + '\n')
            ah.collect(db)
            conversation_store.sync_all(db)

        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            grouped = next(iid for iid in tree.get_children() if any(
                iid.endswith(row['path']) for row in (original, copied)))
            self.assertNotIn('个版本', tree.set(grouped, 'title'))
            tree.selection_set(grouped)
            root.update()
            self.assertTrue(self.button('打开终端并继续').instate(['disabled']))
            versions = next(w for w in self.widgets() if w.winfo_name() == 'conversation_version')
            self.assertEqual(len(versions.cget('values')), 2)
            choice = next(value for value in versions.cget('values') if value.startswith('b /'))
            versions.set(choice)
            versions.event_generate('<<ComboboxSelected>>')
            root.update()
            self.assertTrue(self.button('打开终端并继续').instate(['disabled']))
            self.assertTrue(any('先合并分支' in value for value in self.statuses()))
            reader = next(w for w in self.widgets() if isinstance(w, self.tk.Text) and 'body' in w.tag_names())
            self.assertIn('目标分支新增问题', reader.get('1.0', 'end'))
            self.assertNotIn('原分支新增问题', reader.get('1.0', 'end'))
            self.button('测试启动（不调用模型）').invoke()
            self.wait(lambda: any('未启动 Agent 或发送模型请求' in str(value) for value in self.statuses()))
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)

    def test_first_use_missing_registry_opens_wizard_instead_of_failing(self):
        (self.shared / 'connections.json').unlink()
        with contextlib.closing(ah.connect()) as db:
            db.execute('DELETE FROM files')
            db.execute('DELETE FROM sessions')
            db.commit()
        def interact(root):
            self.wait(lambda: any(isinstance(w, self.tk.Toplevel) and w.title() == '首次配置／历史来源'
                                  for w in self.widgets()))
            self.assertIn('暂无历史', str(self.statuses()))
            self.button('稍后配置').invoke()
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)

    def test_refresh_keeps_logical_selection_when_another_platform_becomes_latest(self):
        from tests.test_codex_import import native_db
        import launch_backend
        import conversation_store
        for name in ('a', 'b'):
            native_db(self.path / name)
            (self.path / name / 'profile.toml').write_text('model="fixture"\n', encoding='utf-8')
        with contextlib.closing(ah.connect()) as db:
            original = dict(ah.resolve(db, 'two'))
            prepared = launch_backend.prepare_launch(db, original, 'b', str(self.path))
            ah.collect(db)
            copied = dict(ah.resolve(db, prepared['session']))
        def interact(root):
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            grouped = next(iid for iid in tree.get_children() if any(
                iid.endswith(row['path']) for row in (original, copied)))
            tree.selection_set(grouped)
            root.update()
            with Path(original['path']).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(type='response_item', payload=dict(type='message', role='user',
                    content=[dict(type='input_text', text='外部平台新增后刷新') ])), ensure_ascii=False) + '\n')
            self.button('刷新历史').invoke()
            reader = next(w for w in self.widgets() if isinstance(w, self.tk.Text) and 'body' in w.tag_names())
            self.wait(lambda: '外部平台新增后刷新' in reader.get('1.0', 'end'))
            self.assertEqual(len(tree.selection()), 1)
            self.assertIn('two.jsonl', tree.selection()[0])
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)

    def test_cross_connection_launch_prepares_selected_version_and_uses_target_id(self):
        from tests.test_codex_import import native_db
        for name in ('a', 'b'):
            native_db(self.path / name)
            (self.path / name / 'profile.toml').write_text('model="fixture-model"\nmodel_provider="openai"\n')
        duplicate = self.path / 'b' / 'sessions' / 'same-id.jsonl'
        duplicate.write_text(json.dumps(dict(type='session_meta', payload=dict(id='one', cwd=str(self.path), source='cli')))
                             + '\n' + json.dumps(dict(type='response_item', payload=dict(type='message', role='user',
                                 content=[dict(type='input_text', text='另一个连接的内容不得混入')])), ensure_ascii=False) + '\n', encoding='utf-8')

        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 3)
            iid = next(item for item in tree.get_children() if item.startswith('a|') and 'one.jsonl' in item)
            tree.selection_set(iid)
            root.update()
            self.choose_target('codexc b')
            self.button('打开终端并继续').invoke()
            self.wait(lambda: '终端已打开；请在终端确认恢复结果' in self.statuses())
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        start = self.exercise(interact, expect_launch=True)
        cwd, args = start.call_args.args
        self.assertEqual(cwd, str(self.path))
        if os.name == 'nt':
            self.assertIn('-NoSync', args)
        self.assertIn('-Session' if os.name == 'nt' else 'resume', args)
        sid = args[args.index('-Session' if os.name == 'nt' else 'resume') + 1]
        self.assertNotEqual(sid, 'one')
        with contextlib.closing(ah.connect()) as db:
            mapped = db.execute('SELECT dst_path FROM codex_imports WHERE dst_key=?', ('codex:' + sid,)).fetchone()
        content = Path(mapped['dst_path']).read_text(encoding='utf-8')
        self.assertIn('原始问题 one', content)
        self.assertNotIn('另一个连接的内容不得混入', content)

    def test_action_scroll_is_local_and_bottom_buttons_remain_reachable(self):
        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            tree.selection_set(next(iid for iid in tree.get_children() if 'one.jsonl' in iid))
            root.update()
            pane = next(w for w in self.widgets() if isinstance(w, self.ttk.PanedWindow))
            canvas = next(w for w in self.widgets() if isinstance(w, self.tk.Canvas))
            title = next(w for w in self.widgets() if w.winfo_name() == 'action_title')
            target = self.target_widget()
            reader = next(w for w in self.widgets() if isinstance(w, self.tk.Text) and 'body' in w.tag_names())
            root.geometry('1100x640+0+0')
            root.update()
            pane.sashpos(1, pane.winfo_width() - 300)
            title.variable.set('很长的中文标题和路径 ' * 120)
            self.wait(lambda: canvas.yview()[1] < 0.8)
            self.wait_layout()
            initial = canvas.yview()
            selected = self.target_value()
            target.event_generate('<MouseWheel>', delta=-120)
            root.update()
            self.assertGreater(canvas.yview()[0], initial[0])
            self.assertEqual(self.target_value(), selected)
            self.wait_layout()
            canvas_position = canvas.yview()
            reader.yview_moveto(0)
            root.update()
            reader_position = reader.yview()
            reader.event_generate('<MouseWheel>', delta=-120)
            root.update()
            self.assertEqual(canvas.yview(), canvas_position)
            self.assertGreater(reader.yview()[0], reader_position[0])
            canvas.yview_moveto(1)
            root.update()
            delete = self.button('删除这条对话')
            self.assertGreaterEqual(delete.winfo_rooty(), canvas.winfo_rooty())
            self.assertLess(delete.winfo_rooty() + delete.winfo_height(), canvas.winfo_rooty() + canvas.winfo_height())
            scrollbar = next(w for w in canvas.master.winfo_children() if isinstance(w, self.ttk.Scrollbar))
            self.assertTrue(scrollbar.winfo_ismapped())
            self.assertGreater(scrollbar.winfo_width(), 0)
            wrap = title.winfo_width()
            pane.sashpos(1, pane.winfo_width() - 240)
            self.wait(lambda: title.winfo_width() < wrap)
            self.assertLess(title.winfo_width(), wrap)
            self.assertTrue(self.button('浏览').winfo_ismapped())
            title.variable.set('短标题')
            root.geometry('1440x1600+0+0')
            self.wait(lambda: int(canvas.itemcget(canvas.find_all()[0], 'width')) == canvas.winfo_width())
            self.wait_layout()
            self.assertLess(float(canvas.bbox('all')[3]), canvas.winfo_height(),
                            (canvas.yview(), canvas.bbox('all'), canvas.winfo_height()))
            self.assertEqual(canvas.yview(), (0.0, 1.0),
                             (canvas.bbox('all'), canvas.cget('scrollregion'), canvas.winfo_height()))
            target.event_generate('<MouseWheel>', delta=-120)
            root.update()
            self.assertEqual(canvas.yview(), (0.0, 1.0))
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)

    def test_resize_coalesces_wrapping_and_height_changes_do_not_rewrap(self):
        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            tree.selection_set(next(iid for iid in tree.get_children() if 'one.jsonl' in iid))
            root.update()
            canvas = next(w for w in self.widgets() if isinstance(w, self.tk.Canvas))
            window = canvas.find_all()[0]
            inner = root.nametowidget(canvas.itemcget(window, 'window'))
            labels = [w for w in inner.winfo_children() if isinstance(w, (self.ttk.Label, self.tk.Label))]
            self.wait(lambda: int(canvas.itemcget(window, 'width')) == canvas.winfo_width())
            self.wait_layout()
            widths, regions = [], []
            original_label = self.tk.Widget.configure
            original_canvas = canvas.configure

            def configure_label(widget, *args, **kwargs):
                if widget in labels and 'wraplength' in kwargs:
                    widths.append((widget, kwargs['wraplength']))
                return original_label(widget, *args, **kwargs)

            def configure_canvas(*args, **kwargs):
                if 'scrollregion' in kwargs:
                    regions.append(kwargs['scrollregion'])
                return original_canvas(*args, **kwargs)

            content = next(w for w in self.widgets() if isinstance(w, self.tk.Text) and 'body' in w.tag_names())
            before = content.get('1.0', 'end')
            with patch.object(self.tk.Widget, 'configure', configure_label), \
                    patch.object(canvas, 'configure', configure_canvas), \
                    patch.object(content, 'place', wraps=content.place) as resize_reader, \
                    patch.object(md.TkRenderer, 'render', side_effect=AssertionError('Resize must not recompile Markdown')), \
                    patch.object(ui, 'launch_plan', side_effect=AssertionError('Resize must not prepare a launch')):
                # A burst of native Configure notifications must settle only the
                # final width, rather than reformatting all labels at every step.
                final = canvas.winfo_width() + 30
                reader_final = content.master.winfo_width() + 30
                for width in range(final - 29, final + 1):
                    canvas.event_generate('<Configure>', width=width, height=canvas.winfo_height())
                for width in range(reader_final - 29, reader_final + 1):
                    content.master.event_generate('<Configure>', width=width, height=content.master.winfo_height())
                self.assertEqual(widths, [])
                resize_reader.assert_not_called()
                self.wait(lambda: int(canvas.itemcget(window, 'width')) == final)
                self.wait(lambda: content.winfo_width() == reader_final)
                resize_reader.assert_called_once_with(x=0, y=0, width=reader_final, height=content.master.winfo_height())
                self.assertEqual(len(widths), len(labels))
                self.assertTrue(all(width == max(final - 48, 40) for _, width in widths))
                count = len(regions)
                for height in range(10):
                    inner.event_generate('<Configure>', width=final, height=inner.winfo_height() + height)
                    canvas.event_generate('<Configure>', width=final, height=canvas.winfo_height() + height)
                root.update()
                self.assertEqual(len(widths), len(labels))
                self.assertEqual(len(regions), count)
                self.assertEqual(content.get('1.0', 'end'), before)
            # Closing with a pending width update must still cancel all callbacks.
            canvas.event_generate('<Configure>', width=final + 10, height=canvas.winfo_height())
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
            self.assertFalse(root.tk.call('after', 'info'))
        self.exercise(interact)

    def test_resize_pauses_large_preview_and_preserves_reading_position(self):
        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children()) == 2)
            content = next(w for w in self.widgets() if isinstance(w, self.tk.Text) and 'body' in w.tag_names())
            viewport = content.master
            self.wait(lambda: content.winfo_width() == viewport.winfo_width())
            self.wait_layout()
            source = ('长对话正文，用于验证缩放和阅读位置。' * 60 + '\n') * 100
            content.configure(state='normal')
            content.delete('1.0', 'end')
            content.insert('1.0', source, 'body')
            content.configure(state='disabled')
            content.yview('50.0')
            root.update()
            position = content.index('@0,0')
            height = viewport.winfo_height() + 20
            viewport.event_generate('<Configure>', width=viewport.winfo_width(), height=height)
            self.assertFalse(content.winfo_ismapped())
            self.assertEqual(content.get('1.0', 'end-1c'), source)
            self.wait(lambda: content.winfo_ismapped() and content.winfo_height() == height)
            self.assertEqual(content.index('@0,0'), position)
            self.assertEqual(content.get('1.0', 'end-1c'), source)
            self.assertEqual(content.cget('state'), 'disabled')
            # Returning to the previous size during a drag still restores a
            # paused reader, even though no new dimensions need to be applied.
            viewport.event_generate('<Configure>', width=viewport.winfo_width(), height=height + 30)
            viewport.event_generate('<Configure>', width=viewport.winfo_width(), height=height)
            self.assertFalse(content.winfo_ismapped())
            self.wait(lambda: content.winfo_ismapped())
            self.assertEqual(content.index('@0,0'), position)
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)

    def test_project_filter_and_recycle_bin_details_restore_to_original_location(self):
        path = self.path / 'a' / 'sessions' / 'two.jsonl'
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        project_path = str(self.path / '已移除的项目')
        records[0]['payload']['cwd'] = project_path
        path.write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records), encoding='utf-8')
        stamp = time.time() - 3600
        os.utime(path, (stamp, stamp))
        with contextlib.closing(ah.connect()) as db:
            ah.collect(db)
            manifest = trash.delete(db, [dict(tool='codex', source='a', path=str(path))])
            ah.collect(db)

        def interact(root):
            root.deiconify()
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            recycle = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'deleted' in w.cget('columns'))
            notebook = next(w for w in self.widgets() if isinstance(w, self.ttk.Notebook))
            self.wait(lambda: len(tree.get_children()) == 1)
            notebook.select(1)
            self.wait(lambda: len(recycle.get_children()) == 1)
            recycle.selection_set(manifest['id'])
            root.update()
            details = next(w for w in self.widgets() if w.winfo_name() == 'trash_details')
            self.assertIn('[原会话] a · two', details.get('1.0', 'end'))
            self.assertIn(str(path), details.get('1.0', 'end'))
            self.assertIn(project_path, details.get('1.0', 'end'))
            self.assertIn('0 副本', recycle.set(manifest['id'], 'count'))
            # Switching tabs preserves selected entry/details.
            notebook.select(0)
            root.update()
            notebook.select(1)
            root.update()
            self.assertEqual(recycle.selection(), (manifest['id'],))
            self.button('恢复所选').invoke()
            self.button('恢复所选').invoke()
            self.wait(lambda: '恢复结果：1 项成功' in details.get('1.0', 'end'), timeout=30)
            self.assertIn(str(path), details.get('1.0', 'end'))
            self.assertIn('[已恢复] a · two', details.get('1.0', 'end'))
            self.assertEqual(len(recycle.get_children()), 0)
            self.wait(lambda: len(tree.get_children()) == 2)
            notebook.select(0)
            root.update()
            project = next(w for w in self.widgets() if isinstance(w, self.ttk.Combobox) and '全部项目' in w.cget('values'))
            self.assertIn(project_path, project.cget('values'))
            project.set(project_path)
            project.event_generate('<<ComboboxSelected>>')
            self.wait(lambda: len(tree.get_children()) == 1)
            self.assertEqual(len(tree.get_children()), 1)
            self.assertIn('two.jsonl', tree.get_children()[0])
            self.button('全部项目').invoke()
            self.wait(lambda: len(tree.get_children()) == 2)
            self.assertEqual(len(tree.get_children()), 2)
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)

    def test_refresh_preserves_reader_and_selection_then_persists_preferences(self):
        errors = []
        created = dt.datetime.now().astimezone().isoformat()
        snapshot = dict(candidates={}, report='fixture', created=created, outdated=0)

        def rebuild():
            time.sleep(0.03)  # allow the second click before the first worker finishes
            return snapshot

        def interact(root):
            root.deiconify()  # fully transparent and absent from the taskbar
            tree = next(w for w in self.widgets() if isinstance(w, self.ttk.Treeview) and 'check' in w.cget('columns'))
            reader = next(w for w in self.widgets() if isinstance(w, self.tk.Text) and 'body' in w.tag_names())
            self.wait(lambda: len(tree.get_children()) == 2)
            pane = next(w for w in self.widgets() if isinstance(w, self.ttk.PanedWindow))
            self.wait(lambda: abs(pane.sashpos(0) / pane.winfo_width() - 0.4) < 0.01)
            iid = next(item for item in tree.get_children() if 'one.jsonl' in item)
            tree.selection_set(iid)
            self.wait_reader()
            self.choose_target('codexc b')
            cwd = next(w for w in self.widgets() if isinstance(w, self.ttk.Entry) and w.get() == str(self.path))
            cwd.delete(0, 'end')
            cwd.insert(0, str(self.path / 'b'))
            self.button('全选').invoke()
            reader.yview_moveto(0.25)
            self.wait_reader()
            position = reader.index('@0,0')
            self.button('刷新历史').invoke()
            self.wait(lambda: '2 条' in self.statuses())
            self.assertEqual(tree.selection(), (iid,))
            self.assertTrue(all(tree.item(item, 'values')[0] == ui.CHECK_ON for item in tree.get_children()))
            self.assertEqual(self.target_value(), 'codexc b')
            self.assertEqual(cwd.get(), str(self.path / 'b'))
            self.assertEqual(reader.index('@0,0'), position)
            project = next(w for w in self.widgets() if isinstance(w, self.ttk.Combobox) and '全部项目' in w.cget('values'))
            project.set(str(self.path))
            project.event_generate('<<ComboboxSelected>>')
            self.wait_reader()
            self.assertEqual(len(tree.get_children()), 2)
            self.button('复制预览').invoke()
            self.assertIn('-From a -Session one' if os.name == 'nt' else 'codex resume', root.clipboard_get())
            self.button('重新分析清理建议').invoke()
            self.button('重新分析清理建议').invoke()
            self.wait(lambda: self.button('重新分析清理建议').instate(['!disabled']))
            self.assertTrue(any(text.startswith('清理报告：') for text in self.statuses()))
            self.button('检查环境').invoke()
            self.wait(lambda: '启动环境已重新检查' in self.statuses())
            search = next(w for w in self.widgets() if w.winfo_name() == 'global_search')
            search.delete(0, 'end')
            search.insert(0, '原始')
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))

        ready = dict(ready=True, issues=[], message='fixture ready')
        with patch.object(self.tk, 'Tk', return_value=self.root), \
                patch.object(type(self.root), 'mainloop', interact), \
                patch.object(support, 'launch_readiness', return_value=ready), \
                patch.object(support, 'rebuild_review', side_effect=rebuild) as audit, \
                patch.object(ui, 'start_terminal') as start, \
                patch.object(self.messagebox, 'showerror', side_effect=lambda *args: errors.append(args)):
            ui.run_ui()
        self.assertEqual(errors, [])
        audit.assert_called_once()
        start.assert_not_called()
        prefs = support.load_preferences()
        self.assertEqual(prefs['filters']['query'], '原始')
        self.assertEqual(prefs['filters']['project'], str(self.path))
        self.assertEqual(prefs['project_targets'][support.project_key(str(self.path))], 'b')
        self.assertEqual(prefs['default_target'], 'b')
        self.assertEqual(support.preferred_target(dict(cwd=str(self.path), source='a', tool='codex'), ['a', 'b'], prefs), 'b')


if __name__ == '__main__':
    unittest.main()
