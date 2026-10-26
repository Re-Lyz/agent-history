"""Project workspace; all reads reuse the viewer's bounded background queue."""
import json
import tkinter as tk
from tkinter import ttk

from history_app.paths import RuntimePaths
from history_app import workspace


class WorkspaceWindow:
    def __init__(self, parent, background, open_project):
        self.background, self.open_project = background, open_project
        self.window = tk.Toplevel(parent)
        self.window.title('账户与项目工作台')
        self.window.geometry('1000x740')
        self.request, self.offset, self.cursor = 0, 0, None
        self.status = tk.StringVar(value='项目历史与任务状态来自现有索引；历史内容不代表当前待办。')
        self.project, self.query = tk.StringVar(), tk.StringVar()
        top = ttk.Frame(self.window, padding=10)
        top.pack(fill='x')
        for text, command in (('刷新工作台', self.refresh), ('上一页项目', lambda: self.page(-1)),
                              ('下一页项目', lambda: self.page(1)), ('打开项目历史', self.open_history)):
            ttk.Button(top, text=text, command=command).pack(side='left', padx=4)
        self.tree = ttk.Treeview(self.window, columns=('versions', 'accounts', 'tasks'), show='tree headings', height=8,
                                 name='workspace_projects')
        self.tree.heading('#0', text='项目目录')
        self.tree.column('#0', width=550)
        for key, title in (('versions', '物理版本'), ('accounts', '历史来源'), ('tasks', '任务记录')):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=120)
        self.tree.pack(fill='x', padx=12)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        bar = ttk.Frame(self.window, padding=10)
        bar.pack(fill='x')
        ttk.Label(bar, text='项目').pack(side='left')
        ttk.Entry(bar, textvariable=self.project, width=55).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Label(bar, text='关键词').pack(side='left')
        ttk.Entry(bar, textvariable=self.query, width=20).pack(side='left', padx=6)
        ttk.Button(bar, text='提取上下文', command=self.context).pack(side='left')
        ttk.Button(bar, text='更多上下文', command=lambda: self.context(True)).pack(side='left', padx=4)
        ttk.Button(bar, text='复制材料', command=self.copy).pack(side='left')
        self.project.trace_add('write', self.invalidate)
        self.query.trace_add('write', self.invalidate)
        self.text = tk.Text(self.window, wrap='word', state='disabled', name='workspace_details', padx=10, pady=10)
        scroll = ttk.Scrollbar(self.window, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.text.pack(fill='both', expand=True, padx=(12, 0), pady=10)
        ttk.Label(self.window, textvariable=self.status, wraplength=950).pack(fill='x', padx=12, pady=(0, 10))
        self.refresh()

    def invalidate(self, *args):
        self.request += 1
        self.cursor = None

    def show(self, value, append=False):
        self.text.configure(state='normal')
        if not append:
            self.text.delete('1.0', 'end')
        self.text.insert('end', value + '\n')
        self.text.configure(state='disabled')

    def run(self, work, done, label):
        self.request += 1
        request = self.request
        self.status.set(label)
        def guarded():
            try:
                return work(), None
            except Exception as error:
                return None, str(error)
        def finished(value):
            if self.window.winfo_exists() and request == self.request:
                result, error = value
                if error:
                    self.status.set('未完成：' + error)
                else:
                    done(result)
        self.background(guarded, finished, label=label, key='workspace', replace=True, refresh_preview=False)

    def refresh(self):
        paths, offset = RuntimePaths.current(), self.offset
        def done(report):
            self.report = report
            self.tree.delete(*self.tree.get_children())
            for index, item in enumerate(report['projects']):
                self.tree.insert('', 'end', iid=str(index), text=item['cwd'], values=(item['versions'],
                    ', '.join(row['source'] for row in item['sources']), len(item['tasks'])))
            self.show(json.dumps(dict(accounts=report['accounts'], task_records=report['jobs']), ensure_ascii=False, indent=2))
            self.status.set(f"显示第 {offset + 1}–{offset + len(report['projects'])} 个项目，共 {report['total']} 个；额度不可用不影响启动。")
        self.run(lambda: workspace.snapshot(paths, offset=offset), done, '正在读取项目、账户与任务…')

    def page(self, direction):
        new = max(0, self.offset + direction * 50)
        if direction > 0 and hasattr(self, 'report') and new >= self.report['total']:
            return
        self.offset = new
        self.refresh()

    def select(self, event=None):
        if not self.tree.selection():
            return
        item = self.report['projects'][int(self.tree.selection()[0])]
        self.project.set(item['cwd'])
        project = item['cwd']
        def done(git):
            self.show(json.dumps(dict(project=item, git=git), ensure_ascii=False, indent=2))
            self.status.set('项目与 worktree 状态已读取；可打开历史或提取带来源的材料。')
        self.run(lambda: workspace.worktrees(project), done, '正在只读检查 worktree…')

    def open_history(self):
        if self.project.get():
            self.open_project(self.project.get())

    def context(self, more=False):
        if more and not self.cursor:
            self.status.set('没有下一页材料。')
            return
        paths, project, query = RuntimePaths.current(), self.project.get(), self.query.get()
        cursor = self.cursor if more else None
        def done(result):
            self.cursor = result['next_cursor']
            self.show(workspace.render_context(result), append=more)
            self.status.set(f"本页 {len(result['materials'])} 条历史材料；{len(result['unavailable'])} 个来源不可读。历史记录可能已过时。")
        request = self.request + 1
        self.run(lambda: workspace.context(paths, project, query, cursor=cursor, cancel=lambda: request != self.request),
                 done, '正在提取有来源的历史上下文…')

    def copy(self):
        self.window.clipboard_clear()
        self.window.clipboard_append(self.text.get('1.0', 'end-1c'))
        self.status.set('已复制历史材料及来源。')
