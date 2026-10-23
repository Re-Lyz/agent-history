"""Workers behavior for the history window; dependencies are bound per window."""
import contextlib
import datetime
import queue
import threading
import agent_history as ah
import trash
import ui_support as support
import connections
import settings_ui
import launch_jobs
import conversation_store as conversations
from history_app.reader_data import ReadCancelled


def create_mixin(bindings, desktop):

    class WorkersController:
        def notice(self, message):
            self.idle_notice = message
            self.busy.set(message)

        def background(self, work, done=None, *, label='处理中…', key=None, refresh_preview=True, replace=False):
            if key and any(job[0] == key for job in self.jobs.values()):
                if replace:
                    self.pending_jobs[key] = (work, done, label, refresh_preview)
                return  # repeated clicks do not start the same operation again
            if key != 'history-list':
                self.idle_notice = None
            self.job_serial += 1
            serial = self.job_serial
            self.jobs[serial] = (key, label, refresh_preview)
            self.busy.set(label)
            if key == 'audit':
                self.audit_button.state(['disabled'])
            if key == 'launch':
                self.launch_button.state(['disabled'])
            if key == 'restore':
                self.restore_button.state(['disabled'])
                self.purge_button.state(['disabled'])

            def runner():
                try:
                    result = work()
                    self.results.put((serial, done, result, None))
                except Exception as error:      # surfaced in the UI thread
                    self.results.put((serial, done, None, error))
            threading.Thread(target=runner, daemon=True).start()

        def poll(self):
            with contextlib.suppress(queue.Empty):
                while True:
                    serial, done, result, error = self.results.get_nowait()
                    completed_job = self.jobs.pop(serial, (None, ''))
                    if completed_job[0] != 'history-list' or error:
                        self.busy.set('')
                    try:
                        if error and completed_job[0] == 'reader-full':
                            self.reader_full_loading = None
                        if isinstance(error, ReadCancelled):
                            pass
                        elif error:
                            self.show_error('操作失败', error)
                        elif done:
                            done(result)
                    except Exception as callback_error:
                        self.show_error('操作失败', callback_error)
                    self.audit_button.state(['disabled'] if any(j[0] == 'audit' for j in self.jobs.values()) else ['!disabled'])
                    restoring = any(j[0] == 'restore' for j in self.jobs.values())
                    for button in (self.restore_button, self.purge_button):
                        button.state(['disabled'] if restoring else ['!disabled'])
                    if len(completed_job) < 3 or completed_job[2]:
                        self.update_preview(immediate=True)
                    if completed_job[0] == 'test-launch' and not error:
                        self.launch_status.set(result['message'])
                    if self.launch_job and completed_job[0] in ('launch', 'handoff-sync'):
                        with contextlib.suppress(ValueError, OSError):
                            self.launch_status.set(launch_jobs.describe(launch_jobs.read(self.launch_job, wait=False)))
                    pending = self.pending_jobs.pop(completed_job[0], None)
                    if pending:
                        work, callback, label, preview = pending
                        self.background(work, callback, label=label, key=completed_job[0], refresh_preview=preview)
                    if self.jobs:
                        self.busy.set(self.jobs[max(self.jobs)][1])
                    elif self.idle_notice is not None:
                        self.busy.set(self.idle_notice)
            self.root.after(100, self.poll)

        def reindex(self):
            with ah.collect_lock(wait=True) as locked:
                if not locked:
                    raise ValueError('历史索引正在使用，请稍后重试。')
                db = ah.connect()
                try:
                    ah.collect(db)
                    report = conversations.sync_all(db)
                    launch_jobs.reconcile()
                    import handoff_lifecycle
                    handoff_lifecycle.retry_pending(db)
                    launch_jobs.mark_collected(report)
                finally:
                    db.close()
            return support.review_snapshot()

        def initial_load(self):
            trash.purge()
            snapshot = self.reindex()
            targets = bindings.launch_targets()
            readiness = {target: support.launch_readiness(target) for target in targets}
            return targets, snapshot, readiness

        def after_initial(self, result):
            targets, snapshot, readiness = result
            self.apply_readiness(readiness)
            registry = connections.load(ah.ROOT)
            self.source_box['values'] = ['全部来源'] + list(registry['connections']) + [bindings.CLAUDE]
            if self.source.get() not in self.source_box['values']:
                self.source.set('全部来源')
            self.initial_list_pending = True
            self.after_reindex(snapshot)

        def manage_profiles(self):
            settings_ui.ProfileManager(self.root, self.settings_changed)

        def setup_sources(self):
            settings_ui.SetupWizard(self.root, self.settings_changed)

        def open_maintenance(self):
            from history_app.ui.maintenance import MaintenanceWindow
            current = getattr(self, 'maintenance_window', None)
            if current and current.window.winfo_exists():
                current.window.lift()
                return
            self.maintenance_window = MaintenanceWindow(self.root, self.background, self.configuration_restored)

        def configuration_restored(self):
            self.preserve_restored_preferences = True
            self.preferences = support.load_preferences()
            self.settings_changed()

        def settings_changed(self):
            import launch_profiles
            configured, _ = launch_profiles.load()
            default = next((key for key, profile in configured.items() if profile['default'] and profile['enabled']), None)
            if default:
                self.preferences['default_target'] = default
            self.background(self.initial_load, self.after_initial, label='正在应用配置并索引历史…', key='initial')

        def apply_readiness(self, readiness):
            selected = self.target_names.get(self.target.get())
            self.readiness = readiness
            self.target_labels = {target: bindings.command_label(target) + (' · 未就绪' if not info['ready'] else '')
                                  for target, info in readiness.items()}
            self.target_names = {label: target for target, label in self.target_labels.items()}
            if selected in self.target_labels:
                self.target.set(self.target_labels[selected])

        def after_reindex(self, snapshot):
            self.projects_dirty = True
            self.candidates = snapshot['candidates']
            if snapshot['created']:
                created = datetime.datetime.fromisoformat(snapshot['created']).astimezone()
                old = datetime.datetime.now().astimezone() - created > datetime.timedelta(days=1)
                note = ' · 报告较早，建议重新分析' if old else ''
                if snapshot['outdated']:
                    note += f" · {snapshot['outdated']} 条已变化或删除，已排除"
                self.review_info.set(f"清理报告：{created:%Y-%m-%d %H:%M} · {len(self.candidates)} 个可审阅候选{note}")
            else:
                self.review_info.set('尚无完整清理报告；重新分析只生成建议，不删除对话。')
            self.refresh()

        def reanalyze(self):
            self.background(support.rebuild_review, self.after_reindex,
                            label='正在重新分析历史，较大历史库可能需要几分钟…', key='audit')

        def check_environment(self):
            targets = list(self.readiness) or bindings.launch_targets()

            def done(readiness):
                self.apply_readiness(readiness)
                self.update_preview()
                self.notice('启动环境已重新检查')

            self.background(lambda: {target: support.launch_readiness(target) for target in targets},
                            done, label='正在检查启动环境…', key='diagnostics')

    return WorkersController
