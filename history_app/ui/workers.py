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
from history_app.read_workers import ReadWorkers
from history_app import performance


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
            if key not in ('history-list', 'reader', 'reader-page', 'reader-full', 'reader-search',
                           'markdown', 'selection-details', 'launch-preview') and not (key and key.startswith('tool-detail-')):
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
                    phase = 'tool-detail' if key and key.startswith('tool-detail-') else {'history-list': 'listing', 'reader': 'reader', 'reader-page': 'reader',
                             'reader-full': 'search', 'launch-preview': 'launch-preview',
                             'selection-details': 'reader', 'tool-detail': 'tool-detail', 'reader-search': 'search'}.get(key)
                    if phase:
                        with performance.measure(phase):
                            result = work()
                    else:
                        result = work()
                    self.results.put((serial, done, result, None))
                except Exception as error:      # surfaced in the UI thread
                    self.results.put((serial, done, None, error))
                finally:
                    performance.flush(ah.DATA)
            if key in ('history-list', 'reader', 'reader-page', 'reader-full', 'markdown',
                       'selection-details', 'launch-preview', 'tool-detail', 'reader-search') or (key and key.startswith('tool-detail-')):
                if not hasattr(self, 'read_workers'):
                    self.read_workers = ReadWorkers()
                self.read_workers.submit(runner)
            else:
                threading.Thread(target=runner, daemon=True).start()

        def poll(self):
            with contextlib.suppress(queue.Empty):
                while True:
                    serial, done, result, error = self.results.get_nowait()
                    completed_job = self.jobs.pop(serial, (None, ''))
                    if completed_job[0] != 'history-list' or error:
                        self.busy.set('')
                    try:
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
                    elif not self.busy.get():
                        self.busy.set(f'{len(self.rows)} 条')
            self.root.after(100, self.poll)

        def reindex(self):
            with ah.collect_lock(wait=True) as locked:
                if not locked:
                    raise ValueError('历史索引正在使用，请稍后重试。')
                db = ah.connect()
                try:
                    indexed = ah.collect(db)
                    report = conversations.sync_all(db)
                    report['failed'].extend(getattr(indexed, 'failed', []))
                    launch_jobs.reconcile()
                    import handoff_lifecycle
                    handoff_lifecycle.retry_pending(db)
                    launch_jobs.mark_collected(report)
                    failures = db.execute('SELECT count(*) FROM index_failures').fetchone()[0]
                finally:
                    db.close()
            snapshot = support.review_snapshot()
            snapshot['index_failures'] = failures
            return snapshot

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
            failures = snapshot.get('index_failures', 0)
            if failures:
                self.notice(f'{failures} 个文件索引待重试；已有内容保留，可点击“重试失败索引”。')

        def retry_failed_indexes(self):
            def work():
                with ah.collect_lock(wait=True) as locked:
                    if not locked:
                        raise ValueError('历史索引正在使用，请稍后重试。')
                    with contextlib.closing(ah.connect()) as db:
                        paths = {row['path'] for row in db.execute('SELECT path FROM index_failures')}
                        result = ah.collect(db, only_paths=paths)
                        return len(paths), len(getattr(result, 'failed', []))
            def done(result):
                self.projects_dirty = True
                self.refresh()
                self.notice(f'已重试 {result[0]} 个文件，仍有 {result[1]} 个待处理。')
            self.background(work, done, label='正在重试失败索引…', key='retry-index', refresh_preview=False)

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
