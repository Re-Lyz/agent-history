"""One maintenance window; work is scheduled by the existing viewer job queue."""
from dataclasses import replace
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from history_app.paths import RuntimePaths
from history_app import config_backup, diagnostics, installation, accounts, program_maintenance, compatibility

STATES = {'ok': '已检查', 'found': '已找到', 'installed': '安装记录', 'not-probed': '未执行自定义入口',
          'missing': '未找到', 'not-installed': '未安装后台包', 'not-checked': '未检查', 'unreachable': '控制服务不可达',
          'running': '服务运行中', 'stopped': '服务未运行', 'permission-denied': '检查权限不足',
          'failed': '检查失败', 'timeout': '检查超时', 'unavailable': '不可用'}


class MaintenanceWindow:
    def __init__(self, parent, background, on_restored=None):
        self.background, self.on_restored = background, on_restored
        self.running, self.plan, self.report = False, None, None
        self.window = tk.Toplevel(parent)
        self.window.title('诊断与维护')
        self.window.geometry('900x660')
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.status = tk.StringVar(value='诊断不会修改历史；更新和卸载需先预览。')
        self.prefix = tk.StringVar(value=str(RuntimePaths.current().prefix))
        self.source = tk.StringVar(value=str(RuntimePaths.current().source))
        self.selected_backup = tk.StringVar()
        self.controls = []
        self.program_report, self.program_plan = None, None
        tabs = ttk.Notebook(self.window)
        tabs.pack(fill='both', expand=True, padx=12, pady=12)
        health, backups, install = (ttk.Frame(tabs, padding=12) for _ in range(3))
        for page, title in ((health, '环境诊断'), (backups, '配置备份与恢复'), (install, '更新与卸载')):
            tabs.add(page, text=title)
        self.outputs = {}
        bar = ttk.Frame(health)
        bar.pack(fill='x')
        self.button(bar, '运行诊断', self.diagnose)
        self.button(bar, '校验数据库', lambda: self.diagnose(True))
        self.button(bar, '复制脱敏报告', self.copy_report)
        self.button(bar, '导出脱敏报告', self.export_report)
        self.output(health, 'health')
        ttk.Label(backups, text='只恢复本工具的来源、启动和界面配置；历史数据库、删除记录和 Agent 认证保持原状。',
                  wraplength=760).pack(anchor='w', pady=(0, 12))
        bar = ttk.Frame(backups)
        bar.pack(fill='x')
        self.button(bar, '备份当前配置', self.backup)
        self.button(bar, '刷新备份列表', self.reload_backups)
        picker = ttk.Combobox(backups, textvariable=self.selected_backup, state='readonly', name='config_backup')
        picker.pack(fill='x', pady=8)
        picker.bind('<<ComboboxSelected>>', lambda event: self.clear_plan())
        self.backup_picker = picker
        bar = ttk.Frame(backups)
        bar.pack(fill='x')
        self.button(bar, '预览配置恢复', self.preview_restore)
        self.restore_button = self.button(bar, '确认恢复配置', self.restore)
        self.output(backups, 'backups')
        for text, variable, directory in (('原安装目录', self.prefix, False), ('新版源码目录', self.source, True)):
            row = ttk.Frame(install)
            row.pack(fill='x', pady=4)
            ttk.Label(row, text=text, width=14).pack(side='left')
            entry = ttk.Entry(row, textvariable=variable)
            entry.pack(side='left', fill='x', expand=True)
            self.controls.append(entry)
            self.button(row, '浏览', lambda var=variable: self.browse(var))
            variable.trace_add('write', lambda *args: self.clear_plan())
        ttk.Label(install, text='更新已有入口及原先启用的集成，不下载源码、不安装 Agent。执行前保存配置备份；卸载保留历史与备份。',
                  wraplength=760).pack(anchor='w', pady=8)
        bar = ttk.Frame(install)
        bar.pack(fill='x')
        self.button(bar, '预览更新', lambda: self.preview_install('upgrade'))
        self.button(bar, '预览卸载', lambda: self.preview_install('uninstall'))
        self.apply_button = self.button(bar, '执行当前预览', self.apply_install)
        self.output(install, 'install')
        programs_page = ttk.Frame(tabs, padding=12)
        tabs.add(programs_page, text='程序与账户')
        ttk.Label(programs_page, text='扫描仅检查程序与已有后台包；更新前先预览，升级后使用合成历史检查兼容。',
                  wraplength=800).pack(anchor='w')
        bar = ttk.Frame(programs_page)
        bar.pack(fill='x')
        self.button(bar, '扫描程序与账户', self.scan_programs)
        self.button(bar, '复制脱敏清单', self.copy_programs)
        self.button(bar, '预览程序更新', self.preview_program_update)
        self.update_program_button = self.button(bar, '执行程序更新', self.apply_program_update)
        self.button(bar, '运行原生兼容检查', self.check_program)
        self.program_tree = ttk.Treeview(programs_page, columns=('tool', 'version', 'channel', 'state'),
                                        show='tree headings', height=4, name='program_inventory')
        self.program_tree.heading('#0', text='程序 / 关联账户')
        for key, title in (('tool', '类型'), ('version', '版本'), ('channel', '安装来源'), ('state', '状态')):
            self.program_tree.heading(key, text=title)
            self.program_tree.column(key, width=95, stretch=True)
        self.program_tree.pack(fill='x', pady=6)
        self.program_tree.bind('<<TreeviewSelect>>', self.select_program)
        self.controls.append(self.program_tree)
        self.account_tree = ttk.Treeview(programs_page, columns=('installed', 'running', 'state', 'pin'),
                                        show='tree headings', height=4, name='account_inventory')
        self.account_tree.heading('#0', text='账户')
        for key, title in (('installed', '后台包版本'), ('running', '运行版本'), ('state', '服务状态'), ('pin', '保留固定版本')):
            self.account_tree.heading(key, text=title)
            self.account_tree.column(key, width=110)
        self.account_tree.pack(fill='x', pady=6)
        self.controls.append(self.account_tree)
        bar = ttk.Frame(programs_page)
        bar.pack(fill='x')
        self.button(bar, '固定所选账户后台包', lambda: self.pin_account(True))
        self.button(bar, '取消所选账户固定', lambda: self.pin_account(False))
        self.output(programs_page, 'programs')
        ttk.Label(self.window, textvariable=self.status, wraplength=850).pack(fill='x', padx=12, pady=(0, 12))
        self.refresh_buttons()
        self.reload_backups()

    def button(self, parent, title, command):
        widget = ttk.Button(parent, text=title, command=command)
        widget.pack(side='left', padx=(0, 8), pady=3)
        self.controls.append(widget)
        return widget

    def output(self, parent, name):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True, pady=(8, 0))
        text = tk.Text(frame, wrap='word', state='disabled', padx=8, pady=8, name=name + '_details')
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        self.outputs[name] = text

    def show(self, name, text):
        widget = self.outputs[name]
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', text)
        widget.configure(state='disabled')

    def paths(self):
        return replace(RuntimePaths.current(self.prefix.get()), source=Path(self.source.get()).expanduser().resolve())

    def refresh_buttons(self):
        for widget in self.controls:
            widget.state(['disabled'] if self.running else ['!disabled'])
        self.backup_picker.state(['disabled'] if self.running else ['!disabled', 'readonly'])
        self.restore_button.state(['!disabled'] if not self.running and self.plan and self.plan[0] == 'restore' else ['disabled'])
        self.apply_button.state(['!disabled'] if not self.running and self.plan and self.plan[0] == 'install' else ['disabled'])
        if hasattr(self, 'update_program_button'):
            self.update_program_button.state(['!disabled'] if not self.running and self.program_plan else ['disabled'])

    def clear_plan(self):
        self.plan = None
        self.program_plan = None
        if hasattr(self, 'apply_button'):
            self.refresh_buttons()

    def run(self, work, done, label):
        if self.running:
            return
        self.running = True
        self.status.set(label)
        self.refresh_buttons()
        def guarded():
            try:
                return work(), None
            except Exception as error:
                return None, error
        def finished(value):
            self.running = False
            result, error = value
            if error:
                self.status.set('未完成：' + str(error))
                self.clear_plan()
            else:
                self.status.set('已完成')
                done(result)
            self.refresh_buttons()
        self.background(guarded, finished, label=label, key='maintenance', refresh_preview=False)

    def diagnose(self, deep=False):
        paths = self.paths()
        def done(report):
            self.report = report
            self.show('health', diagnostics.render(report, include_paths=True))
            self.status.set('诊断完成。窗口显示本机路径；复制和导出仅保留脱敏概要。')
        self.run(lambda: diagnostics.diagnose(paths, deep), done, '正在只读检查环境…')

    def copy_report(self):
        if self.report is None:
            self.status.set('请先运行诊断。')
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(diagnostics.render(self.report))
        self.status.set('已复制脱敏诊断报告，不含路径、配置值或对话内容。')

    def export_report(self):
        if self.report is None:
            self.status.set('请先运行诊断。')
            return
        location = filedialog.asksaveasfilename(parent=self.window, defaultextension='.json',
                                               initialfile='agent-history-diagnostics.json')
        if location:
            try:
                Path(location).write_text(diagnostics.render(self.report) + '\n', encoding='utf-8')
                self.status.set('已导出脱敏诊断报告。')
            except OSError as error:
                self.status.set('导出失败：' + str(error))

    def reload_backups(self):
        paths = self.paths()
        def done(values):
            self.backup_picker['values'] = [item['id'] for item in values]
            if values and self.selected_backup.get() not in self.backup_picker['values']:
                self.selected_backup.set(values[0]['id'])
        self.run(lambda: config_backup.inventory(paths), done, '正在读取配置备份…')

    def backup(self):
        paths = self.paths()
        def done(identity):
            self.show('backups', '已保存不可变配置快照：' + identity + '\n备份仅保存在私人运行目录。')
            self.clear_plan()
            self.reload_backups()
        self.run(lambda: config_backup.backup(paths), done, '正在备份配置…')

    def preview_restore(self):
        paths, identity = self.paths(), self.selected_backup.get()
        def done(plan):
            self.plan = ('restore', paths, plan)
            self.show('backups', json.dumps(plan['changes'], ensure_ascii=False, indent=2) +
                      '\n确认恢复前会再次备份当前配置。')
        self.run(lambda: config_backup.preview_restore(paths, identity), done, '正在核对配置备份…')

    def restore(self):
        if not self.plan or self.plan[0] != 'restore':
            return
        _, paths, plan = self.plan
        def done(result):
            self.clear_plan()
            self.show('backups', f"已恢复 {result['restored']} 项配置。恢复前快照：{result['recovery_backup']}")
            self.status.set('配置已恢复。界面偏好在重新打开后生效。')
            if self.on_restored:
                self.on_restored()
        self.run(lambda: config_backup.restore(paths, plan), done, '正在恢复配置…')

    def browse(self, variable):
        value = filedialog.askdirectory(parent=self.window, initialdir=variable.get())
        if value:
            variable.set(value)

    def preview_install(self, action):
        paths = self.paths()
        def done(plan):
            self.plan = ('install', paths, plan)
            self.apply_button.configure(text='确认更新入口' if action == 'upgrade' else '确认卸载（保留历史）')
            self.show('install', json.dumps({key: plan[key] for key in ('action', 'changes', 'kept', 'tasks')},
                                           ensure_ascii=False, indent=2))
        self.run(lambda: installation.preview(paths, action), done, '正在生成维护预览…')

    def apply_install(self):
        if not self.plan or self.plan[0] != 'install':
            return
        _, paths, plan = self.plan
        def done(result):
            self.clear_plan()
            self.show('install', json.dumps(result, ensure_ascii=False, indent=2))
        self.run(lambda: installation.apply(paths, plan), done, '正在执行已预览的维护…')

    def close(self):
        if self.running:
            self.status.set('维护任务尚在运行，请等待完成后关闭。')
            return
        self.window.destroy()

    def scan_programs(self):
        paths = self.paths()
        def done(report):
            self.program_report = report
            self.program_plan = None
            self.program_tree.delete(*self.program_tree.get_children())
            for item in report['programs']:
                self.program_tree.insert('', 'end', iid=item['id'],
                    text=Path(item['path'] or item['requested']).name + ' / ' + str(len(item['accounts'])),
                    values=(item['tool'], item['version'] or '未知', item['channel'], STATES.get(item['state'], item['state'])))
            self.show('programs', '扫描完成，选择程序查看账户与本机路径。\n兼容记录：\n' +
                      json.dumps(report['compatibility'], ensure_ascii=False, indent=2))
        self.run(lambda: accounts.inventory(paths), done, '正在只读检查程序和账户…')

    def invalidate_programs(self):
        self.program_plan, self.program_report = None, None
        self.program_tree.delete(*self.program_tree.get_children())
        self.account_tree.delete(*self.account_tree.get_children())

    def selected_program(self):
        selected = self.program_tree.selection()
        if not selected or not self.program_report:
            raise ValueError('请先扫描并选择程序。')
        return next(item for item in self.program_report['programs'] if item['id'] == selected[0])

    def select_program(self, event=None):
        if not self.program_tree.selection():
            return
        item = self.selected_program()
        self.program_plan = None
        self.account_tree.delete(*self.account_tree.get_children())
        related = [row for row in self.program_report['accounts'] if row['program_id'] == item['id']]
        for row in related:
            state = row['daemon']
            self.account_tree.insert('', 'end', iid=row['id'], text=row['name'], values=(
                state.get('installed') or '—', state.get('running') or '—', STATES.get(state['state'], state['state']), '是' if row['pinned'] else '否'))
        self.show('programs', json.dumps(dict(program=item, accounts=related), ensure_ascii=False, indent=2))
        self.refresh_buttons()

    def copy_programs(self):
        if self.program_report is None:
            self.status.set('请先扫描程序与账户。')
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(json.dumps(accounts.shareable(self.program_report), ensure_ascii=False, indent=2))
        self.status.set('已复制脱敏程序清单。')

    def pin_account(self, enabled):
        selected = self.account_tree.selection()
        if not selected:
            self.status.set('请选择账户。')
            return
        paths = self.paths()
        self.run(lambda: accounts.set_pinned(paths, selected[0], enabled),
                 lambda _: self.scan_programs(), '正在保存账户维护偏好…')

    def preview_program_update(self):
        try:
            identity = self.selected_program()['id']
        except ValueError as error:
            self.status.set(str(error))
            return
        def done(plan):
            self.program_plan = plan
            self.show('programs', json.dumps(plan, ensure_ascii=False, indent=2))
        paths = self.paths()
        self.run(lambda: program_maintenance.preview(paths, identity), done, '正在检查程序更新计划…')

    def apply_program_update(self):
        if self.program_plan:
            paths, plan = self.paths(), self.program_plan
            def done(result):
                self.invalidate_programs()
                self.show('programs', json.dumps(result, ensure_ascii=False, indent=2))
                self.status.set('程序维护结果已保存：' + result.get('state', 'unknown') + '。请重新扫描查看版本与账户状态。')
            self.run(lambda: program_maintenance.apply(paths, plan), done, '正在执行程序更新与兼容检查…')

    def check_program(self):
        try:
            item = self.selected_program()
            if item['tool'] != 'codex' or not item['path'] or item['state'] != 'ok':
                raise ValueError('请选择已安装的 Codex 程序。')
        except ValueError as error:
            self.status.set(str(error))
            return
        paths = self.paths()
        self.run(lambda: compatibility.check(paths, item['path'], python=accounts.policy(paths).get('python')),
                 lambda result: self.show('programs', json.dumps(result, ensure_ascii=False, indent=2)),
                 '正在使用隔离合成历史检查原生兼容，不调用模型…')
