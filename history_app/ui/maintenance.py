"""One maintenance window; work is scheduled by the existing viewer job queue."""
from dataclasses import replace
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from history_app.paths import RuntimePaths
from history_app import config_backup, diagnostics, installation


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

    def clear_plan(self):
        self.plan = None
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
