"""Launch behavior for the history window; dependencies are bound per window."""
import contextlib
import os
from pathlib import Path
import agent_history as ah
import trash
import ui_support as support
import launch_backend as launcher
import new_conversation_ui
import launch_jobs
import conversation_store as conversations


def create_mixin(bindings, desktop):
    tk = desktop.tk
    ttk = desktop.ttk
    messagebox = desktop.messagebox
    filedialog = desktop.filedialog
    P = desktop.palette

    class LaunchController:
        def new_conversation(self):
            if self.new_dialog and self.new_dialog.alive():
                self.new_dialog.window.lift()
                return
            cwd = self.cwd.get()
            self.new_dialog = new_conversation_ui.NewConversationDialog(self.root, self.start_new,
                target=self.preferences.get('default_target', ''),
                cwd=cwd if cwd and Path(cwd).is_dir() else str(Path.home()), on_settings=self.manage_profiles)

        def start_new(self, dialog, target, cwd, expected_plan):
            if any(job[0] == 'launch' for job in self.jobs.values()):
                dialog.finish('已有启动正在准备，请稍后重试。')
                return

            def prepare():
                try:
                    ready = launcher.new_readiness(target)
                    if not ready['ready']:
                        raise ValueError(ready['message'])
                    return launcher.prepare_new(target, cwd, expected_plan), None
                except Exception as error:
                    return None, error

            def done(result):
                prepared, error = result
                if error is not None:
                    support.log_error('Preparing a new conversation', error)
                    dialog.finish(error)
                    self.busy.set('新会话未启动，请查看新增对话窗口的提示。')
                    return
                if not dialog.alive():
                    launch_jobs.update(prepared['job'], state='cancelled', sync_state='not-needed')
                    self.busy.set('已取消新会话启动')
                    return
                self.launch_job = prepared['job']
                try:
                    bindings.start_terminal(prepared['cwd'], prepared['inner'], env=prepared['env'])
                except (OSError, ValueError) as error:
                    launch_jobs.update(self.launch_job, state='failed', sync_state='not-needed',
                                       error='无法打开终端；请检查终端程序后重试。')
                    support.log_error('Opening a new conversation terminal', error)
                    dialog.finish(error)
                    self.busy.set('新会话终端未打开，可修改配置后重试。')
                    return
                self.preferences['default_target'] = target
                self.preferences['project_targets'][support.project_key(prepared['cwd'])] = target
                dialog.finish()
                self.busy.set('新会话终端已打开；请在终端完成登录并开始对话')

            self.background(prepare, done, label='正在准备新会话…', key='launch')

        def target_changed(self, event=None):
            target = self.target_names.get(self.target.get())
            if target:
                self.preferences['default_target'] = target
                rows = self.selected()
                project = support.project_key(rows[0]['cwd']) if rows else ''
                if project:
                    self.preferences['project_targets'][project] = target
            self.update_preview(immediate=True)

        def copy_preview(self):
            self.root.clipboard_clear()
            self.root.clipboard_append(self.preview.get())
            self.notice('命令预览已复制')

        def open_log(self):
            path = support.error_log_path()
            if not path.is_file():
                self.busy.set('尚无错误日志')
            elif os.name == 'nt':
                os.startfile(path)
            else:
                messagebox.showinfo('错误日志', str(path))

        def test_launch(self):
            try:
                row, _ = self.current_plan()
                target = self.target_names[self.target.get()]
                cwd = self.cwd.get()
            except ValueError as error:
                return self.show_error('无法测试启动', error)
            def check():
                with contextlib.closing(ah.connect()) as db:
                    return launcher.test_launch(db, row, target, cwd)
            self.background(check, lambda report: self.launch_status.set(report['message']),
                            label='正在测试启动配置（不发送模型请求）…', key='test-launch')

        def poll_launch_report(self):
            if self.launch_job:
                try:
                    report = launch_jobs.read(self.launch_job, wait=False)
                    if report != self.last_launch_report:
                        self.last_launch_report = report
                        self.launch_status.set(launch_jobs.describe(report))
                        if report.get('sync_state') == 'collected' and report['state'] in ('exited', 'failed'):
                            self.background(self.reindex, self.after_reindex, label='正在读取续聊内容…', key='handoff-sync')
                except TimeoutError:
                    pass  # The runner is publishing a report; try next tick.
                except (ValueError, OSError) as error:
                    support.log_error('Reading terminal status', error)
            self.root.after(1000, self.poll_launch_report)

        def show_launch_reports(self):
            window = tk.Toplevel(self.root)
            window.title('启动记录与诊断')
            window.geometry('760x480')
            body = tk.Text(window, wrap='word', padx=12, pady=12)
            scroll = ttk.Scrollbar(window, command=body.yview)
            body.configure(yscrollcommand=scroll.set)
            scroll.pack(side='right', fill='y')
            body.pack(fill='both', expand=True)
            reports = launch_jobs.reports()
            for report in reports:
                body.insert('end', report['created'][:19] + ' · ' + report['target'] + '\n' +
                            launch_jobs.describe(report) + '\n\n')
            if not reports:
                body.insert('end', '尚无启动记录。')
            body.configure(state='disabled')

        def retry_handoff(self, allow_branch=False):
            rows = self.selected()
            selected = rows[0] if rows else None
            latest_job = self.launch_job
            def work():
                import handoff_lifecycle
                with trash.locked_db() as db:
                    identity = bindings.recovery_job(db, selected, None if allow_branch else latest_job)
                    if allow_branch and not identity:
                        raise ValueError('请先选择要保存的临时续聊分支。')
                    outcome = handoff_lifecycle.retry_pending(db, manual=True, allow_branch=allow_branch, identity=identity) \
                        if identity else dict(completed=0, pending=0)
                return outcome, self.reindex(), identity
            def done(result):
                outcome, snapshot, identity = result
                if identity:
                    self.launch_job = identity
                self.after_reindex(snapshot)
                message = f"同步完成 {outcome['completed']} 项；仍待处理 {outcome['pending']} 项，详情见启动记录。"
                self.launch_status.set(message)
                self.busy.set(message)
            self.background(work, done, label='正在保存主对话并清理已接替的旧文件…', key='handoff-sync')

        def cancel_unclaimed_launch(self):
            rows = self.selected()
            if len(rows) != 1:
                return self.show_error('无法取消启动', ValueError('请先选择对应的对话。'))
            selected = rows[0]
            def work():
                import single_conversation
                with trash.locked_db(reindex=False) as db:
                    identity = bindings.recovery_job(db, selected)
                    if not identity:
                        raise ValueError('所选对话没有待接管的启动预约。')
                    single_conversation.cancel_prepared(db, identity)
                return identity
            def done(identity):
                self.refresh()
                self.notice('已取消未接管启动；会话文件保留，迟到的旧启动不会再次执行。')
            self.background(work, done, label='正在核对原生会话并取消未接管启动…', key='cancel-launch')

        def sync_switched_session(self):
            rows = self.selected()
            if len(rows) != 1:
                return self.show_error('无法同步切出对话', ValueError('请先选择原来那条对话。'))
            selected = rows[0]
            def work():
                import single_conversation
                with trash.locked_db(reindex=False) as db:
                    identity = bindings.recovery_job(db, selected)
                    if not identity:
                        raise ValueError('所选对话没有对应的运行任务。')
                    return single_conversation.detach_session(db, identity)
            def done(result):
                self.refresh()
                self.notice('已同步切出的原对话并释放预约；终端继续运行，会话文件全部保留。')
            self.background(work, done, label='正在核对原生写锁并同步切出对话…', key='detach-session')

        def current_plan(self, db=None, plans=None):
            rows = self.selected()
            if len(rows) != 1:
                raise ValueError('请选择一条对话。')
            target = self.target_names.get(self.target.get())
            if not target:
                raise ValueError('请选择启动命令。')
            if self.reader_stamp is None or bindings.transcript_stamp(rows[0]) != self.reader_stamp:
                raise ValueError('会话已变化，请刷新历史后核对新增内容再启动。')
            if plans is not None and target in plans:
                return rows[0], plans[target]
            if db is not None:
                return rows[0], bindings.launch_plan(db, rows[0], target, self.cwd.get())
            with contextlib.closing(ah.connect()) as db:
                return rows[0], bindings.launch_plan(db, rows[0], target, self.cwd.get())

        def update_preview(self, immediate=False):
            if immediate:
                self.refresh_launch_preview()
                return
            if getattr(self, 'preview_job', None) is None:
                self.preview_job = self.root.after_idle(self.refresh_launch_preview)

        def refresh_launch_preview(self):
            if getattr(self, 'preview_job', None) is not None:
                self.root.after_cancel(self.preview_job)
            self.preview_job = None
            with contextlib.closing(ah.connect()) as db:
                self.show_launch_preview(db)

        def show_launch_preview(self, db):
            self.target_menu.delete(0, 'end')
            rows = self.selected()
            plans = {}
            launch_reason = ''
            if len(rows) == 1:
                member = conversations.lookup(db, rows[0])
                if member:
                    import single_conversation
                    try:
                        launch_reason = single_conversation.reservation_reason(db, member, wait=False)
                        single_conversation.latest(db, dict(member))
                    except ValueError as error:
                        launch_reason = str(error)
            for name, label in self.target_labels.items():
                reason, description = '', '请先选择对话'
                if rows:
                    try:
                        option = bindings.launch_plan(db, rows[0], name, self.cwd.get())
                        plans[name] = option
                        description = option.get('description', '恢复选中版本')
                        reason = launch_reason
                    except (ValueError, OSError) as error:
                        reason = str(error)
                else:
                    reason = description
                ready = self.readiness.get(name, dict(ready=False, message='请先检查启动环境。'))
                reason = reason or (ready['message'] if not ready['ready'] else '')
                text = f'{bindings.command_label(name)} · {reason or description}'
                self.target_menu.add_radiobutton(label=text, value=label, variable=self.target,
                                                state='disabled' if reason else 'normal', command=self.target_changed)
            try:
                _, plan = self.current_plan(db, plans)
                current = self.selected()[0]
                if launch_reason:
                    raise ValueError(launch_reason)
                self.preview.set(plan['preview'])
                self.preview_label.configure(fg=P['text'], bg=P['code_bg'])
                self.copy_button.state(['!disabled'])
                target = self.target_names[self.target.get()]
                readiness = self.readiness.get(target, dict(ready=False, message='请先检查启动环境。'))
                self.launch_status.set(plan.get('description', '恢复选中版本') + '\n' + readiness['message'])
                launching = any(job[0] == 'launch' for job in self.jobs.values())
                self.launch_button.state(['!disabled'] if readiness['ready'] and not launching else ['disabled'])
            except ValueError as error:
                self.preview.set(str(error) if self.selected() else '')
                self.preview_label.configure(fg=P['danger'], bg=P['card'] if not self.selected() else '#f8ebe8')
                self.launch_button.state(['disabled'])
                self.copy_button.state(['disabled'])
                self.launch_status.set('')

        def browse(self):
            folder = filedialog.askdirectory(initialdir=self.cwd.get() or str(Path.home()))
            if folder:
                self.cwd.set(str(Path(folder)))

        def launch(self):
            try:
                row, plan = self.current_plan()
                target = self.target_names[self.target.get()]
                readiness = support.launch_readiness(target)
                if not readiness['ready']:
                    self.readiness[target] = readiness
                    self.update_preview()
                    raise ValueError(readiness['message'])
            except ValueError as error:
                return self.show_error('无法启动', error)
            cwd = self.cwd.get().strip()
            expected_stamp = self.reader_stamp

            def prepare():
                with trash.locked_db() as db:
                    return launcher.prepare_launch(db, row, target, cwd, expected_stamp)

            def done(prepared):
                self.launch_job = prepared.get('job')
                try:
                    if prepared['env']:
                        bindings.start_terminal(prepared['cwd'], prepared['inner'], env=prepared['env'])
                    else:
                        bindings.start_terminal(prepared['cwd'], prepared['inner'])
                except OSError:
                    if self.launch_job:
                        launch_jobs.update(self.launch_job, state='failed', error='无法打开终端；请检查终端程序后重试。')
                    raise
                self.preferences['default_target'] = target
                if row['cwd']:
                    self.preferences['project_targets'][support.project_key(row['cwd'])] = target
                self.busy.set('终端已打开；请在终端确认恢复结果')
            self.background(prepare, done, label='正在准备选中的会话版本…', key='launch')

    return LaunchController
