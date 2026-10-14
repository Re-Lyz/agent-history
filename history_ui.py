"""History viewer and launcher: browse Codex/Claude conversations, continue one in a new
terminal with any registered command, and move conversations to the 30-day recycle bin.

Tkinter, with optional Markdown/highlighting dependencies in requirements-ui.txt.
Start with `codexui`, or: pythonw history_ui.py
"""
import contextlib
import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import webbrowser

import agent_history as ah
import trash
import ui_support as support
import markdown_preview as markdown

SHORTCUTS = {'openai1': 'codex1', 'openai2': 'codex2', 'openai3': 'codex3', 'deepseek': 'codexds'}
CLAUDE = 'claude'
KIND_LABELS = {'import': '副本', 'guardian': '审批', 'subagent': '子代理'}
MESSAGE_LIMIT = 400
LIST_PAGE = 500


from launch_backend import (file_messages, physical_row, transcript_stamp, launch_targets,
                            command_label, original_of, launch_plan, claude_copy,
                            terminal_command, start_terminal)
import launch_backend as launcher
import connections
import settings_ui
import new_conversation_ui
import launch_jobs
import conversation_store as conversations
import conversation_titles as titles
import tool_details
from tool_panel import ToolPanel


def review_candidates():
    """Only current candidates from the latest complete audit."""
    return support.review_snapshot()['candidates']


def list_rows(db, source='', query='', include_imports=False, include_internal=False, only=None, limit=500,
              project=None, sort_by='time', descending=True):
    """Filter all transcript versions and sort before applying a page limit."""
    kinds = {'main'} | ({'import'} if include_imports else set()) | \
        ({'guardian', 'subagent'} if include_internal else set())
    rows, seen = [], set()
    import setup_backend
    scope = setup_backend.load_sources()
    for raw in db.execute('''SELECT s.*, f.source AS file_source, f.path AS file_path, f.mtime AS modified
                             FROM files f JOIN sessions s ON s.key=f.session_key ORDER BY f.mtime DESC'''):
        r = dict(raw)
        r['indexed_path'] = r['path']
        r['source'], r['path'] = r.pop('file_source'), r.pop('file_path')
        if scope is not None and (r['source'] not in scope['codex'] if r['tool'] == 'codex' else
                                 str(Path(r['path']).parent.parent.parent) not in scope['claude']):
            continue
        if (r['source'], r['path']) in seen:
            continue
        seen.add((r['source'], r['path']))
        matched = False
        with contextlib.suppress(ValueError, OSError):
            r, matched = ah.physical_view(db, r, query)
        r = titles.apply(db, r)
        if only is not None:
            if str(Path(r['path'])) not in only:
                continue
        elif r['kind'] not in kinds and not (r['kind'] == 'import' and conversations.lookup(db, r)):
            continue
        if source and r['source'] != source:
            continue
        if project is not None and support.project_key(r['cwd']) != support.project_key(project):
            continue
        if query:
            text = ' '.join(str(r[k] or '') for k in ('title', 'cwd', 'id')).casefold()
            if query.casefold() not in text:
                if matched:
                    rows.append(r)
                continue
        rows.append(r)
    rows = support.sort_history(rows, sort_by, descending)
    return rows if limit is None else rows[:limit]


def project_choices(db):
    """Include every indexed project, including missing directories and hidden session kinds."""
    projects = {}
    for row in list_rows(db, include_imports=True, include_internal=True, limit=None):
        projects.setdefault(support.project_key(row['cwd']), row['cwd'] or '')
    return {'全部项目': None, **{cwd or '未记录目录': cwd for cwd in sorted(projects.values(), key=str.casefold)}}


def render_messages(rows, limit=MESSAGE_LIMIT):
    """Plain text for the viewer: tool traffic folded to one line each."""
    out = []
    shown = rows[-limit:] if limit is not None else rows
    if len(rows) > len(shown):
        out.append(('note', f'（仅显示最近 {limit} 条，共 {len(rows)} 条；可加载更早消息）\n\n'))
    for r in shown:
        role, text = r['role'], r['text'] or ''
        if role in ('tool_call', 'tool_output'):
            line = ' '.join(text.split())[:160]
            out.append(('tool', f'  ▸ {role}: {line}\n'))
            continue
        stamp = (r['ts'] or '')[:16].replace('T', ' ')
        out.append(('head_' + ('user' if role == 'user' else 'other'), f'\n{role.upper()}  {stamp}\n'))
        out.append(('body', text.rstrip() + '\n'))
    return out


# ---------------------------------------------------------------- UI


PALETTE = dict(
    bg='#f6f3ee',          # warm paper background
    card='#fffdfa',
    line='#e7e1d8',
    thumb='#d9d2c7',
    text='#3b3a37',
    muted='#8a857c',
    accent='#6b8f71',      # sage green
    accent_dark='#58795e',
    accent_soft='#e3ece2',
    user='#4f6d8f',        # soft slate blue
    assistant='#6b8f71',
    review='#fbf1dc',      # cleanup suggestions
    checked='#eef3ec',
    danger='#c0736a',      # muted terracotta
    danger_dark='#a65f57',
    code_bg='#f3efe8',
)
FONT = 'Microsoft YaHei UI' if os.name == 'nt' else 'DejaVu Sans'
CHECK_ON, CHECK_OFF = '☑', '☐'


def settle_geometry(viewport, apply, *, pause=None, include_height=False, delay=200):
    """Coalesce expensive child layout without blocking window/sash movement."""
    pending = None
    requested = applied = None
    paused = False

    def finish():
        nonlocal pending, applied, paused
        pending = None
        applied = requested
        apply(*applied)
        paused = False

    def changed(event):
        nonlocal pending, requested, paused
        size = (event.width, event.height) if include_height else (event.width,)
        if min(size) <= 1 or size == requested:
            return
        requested = size
        if pending is not None:
            viewport.after_cancel(pending)
            pending = None
        if applied is None:
            finish()
        elif requested != applied or paused:
            if pause is not None and not paused:
                pause()
                paused = True
            pending = viewport.after(delay, finish)

    viewport.bind('<Configure>', changed, add='+')


def run_ui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk
    P = PALETTE

    def style(root):
        s = ttk.Style(root)
        s.theme_use('clam')
        root.configure(bg=P['bg'])
        root.option_add('*TCombobox*Listbox.font', (FONT, 10))
        root.option_add('*TCombobox*Listbox.background', P['card'])
        root.option_add('*TCombobox*Listbox.selectBackground', P['accent_soft'])
        root.option_add('*TCombobox*Listbox.selectForeground', P['text'])
        s.configure('.', background=P['bg'], foreground=P['text'], font=(FONT, 10), borderwidth=0,
                    focuscolor=P['bg'])
        s.configure('Card.TFrame', background=P['card'])
        s.configure('Card.TLabel', background=P['card'])
        s.configure('Title.TLabel', background=P['card'], font=(FONT, 12, 'bold'))
        s.configure('Muted.TLabel', background=P['card'], foreground=P['muted'], font=(FONT, 9))
        s.configure('Bar.TLabel', foreground=P['muted'], font=(FONT, 9))
        s.configure('Section.TLabel', background=P['card'], foreground=P['muted'], font=(FONT, 9, 'bold'))
        s.configure('TCheckbutton', background=P['bg'], foreground=P['text'], indicatorbackground=P['card'],
                    indicatormargin=(0, 0, 6, 0), upperbordercolor=P['muted'], lowerbordercolor=P['muted'])
        s.map('TCheckbutton', background=[('active', P['bg'])],
              indicatorbackground=[('selected', P['accent']), ('!selected', P['card'])],
              indicatorforeground=[('selected', 'white')])
        s.configure('Card.TCheckbutton', background=P['card'])
        s.map('Card.TCheckbutton', background=[('active', P['card'])])
        for name, base, hover in (('Accent', P['accent'], P['accent_dark']),
                                  ('Danger', P['danger'], P['danger_dark'])):
            s.configure(f'{name}.TButton', background=base, foreground='white', padding=(14, 8),
                        font=(FONT, 10, 'bold'), borderwidth=0)
            s.map(f'{name}.TButton', background=[('disabled', P['line']), ('active', hover)],
                  foreground=[('disabled', P['muted'])])
        s.configure('Soft.TButton', background=P['card'], foreground=P['text'], padding=(10, 5),
                    bordercolor=P['line'], borderwidth=1, relief='solid', width=-1)
        s.configure('Danger.TButton', width=-1, padding=(12, 6))
        s.map('Soft.TButton', background=[('active', P['accent_soft'])])
        s.configure('TEntry', fieldbackground=P['card'], bordercolor=P['line'], lightcolor=P['line'],
                    darkcolor=P['line'], padding=6)
        s.map('TEntry', bordercolor=[('focus', P['accent'])], lightcolor=[('focus', P['accent'])])
        s.configure('TCombobox', fieldbackground=P['card'], background=P['card'], bordercolor=P['line'],
                    lightcolor=P['line'], darkcolor=P['line'], arrowcolor=P['muted'], padding=5)
        s.map('TCombobox', fieldbackground=[('readonly', P['card'])], selectbackground=[('readonly', P['card'])],
              selectforeground=[('readonly', P['text'])], bordercolor=[('focus', P['accent'])])
        s.configure('Treeview', background=P['card'], fieldbackground=P['card'], foreground=P['text'],
                    rowheight=30, borderwidth=0, font=(FONT, 10))
        s.map('Treeview', background=[('selected', P['accent_soft'])], foreground=[('selected', P['text'])])
        s.configure('Treeview.Heading', background=P['bg'], foreground=P['muted'], relief='flat',
                    font=(FONT, 9, 'bold'), padding=(6, 6))
        s.map('Treeview.Heading', background=[('active', P['line'])])
        s.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])
        s.configure('TNotebook', background=P['bg'], borderwidth=0, tabmargins=(0, 4, 0, 0),
                    bordercolor=P['bg'], lightcolor=P['bg'], darkcolor=P['bg'])
        s.configure('TNotebook.Tab', background=P['bg'], foreground=P['muted'], padding=(16, 7),
                    font=(FONT, 10), borderwidth=0, bordercolor=P['bg'], lightcolor=P['bg'])
        s.map('TNotebook.Tab', background=[('selected', P['accent_soft'])],
              foreground=[('selected', P['accent_dark'])], expand=[('selected', (0, 0, 0, 0))],
              bordercolor=[('selected', P['bg'])], lightcolor=[('selected', P['accent_soft'])])
        # In clam, arrowsize is also the bar's thickness; the layout below drops the arrows themselves.
        s.configure('Vertical.TScrollbar', background=P['thumb'], troughcolor=P['card'], bordercolor=P['card'],
                    arrowcolor=P['muted'], lightcolor=P['thumb'], darkcolor=P['thumb'], gripcount=0, arrowsize=11)
        s.layout('Vertical.TScrollbar', [('Vertical.Scrollbar.trough', {'sticky': 'ns', 'children': [
            ('Vertical.Scrollbar.thumb', {'expand': '1', 'sticky': 'nswe'})]})])
        s.map('Vertical.TScrollbar', background=[('active', P['muted'])], lightcolor=[('active', P['muted'])],
              darkcolor=[('active', P['muted'])])
        s.configure('TPanedwindow', background=P['bg'])
        s.configure('Sash', sashthickness=8, background=P['bg'])

    def card(parent, **pack):
        frame = tk.Frame(parent, bg=P['card'], highlightthickness=1, highlightbackground=P['line'])
        if pack:
            frame.pack(**pack)
        return frame

    class App:
        def __init__(self, root):
            self.root = root
            self.rows, self.results = {}, queue.Queue()
            self.preferences = support.load_preferences()
            self.sort_column = self.preferences['sort']['column']
            self.sort_descending = self.preferences['sort']['descending']
            self.current_iid = None
            self.list_limit = LIST_PAGE
            self.message_limit = MESSAGE_LIMIT
            self.reader_messages = []
            self.tool_states = {}
            self.reader_stamp = None
            self.jobs = {}
            self.job_serial = 0
            self.checked = set()
            self.candidates = {}
            self.target_names = {}
            self.target_labels = {}
            self.readiness = {}
            self.launch_job = None
            self.last_launch_report = None
            self.new_dialog = None
            root.title('Agent History')
            self.normal_geometry = support.fit_geometry(self.preferences['geometry'], root.winfo_screenwidth(),
                                                        root.winfo_screenheight())
            root.geometry(self.normal_geometry)
            root.minsize(min(1100, root.winfo_screenwidth() - 40), min(640, root.winfo_screenheight() - 80))
            root.protocol('WM_DELETE_WINDOW', self.close)
            root.bind('<Configure>', self.remember_geometry, add='+')
            root.report_callback_exception = self.callback_error
            self.busy = tk.StringVar(value='正在索引历史…')
            self.build()
            root.after(100, self.poll)
            root.after(1000, self.poll_launch_report)
            self.background(self.initial_load, self.after_initial, label='正在索引历史…', key='initial')
            root.bind('<Control-f>', self.focus_find)

        def remember_geometry(self, event):
            if event.widget is self.root and self.root.state() == 'normal':
                self.normal_geometry = self.root.geometry()

        def close(self):
            try:
                self.preferences.update(geometry=self.normal_geometry, maximized=self.root.state() == 'zoomed',
                                        sort=dict(column=self.sort_column, descending=self.sort_descending),
                                        preview_mode='markdown' if self.preview_mode.get() == 'Markdown' else 'raw',
                                        filters=dict(query=self.search_text(), source=self.source.get(),
                                                     project=self.project.get(),
                                                     imports=self.show_imports.get(), internal=self.show_internal.get(),
                                                     review=self.only_review.get()))
                width = self.pane.winfo_width()
                if width > 1:
                    self.preferences['panes'] = [self.pane.sashpos(i) / width for i in (0, 1)]
                support.save_preferences(self.preferences)
            except Exception as error:
                # A preference failure must not block exit with a modal dialog.
                support.log_error('Saving viewer preferences on close', error)
            finally:
                try:
                    for callback in self.root.tk.call('after', 'info'):
                        # Tk timers can contain Tcl lists, not Python command names.
                        # Cancel through Tcl; destroy() removes registered Python commands.
                        self.root.tk.call('after', 'cancel', callback)
                finally:
                    self.root.destroy()

        def show_error(self, title, error):
            support.log_error(title, error)
            messagebox.showerror(title, f'{error}\n\n错误日志：{support.error_log_path()}')

        def callback_error(self, kind, error, traceback):
            self.show_error('操作失败', error.with_traceback(traceback))

        # -------- layout
        def build(self):
            root = self.root
            top = ttk.Frame(root, padding=(16, 14, 16, 6))
            top.pack(fill='x')
            tk.Label(top, text='Agent History', bg=P['bg'], fg=P['text'],
                     font=(FONT, 15, 'bold')).pack(side='left', padx=(0, 18))
            filters = self.preferences['filters']
            self.query = tk.StringVar(value=filters.get('query', ''))
            entry = ttk.Entry(top, textvariable=self.query, width=34, name='global_search')
            entry.pack(side='left')
            entry.bind('<Return>', lambda e: self.refresh())
            self.placeholder(entry, '搜索标题、目录或对话内容，回车确认')
            self.source = tk.StringVar(value=filters.get('source', '全部来源'))
            self.source_box = ttk.Combobox(top, textvariable=self.source, width=12, state='readonly')
            self.source_box.pack(side='left', padx=10)
            self.source_box.bind('<<ComboboxSelected>>', lambda e: self.refresh())
            self.show_imports, self.show_internal, self.only_review = (tk.BooleanVar() for _ in range(3))
            for var, name in ((self.show_imports, 'imports'), (self.show_internal, 'internal'), (self.only_review, 'review')):
                var.set(filters.get(name, False))
            for text, var in (('导入副本', self.show_imports), ('审批/子代理', self.show_internal),
                              ('仅清理建议', self.only_review)):
                ttk.Checkbutton(top, text=text, variable=var, command=self.refresh).pack(side='left', padx=6)
            ttk.Button(top, text='刷新历史', style='Soft.TButton',
                       command=lambda: self.background(self.reindex, self.after_reindex,
                                                       label='正在更新历史索引…', key='reindex')).pack(side='left', padx=8)
            status_bar = ttk.Frame(root, padding=(16, 0, 16, 8))
            status_bar.pack(side='bottom', fill='x')
            ttk.Label(status_bar, textvariable=self.busy, style='Bar.TLabel').pack(side='left')

            audit_bar = ttk.Frame(root, padding=(16, 0, 16, 4))
            audit_bar.pack(fill='x')
            self.review_info = tk.StringVar(value='尚无清理报告；重新分析只生成建议，不删除对话。')
            ttk.Label(audit_bar, textvariable=self.review_info, style='Bar.TLabel').pack(side='left')
            self.audit_button = ttk.Button(audit_bar, text='重新分析清理建议', style='Soft.TButton', command=self.reanalyze)
            self.audit_button.pack(side='right')
            ttk.Button(audit_bar, text='启动配置', command=self.manage_profiles).pack(side='right', padx=8)
            ttk.Button(audit_bar, text='配置历史来源', command=self.setup_sources).pack(side='right')
            ttk.Button(audit_bar, text='新增对话', style='Accent.TButton',
                       command=self.new_conversation).pack(side='right', padx=8)

            project_bar = ttk.Frame(root, padding=(16, 0, 16, 4))
            project_bar.pack(fill='x')
            ttk.Label(project_bar, text='项目目录', style='Bar.TLabel').pack(side='left', padx=(0, 8))
            self.project = tk.StringVar(value=filters.get('project', '全部项目'))
            self.project_map = {'全部项目': None}
            self.projects_dirty = True
            self.project_box = ttk.Combobox(project_bar, textvariable=self.project, state='readonly')
            self.project_box.pack(side='left', fill='x', expand=True)
            self.project_box.bind('<<ComboboxSelected>>', lambda e: self.refresh())
            ttk.Button(project_bar, text='全部项目', style='Soft.TButton',
                       command=lambda: (self.project.set('全部项目'), self.refresh())).pack(side='right', padx=(8, 0))

            tabs = ttk.Notebook(root)
            tabs.pack(fill='both', expand=True, padx=16, pady=(4, 16))
            self.tabs = tabs
            tabs.add(self.build_history(tabs), text='对话')
            tabs.add(self.build_trash(tabs), text=f'回收站 · 保留 {trash.RETENTION_DAYS} 天')
            tabs.bind('<<NotebookTabChanged>>', lambda e: self.refresh_trash())

        def placeholder(self, entry, text):
            def show(_=None):
                if not self.query.get():
                    entry.configure(foreground=P['muted'])
                    self.query.set(text)
            def hide(_=None):
                if self.query.get() == text:
                    self.query.set('')
                    entry.configure(foreground=P['text'])
            self.placeholder_text = text
            entry.bind('<FocusIn>', hide, add='+')
            entry.bind('<FocusOut>', show, add='+')
            show()

        def search_text(self):
            value = self.query.get().strip()
            return '' if value == self.placeholder_text else value

        def build_history(self, parent):
            pane = ttk.PanedWindow(parent, orient='horizontal')

            # list card with batch toolbar
            left = card(pane)
            bar = tk.Frame(left, bg=P['card'])
            bar.pack(fill='x', padx=10, pady=(10, 4))
            ttk.Button(bar, text='全选', style='Soft.TButton', command=self.check_all).pack(side='left')
            ttk.Button(bar, text='选清理建议', style='Soft.TButton',
                       command=self.check_candidates).pack(side='left', padx=6)
            ttk.Button(bar, text='清空', style='Soft.TButton', command=self.check_none).pack(side='left')
            self.check_count = tk.StringVar(value='')
            self.batch_button = ttk.Button(bar, text='批量删除', style='Danger.TButton', command=self.delete_checked)
            self.batch_button.pack(side='right')
            ttk.Label(bar, textvariable=self.check_count, style='Muted.TLabel').pack(side='right', padx=8)
            self.batch_button.state(['disabled'])

            body = tk.Frame(left, bg=P['card'])
            body.pack(fill='both', expand=True, padx=(4, 0), pady=(0, 6))
            cols = ('check', 'time', 'source', 'turns', 'title')
            self.tree = ttk.Treeview(body, columns=cols, show='headings', selectmode='browse')
            for col, text, width, anchor in (('check', '', 34, 'center'), ('time', '时间', 96, 'w'),
                                             ('source', '来源', 78, 'w'), ('turns', '轮', 40, 'center'),
                                             ('title', '标题', 320, 'w')):
                self.tree.heading(col, text=text, anchor=anchor)
                if col != 'check':
                    self.tree.heading(col, command=lambda column=col: self.sort_list(column))
                self.tree.column(col, width=width, minwidth=width if col == 'check' else 30,
                                 stretch=col == 'title', anchor=anchor)
            self.tree.heading('check', text=CHECK_OFF, command=self.toggle_all)
            self.tree.tag_configure('review', background=P['review'])
            self.tree.tag_configure('checked', background=P['checked'])
            scroll = ttk.Scrollbar(body, command=self.tree.yview)
            self.tree.configure(yscrollcommand=scroll.set)
            scroll.pack(side='right', fill='y')      # packed first so the expanding list cannot squeeze it out
            self.tree.pack(side='left', fill='both', expand=True)
            self.tree.bind('<<TreeviewSelect>>', lambda e: self.on_select())
            self.tree.bind('<F2>', lambda e: self.rename_current())
            self.tree.bind('<Button-1>', self.on_click, add='+')
            self.tree.bind('<space>', lambda e: self.toggle(self.tree.focus()) or 'break')
            hint = '点左侧方框勾选，Shift 点击可连续勾选；空格键勾选当前行。黄色为清理建议。'
            ttk.Label(left, text=hint, style='Muted.TLabel').pack(anchor='w', padx=12, pady=(0, 8))
            self.list_count = tk.StringVar()
            footer = tk.Frame(left, bg=P['card'])
            footer.pack(fill='x', padx=12, pady=(0, 8))
            self.more_rows = ttk.Button(footer, text='加载更多对话', style='Soft.TButton', command=self.load_more_rows)
            self.more_rows.pack(side='right')
            ttk.Label(footer, textvariable=self.list_count, style='Muted.TLabel').pack(side='left')
            pane.add(left, weight=3)

            # conversation card
            middle = card(pane)
            reader_bar = tk.Frame(middle, bg=P['card'])
            reader_bar.pack(side='top', fill='x', padx=8, pady=(8, 4))
            self.find_query = tk.StringVar()
            self.find_entry = ttk.Entry(reader_bar, textvariable=self.find_query, width=12, name='conversation_find')
            self.find_entry.pack(side='left', fill='x', expand=True)
            self.find_entry.bind('<Return>', lambda e: self.find_message(1))
            self.find_entry.bind('<Shift-Return>', lambda e: self.find_message(-1))
            self.find_query.trace_add('write', lambda *args: self.highlight_matches())
            ttk.Button(reader_bar, text='上一处', style='Soft.TButton', command=lambda: self.find_message(-1)).pack(side='left')
            ttk.Button(reader_bar, text='下一处', style='Soft.TButton', command=lambda: self.find_message(1)).pack(side='left')
            self.find_status = tk.StringVar(value='Ctrl+F 搜索此对话')
            ttk.Label(reader_bar, textvariable=self.find_status, style='Muted.TLabel').pack(side='left', padx=4)
            format_bar = tk.Frame(middle, bg=P['card'])
            format_bar.pack(side='top', fill='x', padx=8, pady=(0, 4))
            enabled = markdown.available()
            preferred = 'Markdown' if self.preferences.get('preview_mode', 'markdown') == 'markdown' else '原文'
            self.preview_mode = tk.StringVar(value=preferred if enabled else '原文')
            mode_box = ttk.Combobox(format_bar, textvariable=self.preview_mode, state='readonly', width=10,
                                    values=('Markdown', '原文') if enabled else ('原文',), name='preview_mode')
            mode_box.pack(side='left')
            mode_box.bind('<<ComboboxSelected>>', self.change_preview_mode)
            self.render_info = tk.StringVar(value='链接点击打开 · 图片仅显示说明' if enabled else
                                           'Markdown 依赖未安装；请按 README 安装，当前可阅读原文。')
            ttk.Label(format_bar, textvariable=self.render_info, style='Muted.TLabel',
                      wraplength=280).pack(side='left', padx=8)
            reader_footer = tk.Frame(middle, bg=P['card'])
            reader_footer.pack(side='bottom', fill='x', padx=8, pady=6)
            self.message_count = tk.StringVar()
            self.more_messages = ttk.Button(reader_footer, text='加载更早消息', style='Soft.TButton', command=self.load_earlier)
            self.more_messages.pack(side='right')
            ttk.Label(reader_footer, textvariable=self.message_count, style='Muted.TLabel').pack(side='left')
            # Keep the reader's size steady during a drag. Tk otherwise reflows
            # long Markdown paragraphs on every pixel of window/pane movement.
            reader_viewport = tk.Frame(middle, bg=P['card'])
            self.text = tk.Text(reader_viewport, wrap='word', state='disabled', font=(FONT, 10), bg=P['card'],
                                fg=P['text'], relief='flat', padx=22, pady=16, spacing1=2, spacing3=4,
                                highlightthickness=0, insertbackground=P['text'], selectbackground=P['accent_soft'])
            self.text.tag_configure('head_user', foreground=P['user'], font=(FONT, 9, 'bold'), spacing1=14)
            self.text.tag_configure('head_other', foreground=P['assistant'], font=(FONT, 9, 'bold'), spacing1=14)
            self.text.tag_configure('body', lmargin1=2, lmargin2=2, spacing2=3)
            self.text.tag_configure('tool', foreground=P['muted'], font=('Consolas', 9), background=P['code_bg'],
                                    lmargin1=10, lmargin2=10)
            self.text.tag_configure('note', foreground=P['muted'], justify='center')
            self.text.tag_configure('empty', foreground=P['muted'], justify='center', spacing1=120)
            self.text.tag_configure('find', background='#f8e5ab')
            self.text.tag_configure('find_current', background='#e9be62')
            self.markdown_renderer = markdown.TkRenderer(self.text, palette=P, font=FONT,
                                                          copy_code=self.copy_code, open_link=self.open_link)
            self.message_marks = []
            hscroll = ttk.Scrollbar(middle, orient='horizontal', command=self.text.xview)
            self.text.configure(xscrollcommand=hscroll.set)
            hscroll.pack(side='bottom', fill='x')
            tscroll = ttk.Scrollbar(middle, command=self.text.yview)
            self.text.configure(yscrollcommand=tscroll.set)
            tscroll.pack(side='right', fill='y')
            reader_viewport.pack(side='left', fill='both', expand=True)
            self.text.place(x=0, y=0, width=1, height=1)

            def pause_reader():
                # Even moving a mapped Text on Windows can redraw its long lines.
                # Only large previews need to pause painting until the drag ends;
                # the widget retains its text, tags, selection and reading position.
                count = self.text.count('1.0', 'end-1c', 'chars')
                if count and count[0] >= 50_000:
                    self.text.place_forget()

            settle_geometry(reader_viewport,
                            lambda width, height: self.text.place(x=0, y=0, width=width, height=height),
                            pause=pause_reader, include_height=True)
            pane.add(middle, weight=4)

            # action card
            right = card(pane)
            canvas = tk.Canvas(right, bg=P['card'], highlightthickness=0, width=290, height=1,
                               yscrollincrement=18)
            rscroll = ttk.Scrollbar(right, command=canvas.yview)
            canvas.configure(yscrollcommand=rscroll.set)
            rscroll.pack(side='right', fill='y')
            canvas.pack(side='left', fill='both', expand=True)
            inner = tk.Frame(canvas, bg=P['card'], padx=18, pady=16)
            window = canvas.create_window(0, 0, window=inner, anchor='nw')
            self.action_canvas = canvas
            self.title_var, self.info = tk.StringVar(value='选择一条对话'), tk.StringVar()
            ttk.Button(inner, text='修改对话名称', command=self.rename_current).pack(anchor='e')
            ttk.Label(inner, textvariable=self.title_var, style='Title.TLabel', wraplength=290,
                      justify='left').pack(anchor='w', fill='x')
            ttk.Label(inner, textvariable=self.info, style='Muted.TLabel', wraplength=290,
                      justify='left').pack(anchor='w', fill='x', pady=(6, 0))
            tk.Frame(inner, bg=P['line'], height=1).pack(fill='x', pady=16)
            ttk.Label(inner, text='启动命令', style='Section.TLabel').pack(anchor='w')
            self.target = tk.StringVar()
            self.target_box = ttk.Menubutton(inner, textvariable=self.target, name='launch_target')
            self.target_menu = tk.Menu(self.target_box, tearoff=False)
            self.target_box.configure(menu=self.target_menu)
            self.target_box.pack(fill='x', pady=(4, 12))
            self.version_value = tk.StringVar(value='当前版本')
            self.version_box = ttk.Combobox(inner, textvariable=self.version_value, state='readonly', name='conversation_version')
            self.version_box.pack(fill='x', pady=(0, 8))
            self.version_box.bind('<<ComboboxSelected>>', self.select_version)
            self.version_options = {}
            self.launch_status = tk.StringVar()
            ttk.Label(inner, textvariable=self.launch_status, style='Muted.TLabel', wraplength=280,
                      justify='left').pack(fill='x', pady=(0, 8))
            ttk.Label(inner, text='工作目录', style='Section.TLabel').pack(anchor='w')
            row = tk.Frame(inner, bg=P['card'])
            row.pack(fill='x', pady=(4, 12))
            self.cwd = tk.StringVar()
            self.cwd.trace_add('write', lambda *a: self.update_preview())
            ttk.Button(row, text='浏览', style='Soft.TButton', command=self.browse).pack(side='right', padx=(6, 0))
            ttk.Entry(row, textvariable=self.cwd).pack(side='left', fill='x', expand=True)
            ttk.Label(inner, text='将执行', style='Section.TLabel').pack(anchor='w')
            self.preview = tk.StringVar()
            self.preview_label = tk.Label(inner, textvariable=self.preview, wraplength=280, justify='left',
                                          anchor='w', font=('Consolas', 9), bg=P['code_bg'], fg=P['text'],
                                          padx=10, pady=8)
            self.preview_label.pack(fill='x', pady=(4, 14))
            actions = tk.Frame(inner, bg=P['card'])
            actions.pack(fill='x', pady=(0, 8))
            self.copy_button = ttk.Button(actions, text='复制预览', style='Soft.TButton', command=self.copy_preview)
            self.copy_button.pack(side='left')
            self.copy_button.state(['disabled'])
            ttk.Button(actions, text='检查环境', style='Soft.TButton', command=self.check_environment).pack(side='right')
            self.launch_button = ttk.Button(inner, text='打开终端并继续', style='Accent.TButton', command=self.launch)
            self.launch_button.pack(fill='x')
            ttk.Button(inner, text='测试启动（不调用模型）', command=self.test_launch).pack(fill='x', pady=(6, 0))
            ttk.Button(inner, text='重试启动', command=self.launch).pack(fill='x', pady=(6, 0))
            ttk.Button(inner, text='重试同步／刷新续聊', command=self.retry_handoff).pack(fill='x', pady=(6, 0))
            ttk.Button(inner, text='保留分支并完成同步', command=lambda: self.retry_handoff(allow_branch=True)).pack(fill='x', pady=(6, 0))
            ttk.Button(inner, text='启动记录', command=self.show_launch_reports).pack(fill='x', pady=(6, 0))
            ttk.Button(inner, text='查看错误日志', style='Soft.TButton', command=self.open_log).pack(fill='x', pady=(6, 0))
            tk.Frame(inner, bg=P['line'], height=1).pack(fill='x', pady=18)
            self.with_copies = tk.BooleanVar(value=True)
            ttk.Checkbutton(inner, text='同时删除关联副本', variable=self.with_copies,
                            style='Card.TCheckbutton').pack(anchor='w')
            ttk.Label(inner, text='包括导入到其他连接或 Claude 的副本。',
                      style='Muted.TLabel').pack(fill='x', pady=(4, 0))
            self.delete_button = ttk.Button(inner, text='删除这条对话', style='Soft.TButton', command=self.delete_current)
            self.delete_button.pack(fill='x', pady=(8, 0))
            ttk.Label(inner, text=f'删除后在回收站保留 {trash.RETENTION_DAYS} 天，可随时恢复。',
                      style='Muted.TLabel').pack(anchor='w', pady=(8, 0))
            pane.add(right, weight=2)
            # Labels in the action card wrap to its current width.
            wrapped = [w for w in inner.winfo_children() if isinstance(w, (ttk.Label, tk.Label))]
            region_job = None
            wrap_width = last_region = None

            def sync_region():
                nonlocal region_job, last_region
                region_job = None
                region = canvas.bbox('all')
                if region != last_region:
                    canvas.configure(scrollregion=region)
                    last_region = region
                if inner.winfo_reqheight() <= canvas.winfo_height() and canvas.yview()[0] != 0:
                    canvas.yview_moveto(0)

            def schedule_region(_event=None):
                nonlocal region_job
                # Height changes after wrapping must not rewrap every label again.
                # Keep just one measurement queued after Tk's geometry pass.
                if region_job is None:
                    region_job = canvas.after_idle(sync_region)

            def apply_width(width):
                nonlocal wrap_width
                canvas.itemconfigure(window, width=width)
                width = max(width - 48, 40)
                if width != wrap_width:
                    for label in wrapped:
                        label.configure(wraplength=width)
                    wrap_width = width
                schedule_region()

            inner.bind('<Configure>', schedule_region)
            settle_geometry(canvas, apply_width)
            canvas.bind('<Configure>', schedule_region, add='+')
            wheel_tag = 'ActionWheel_' + str(canvas).replace('.', '_')

            def wheel(event):
                if inner.winfo_reqheight() > canvas.winfo_height() and (event.delta or event.num in (4, 5)):
                    units = (-1 if event.num == 4 else 1) if event.num in (4, 5) else \
                        (-1 if event.delta > 0 else 1) * max(1, abs(event.delta) // 120)
                    canvas.yview_scroll(units, 'units')
                return 'break'

            # A private bind tag precedes widget classes, so entries/comboboxes scroll
            # this pane without changing the selected command or affecting other panes.
            for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
                canvas.bind_class(wheel_tag, sequence, wheel)
            pending = [canvas, rscroll]
            while pending:
                widget = pending.pop()
                widget.bindtags((wheel_tag, *widget.bindtags()))
                pending.extend(widget.winfo_children())
            self.pane = pane
            return pane

        def build_trash(self, parent):
            outer = card(parent)
            bar = tk.Frame(outer, bg=P['card'])
            bar.pack(fill='x', padx=12, pady=(12, 6))
            self.restore_button = ttk.Button(bar, text='恢复所选', style='Accent.TButton', command=self.restore)
            self.restore_button.pack(side='left')
            self.purge_button = ttk.Button(bar, text='立即永久删除', style='Soft.TButton', command=self.purge_now)
            self.purge_button.pack(side='left', padx=8)
            ttk.Label(bar, text='到期条目会自动永久删除。可按住 Ctrl / Shift 多选。',
                      style='Muted.TLabel').pack(side='right')
            detail_body = tk.Frame(outer, bg=P['card'])
            detail_body.pack(side='bottom', fill='x', padx=12, pady=(0, 12))
            self.trash_details = tk.Text(detail_body, height=7, wrap='word', state='disabled', bg=P['code_bg'],
                                         fg=P['text'], font=(FONT, 10), relief='flat', padx=12, pady=8)
            detail_scroll = ttk.Scrollbar(detail_body, command=self.trash_details.yview)
            self.trash_details.configure(yscrollcommand=detail_scroll.set)
            detail_scroll.pack(side='right', fill='y')
            self.trash_details.pack(side='left', fill='both', expand=True)
            body = tk.Frame(outer, bg=P['card'])
            body.pack(fill='both', expand=True, padx=(6, 0), pady=(0, 10))
            cols = ('deleted', 'expires', 'remaining', 'source', 'count', 'label')
            self.trash_tree = ttk.Treeview(body, columns=cols, show='headings', selectmode='extended')
            for col, text, width in (('deleted', '删除时间', 140), ('expires', '永久删除于', 140),
                                     ('remaining', '剩余保留', 90), ('source', '来源', 100),
                                     ('count', '会话 / 副本', 210), ('label', '对话', 300)):
                self.trash_tree.heading(col, text=text, anchor='w')
                self.trash_tree.column(col, width=width, stretch=col == 'label')
            self.trash_tree.tag_configure('soon', foreground=P['danger'])
            scroll = ttk.Scrollbar(body, command=self.trash_tree.yview)
            self.trash_tree.configure(yscrollcommand=scroll.set)
            scroll.pack(side='right', fill='y')
            self.trash_tree.pack(side='left', fill='both', expand=True)
            self.trash_manifests, self.trash_imported_keys = {}, set()
            self.trash_restore_report = ''
            self.trash_tree.bind('<<TreeviewSelect>>', lambda e: self.show_trash_details())
            return outer

        # -------- background work
        def background(self, work, done=None, *, label='处理中…', key=None):
            if key and any(job[0] == key for job in self.jobs.values()):
                return  # repeated clicks do not start the same operation again
            self.job_serial += 1
            serial = self.job_serial
            self.jobs[serial] = (key, label)
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
                    self.busy.set('')
                    try:
                        if error:
                            self.show_error('操作失败', error)
                        elif done:
                            done(result)
                    except Exception as callback_error:
                        self.show_error('操作失败', callback_error)
                    self.audit_button.state(['disabled'] if any(j[0] == 'audit' for j in self.jobs.values()) else ['!disabled'])
                    restoring = any(j[0] == 'restore' for j in self.jobs.values())
                    for button in (self.restore_button, self.purge_button):
                        button.state(['disabled'] if restoring else ['!disabled'])
                    self.update_preview()
                    if completed_job[0] == 'test-launch' and not error:
                        self.launch_status.set(result['message'])
                    if self.launch_job and completed_job[0] in ('launch', 'handoff-sync'):
                        with contextlib.suppress(ValueError, OSError):
                            self.launch_status.set(launch_jobs.describe(launch_jobs.read(self.launch_job)))
                    if self.jobs:
                        self.busy.set(self.jobs[max(self.jobs)][1])
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
            targets = launch_targets()
            readiness = {target: support.launch_readiness(target) for target in targets}
            return targets, snapshot, readiness

        def after_initial(self, result):
            targets, snapshot, readiness = result
            self.apply_readiness(readiness)
            registry = connections.load(ah.ROOT)
            self.source_box['values'] = ['全部来源'] + list(registry['connections']) + [CLAUDE]
            if self.source.get() not in self.source_box['values']:
                self.source.set('全部来源')
            self.after_reindex(snapshot)
            if (not self.tree.get_children() or not (ah.ROOT / 'connections.json').exists()) and not getattr(self, 'setup_prompted', False):
                self.setup_prompted = True
                self.busy.set('暂无历史，请通过配置向导选择本机历史目录。')
                self.setup_sources()

        def manage_profiles(self):
            settings_ui.ProfileManager(self.root, self.settings_changed)

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
                    start_terminal(prepared['cwd'], prepared['inner'], env=prepared['env'])
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

        def setup_sources(self):
            settings_ui.SetupWizard(self.root, self.settings_changed)

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
            self.target_labels = {target: command_label(target) + (' · 未就绪' if not info['ready'] else '')
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
            targets = list(self.readiness) or launch_targets()

            def done(readiness):
                self.apply_readiness(readiness)
                self.update_preview()
                self.busy.set('启动环境已重新检查')

            self.background(lambda: {target: support.launch_readiness(target) for target in targets},
                            done, label='正在检查启动环境…', key='diagnostics')

        def target_changed(self, event=None):
            target = self.target_names.get(self.target.get())
            if target:
                self.preferences['default_target'] = target
                rows = self.selected()
                project = support.project_key(rows[0]['cwd']) if rows else ''
                if project:
                    self.preferences['project_targets'][project] = target
            self.update_preview()

        def copy_preview(self):
            self.root.clipboard_clear()
            self.root.clipboard_append(self.preview.get())
            self.busy.set('命令预览已复制')

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
                    report = launch_jobs.read(self.launch_job)
                    if report != self.last_launch_report:
                        self.last_launch_report = report
                        self.launch_status.set(launch_jobs.describe(report))
                        if report.get('sync_state') == 'collected' and report['state'] in ('exited', 'failed'):
                            self.background(self.reindex, self.after_reindex, label='正在读取续聊内容…', key='handoff-sync')
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
                    identity = None if allow_branch else latest_job
                    if selected:
                        member = conversations.lookup(db, selected)
                        owner = db.execute('SELECT id FROM temporary_handoffs WHERE temporary_version=? OR durable_version=?',
                                           (member['id'], member['id'])).fetchone() if member else None
                        if owner:
                            identity = owner['id']
                    if allow_branch and not identity:
                        raise ValueError('请先选择要保存的临时续聊分支。')
                    outcome = handoff_lifecycle.retry_pending(db, manual=True, allow_branch=allow_branch, identity=identity)
                return outcome, self.reindex(), identity
            def done(result):
                outcome, snapshot, identity = result
                if identity:
                    self.launch_job = identity
                self.after_reindex(snapshot)
                message = f"同步完成 {outcome['completed']} 项；仍待处理 {outcome['pending']} 项，详情见启动记录。"
                self.launch_status.set(message)
                self.busy.set(message)
            self.background(work, done, label='正在保存续聊并清理临时副本…', key='handoff-sync')

        # -------- history list and batch selection
        def sort_list(self, column):
            self.sort_descending = not self.sort_descending if self.sort_column == column else column == 'time'
            self.sort_column = column
            if hasattr(self, 'all_rows'):
                selected = next(iter(self.tree.selection()), None)
                rows = support.sort_history(self.all_rows, self.sort_column, self.sort_descending)
                self.display_rows(rows, selected, self.tree.yview()[0])
            else:
                self.refresh()

        def refresh(self):
            self.list_request = getattr(self, 'list_request', 0) + 1
            request = self.list_request
            if self.search_text():
                source = '' if self.source.get() == '全部来源' else self.source.get()
                query = self.search_text()
                imports, internal = self.show_imports.get(), self.show_internal.get()
                only = set(self.candidates) if self.only_review.get() else None
                project = self.project_map.get(self.project.get())
                column, descending = self.sort_column, self.sort_descending
                selected = next(iter(self.tree.selection()), None)
                position = self.tree.yview()[0]
                self.list_limit = LIST_PAGE
                search_filters = (source, query, imports, internal, project, tuple(sorted(only or [])))
                if search_filters == getattr(self, 'search_filters', None):
                    self.list_limit = getattr(self, 'search_list_limit', LIST_PAGE)
                self.search_filters = search_filters
                self.search_list_limit = self.list_limit
                def search():
                    with contextlib.closing(ah.connect()) as db:
                        rows = list_rows(db, source, query, imports, internal, only, limit=None,
                                         project=project, sort_by=column, descending=descending)
                        # Keep the matching physical version: grouping must not
                        # replace a search hit with a different branch's history.
                        return rows
                def done(rows):
                    if request == self.list_request:
                        self.display_rows(support.sort_history(rows, self.sort_column, self.sort_descending), selected, position)
                self.background(search, done, label='正在搜索历史…')
                return
            self.version_override = None
            self.version_iid = None
            selected = self.tree.selection()
            selected = selected[0] if selected else None
            selected_row = self.rows.get(selected)
            list_position = self.tree.yview()[0]
            source = '' if self.source.get() == '全部来源' else self.source.get()
            only = set(self.candidates) if self.only_review.get() else None
            with contextlib.closing(ah.connect()) as db:
                selected_member = conversations.lookup(db, selected_row) if selected_row else None
                if self.projects_dirty:
                    self.project_map = project_choices(db)
                    self.project_box['values'] = list(self.project_map)
                    if self.project.get() not in self.project_map:
                        match = next((label for label, path in self.project_map.items() if path is not None and
                                      support.project_key(path) == support.project_key(self.project.get())), '全部项目')
                        self.project.set(match)
                    self.projects_dirty = False
                filters = (source, self.search_text(), self.show_imports.get(), self.show_internal.get(),
                           self.only_review.get(), self.project.get())
                if filters != getattr(self, 'previous_filters', None):
                    self.list_limit = LIST_PAGE
                self.previous_filters = filters
                rows = list_rows(db, source, self.search_text(), self.show_imports.get(),
                                 self.show_internal.get(), only, limit=None,
                                 project=self.project_map.get(self.project.get()),
                                 sort_by=self.sort_column, descending=self.sort_descending)
                if not self.show_imports.get() and only is None:
                    rows = conversations.group_rows(db, rows)
                    if selected_member:
                        replacement = next((row for row in rows if row.get('conversation') == selected_member['conversation']), None)
                        if replacement:
                            selected = f"{replacement['source']}|{replacement['path']}"
                elif selected_member:
                    owner = db.execute("SELECT durable_version FROM temporary_handoffs WHERE temporary_version=? AND state='cleaned'",
                                       (selected_member['id'],)).fetchone()
                    if owner:
                        durable = db.execute('SELECT row_json FROM conversation_versions WHERE id=?', (owner['durable_version'],)).fetchone()
                        native = json.loads(durable['row_json']) if durable else None
                        replacement = next((row for row in rows if native and row['path'] == native['path']), None)
                        if replacement:
                            selected = f"{replacement['source']}|{replacement['path']}"
            rows = support.sort_history(rows, self.sort_column, self.sort_descending)
            self.display_rows(rows, selected, list_position)

        def display_rows(self, rows, selected, list_position):
            self.all_rows = rows
            order = (self.sort_column, self.sort_descending)
            if order != getattr(self, 'previous_sort', order):
                keep = self.checked | ({selected} if selected else set())
                last = max((index + 1 for index, row in enumerate(rows)
                            if f"{row['source']}|{row['path']}" in keep), default=0)
                self.list_limit = max(self.list_limit, last)
            self.previous_sort = order
            total = len(rows)
            has_more = len(rows) > self.list_limit
            rows = rows[:self.list_limit]
            self.more_rows.state(['!disabled'] if has_more else ['disabled'])
            self.list_count.set(f'已显示 {len(rows)} / {total} 条' + (' · 还有更多' if has_more else ' · 已全部显示'))
            for column, title in dict(time='时间', source='来源', turns='轮', title='标题').items():
                arrow = (' ▼' if self.sort_descending else ' ▲') if column == self.sort_column else ''
                self.tree.heading(column, text=title + arrow)
            visible = {f"{r['source']}|{r['path']}" for r in rows}
            for iid in set(self.tree.get_children()) - visible:
                self.tree.delete(iid)
            self.rows = {}
            for index, r in enumerate(rows):
                iid = f"{r['source']}|{r['path']}"
                self.rows[iid] = r
                when = datetime.datetime.fromtimestamp(r['modified']).strftime('%m-%d %H:%M')
                flag = KIND_LABELS.get(r['kind'])
                if r.get('logical'):
                    flag = str(r['version_count']) + ' 个版本 / ' + str(r['branches']) + ' 个分支'
                title = (r['title'] or r['cwd'] or r['id']).replace('\n', ' ')[:120]
                values = (CHECK_OFF, when, r['source'], r['turns'], f'[{flag}] {title}' if flag else title)
                if self.tree.exists(iid):
                    self.tree.item(iid, values=values)
                    self.tree.move(iid, '', index)
                else:
                    self.tree.insert('', index, iid=iid, values=values)
            self.checked &= set(self.rows)      # keep ticks that are still visible
            for iid in self.rows:
                self.paint(iid)
            self.busy.set(f'{len(rows)} 条')
            self.update_checks()
            if selected in self.rows:
                self.tree.selection_set(selected)
                self.tree.focus(selected)
                self.tree.yview_moveto(list_position)
            self.on_select()

        def paint(self, iid):
            on = iid in self.checked
            self.tree.set(iid, 'check', CHECK_ON if on else CHECK_OFF)
            review = str(Path(self.rows[iid]['path'])) in self.candidates
            self.tree.item(iid, tags=('checked',) if on else ('review',) if review else ())

        def toggle(self, iid, state=None):
            if iid not in self.rows:
                return
            on = (iid not in self.checked) if state is None else state
            (self.checked.add if on else self.checked.discard)(iid)
            self.paint(iid)
            self.last_toggled = iid
            self.update_checks()

        def on_click(self, event):
            if self.tree.identify_region(event.x, event.y) != 'cell' or self.tree.identify_column(event.x) != '#1':
                return
            iid = self.tree.identify_row(event.y)
            anchor = getattr(self, 'last_toggled', None)
            if event.state & 0x1 and anchor in self.rows:
                # Shift-click: tick the whole range to the same state as the anchor.
                items = self.tree.get_children()
                a, b = sorted((items.index(anchor), items.index(iid)))
                state = anchor in self.checked
                for item in items[a:b + 1]:
                    (self.checked.add if state else self.checked.discard)(item)
                    self.paint(item)
                self.update_checks()
            else:
                self.toggle(iid)
            return 'break'

        def set_checked(self, iids):
            self.checked = set(iids)
            for iid in self.rows:
                self.paint(iid)
            self.update_checks()

        def check_all(self):
            self.set_checked(self.rows)

        def check_none(self):
            self.set_checked(())

        def toggle_all(self):
            self.check_none() if self.checked and self.checked >= set(self.rows) else self.check_all()

        def check_candidates(self):
            self.set_checked(i for i, r in self.rows.items() if str(Path(r['path'])) in self.candidates)

        def update_checks(self):
            n = len(self.checked)
            self.check_count.set(f'已勾选 {n}' if n else '')
            self.batch_button.configure(text=f'删除 {n} 条' if n else '批量删除')
            self.batch_button.state(['!disabled'] if n else ['disabled'])
            full = n and self.checked >= set(self.rows)
            self.tree.heading('check', text=CHECK_ON if full else CHECK_OFF)

        def selected(self):
            selected = [self.rows[i] for i in self.tree.selection() if i in self.rows]
            if len(selected) == 1 and getattr(self, 'version_override', None) and getattr(self, 'version_iid', None) == self.tree.selection()[0]:
                return [self.version_override]
            return selected

        # -------- detail pane
        def rename_current(self):
            rows = self.selected()
            if len(rows) != 1:
                return
            row = rows[0]
            value = simpledialog.askstring('修改对话名称',
                '修改整条逻辑对话的显示名称；留空恢复自动标题。\n名称保存在本工具中，原生 Agent 名称保持独立。',
                initialvalue=row.get('title') or '', parent=self.root)
            if value is None:
                return
            def work():
                with trash.locked_db(reindex=False) as db:
                    return titles.rename(db, row, value)
            self.background(work, lambda result: self.refresh(), label='保存对话名称…', key='rename')

        def load_more_rows(self):
            self.list_limit += LIST_PAGE
            self.search_list_limit = self.list_limit
            self.refresh()

        def focus_find(self, event=None):
            self.find_entry.focus_set()
            self.find_entry.selection_range(0, 'end')
            return 'break'

        def load_earlier(self):
            anchor = self.reader_anchor()
            self.message_limit += MESSAGE_LIMIT
            self.show_messages(anchor=(anchor, False))

        def reader_anchor(self):
            top = self.text.index('@0,0')
            marks = [(index, name) for index, name in self.message_marks if self.text.compare(name, '<=', top)]
            if not marks:
                return None
            index, name = marks[-1]
            count = self.text.count(name, top, 'indices')
            offset = count[0] if count else 0
            following = next((mark for number, mark in self.message_marks if number > index), 'end-1c')
            count = self.text.count(name, following, 'indices')
            length = count[0] if count else 1
            return index, offset, offset / max(1, length)

        def restore_reader_anchor(self, anchor, mode_changed=False):
            if anchor is None:
                self.text.yview_moveto(0)
                return
            index, offset, fraction = anchor
            mark = next((name for number, name in self.message_marks if number == index), None)
            if mark:
                following = next((name for number, name in self.message_marks if number > index), 'end-1c')
                count = self.text.count(mark, following, 'indices')
                length = count[0] if count else 0
                offset = round(fraction * length) if mode_changed else min(offset, max(0, length - 1))
                self.text.yview(f'{mark}+{offset}c')

        def change_preview_mode(self, event=None):
            anchor = self.reader_anchor()
            self.show_messages(anchor=(anchor, True))

        def copy_code(self, code):
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.render_info.set('代码已复制')

        def open_link(self, url):
            if markdown.clickable_url(url):
                webbrowser.open(url)
            return 'break'

        def show_messages(self, position=None, prepared=None, request=None, anchor=None):
            if request is not None and request != getattr(self, 'render_request', 0):
                return
            if request is None:
                self.render_request = getattr(self, 'render_request', 0) + 1
            request = self.render_request
            count = len(self.reader_messages)
            shown = min(count, self.message_limit)
            large = sum(len(row['text'] or '') for row in self.reader_messages[-shown:]) > 50_000
            if large and prepared is None and self.preview_mode.get() == 'Markdown':
                messages = list(self.reader_messages)
                def prepare():
                    result, highlights = {}, {}
                    for index in range(count - shown, count):
                        row = messages[index]
                        if row['role'] in ('user', 'assistant', 'agent'):
                            elements = markdown.parse(row['text'] or '')
                            result[index] = elements
                            for element in elements:
                                if isinstance(element, markdown.Code):
                                    key = (element.text, element.language)
                                    highlights[key] = tuple(markdown.highlighted(*key))
                    return result, highlights
                self.background(prepare, lambda result: self.show_messages(position, result, request, anchor), label='准备对话排版…')
                return
            self.message_count.set(f'已显示 {shown} / {count} 条消息 · 工具可展开详情')
            self.more_messages.state(['!disabled'] if shown < count else ['disabled'])
            self.begin_text()
            if shown < count:
                self.text.insert('end', f'（仅显示最近 {shown} 条，共 {count} 条；可加载更早消息）\n\n', 'note')
            groups = tool_details.groups(self.reader_messages[count - shown:])
            def render():
                for index in range(count - shown, count):
                    row = self.reader_messages[index]
                    mark = f'message_{index}'
                    self.text.mark_set(mark, 'end-1c')
                    self.text.mark_gravity(mark, 'left')
                    self.message_marks.append((index, mark))
                    if row['role'] in ('tool_call', 'tool_output') and self.preview_mode.get() == 'Markdown':
                        paired = groups.get(index - (count - shown))
                        if paired is not None:
                            rows = [self.reader_messages[number + count - shown] for number in paired]
                            locator = row.get('detail', {})
                            key = str((locator.get('path') or self.current_iid, locator.get('ordinal', index), row['role']))
                            panel = ToolPanel(self, rows, key, self.tool_states, P)
                            panel.mark = mark
                            self.tool_panels.append(panel)
                            self.markdown_renderer.widgets.append(panel.frame)
                            self.text.window_create('end', window=panel.frame, padx=4, pady=4)
                            self.text.insert('end', '\n', 'tool')
                    else:
                        for tag, chunk in render_messages([row], None):
                            if tag == 'body' and self.preview_mode.get() == 'Markdown':
                                if prepared:
                                    yield from self.markdown_renderer.iter_render(row['text'] or '',
                                        prepared[0].get(index), prepared[1])
                                else:
                                    self.markdown_renderer.render(row['text'] or '')
                                self.text.insert('end', '\n', 'body')
                            else:
                                self.text.insert('end', chunk, tag)
                    yield
                if not count:
                    self.text.insert('end', '这条对话没有可显示的文字', 'empty')

            iterator = render()
            def batch():
                if request != self.render_request:
                    return
                self.text.configure(state='normal')
                deadline = time.perf_counter() + 0.008
                try:
                    while True:
                        next(iterator)
                        if large and time.perf_counter() >= deadline:
                            self.text.configure(state='disabled')
                            self.root.after(1, batch)
                            return
                except StopIteration:
                    self.end_text(position)
                    if anchor:
                        self.restore_reader_anchor(*anchor)
                    if self.search_text() and self.find_query.get():
                        self.find_message(1)
            batch()

        def highlight_matches(self):
            if getattr(self, 'setting_find_query', False):
                return
            self.text.tag_remove('find', '1.0', 'end')
            self.text.tag_remove('find_current', '1.0', 'end')
            self.find_matches, self.find_index = [], -1
            query = self.find_query.get()
            if query and self.message_limit < len(self.reader_messages):
                self.message_limit = len(self.reader_messages)
                self.show_messages(self.text.index('@0,0'))
                return
            if not query:
                self.find_status.set('Ctrl+F 搜索此对话')
                return
            start = '1.0'
            count = tk.IntVar()
            while True:
                match = self.text.search(query, start, stopindex='end', nocase=True, exact=True, count=count)
                if not match:
                    break
                end = self.text.index(f'{match}+{count.get()}c')
                self.find_matches.append((match, end))
                self.text.tag_add('find', match, end)
                start = end
            tools = []
            for panel in getattr(self, 'tool_panels', []):
                if any(query.casefold() in row['text'].casefold() for row in panel.rows):
                    if not panel.expanded:
                        panel.toggle()
                    tools.append(panel)
            if tools and not self.find_matches:
                panel = tools[0]
                def locate():
                    if panel.frame.winfo_exists():
                        self.text.see(panel.mark)
                self.root.after_idle(locate)
                self.find_status.set(f'{len(tools)} 个工具详情匹配（展开读取完整记录）')
                return
            self.find_status.set(f'{len(self.find_matches)} 处' +
                                 (' · 当前已加载内容' if len(self.reader_messages) > self.message_limit else ''))

        def find_message(self, direction):
            if not getattr(self, 'find_matches', None):
                return
            self.find_index = (self.find_index + direction) % len(self.find_matches) if self.find_index >= 0 else \
                (0 if direction > 0 else len(self.find_matches) - 1)
            start, end = self.find_matches[self.find_index]
            self.text.tag_remove('find_current', '1.0', 'end')
            self.text.tag_add('find_current', start, end)
            self.text.see(start)
            self.find_status.set(f'{self.find_index + 1} / {len(self.find_matches)}')

        def on_select(self, loaded=None, request=None):
            if request is not None and request != getattr(self, 'reader_request', None):
                return
            if request is None:
                self.reader_request = getattr(self, 'reader_request', 0) + 1
            if self.tree.selection() and getattr(self, 'version_iid', None) != self.tree.selection()[0]:
                self.version_override = None
            rows = self.selected()
            if len(rows) != 1:
                self.current_iid = None
                self.reader_messages, self.reader_stamp = [], None
                self.message_count.set('')
                self.more_messages.state(['disabled'])
                self.title_var.set('选择一条对话')
                self.info.set('在左侧点击查看内容；勾选方框可批量删除。')
                self.preview.set('')
                self.set_text([('empty', '这里会显示对话内容')])
                self.launch_button.state(['disabled'])
                self.copy_button.state(['disabled'])
                self.launch_status.set('')
                self.delete_button.state(['disabled'])
                return
            r = rows[0]
            iid = self.tree.selection()[0]
            same = iid == self.current_iid
            position = self.text.index('@0,0') if same else None
            previous_target = self.target_names.get(self.target.get())
            previous_cwd = self.cwd.get()
            self.delete_button.state(['!disabled'])
            try:
                def read():
                    try:
                        physical = physical_row(r)
                        stamp = transcript_stamp(physical)
                        messages = file_messages(physical)
                        if stamp != transcript_stamp(physical):
                            raise ValueError('会话在读取过程中变化，请刷新后再查看。')
                        return physical, messages, stamp, None
                    except (ValueError, OSError) as error:
                        return r, [], None, error
                stamp = transcript_stamp(r)
                size = sum(entry[1] for entry in json.loads(stamp)) if isinstance(stamp, str) else stamp[0]
                if loaded is None:
                    cached = getattr(self, 'reader_cache', None)
                    if cached and cached[0]['path'] == r['path'] and cached[2] == stamp:
                        loaded = (physical_row(r), *cached[1:])
                    elif size > 1024 * 1024:
                        self.reader_stamp = None
                        self.title_var.set(r['title'] or r['id'])
                        self.set_text([('note', '正在读取对话…')])
                        self.launch_button.state(['disabled'])
                        self.copy_button.state(['disabled'])
                        request = self.reader_request
                        self.background(read, lambda result: self.on_select(result, request),
                                        label='读取对话…')
                        return
                    else:
                        loaded = read()
                r, messages, stamp, error = loaded
                if error:
                    raise error
            except (ValueError, OSError) as error:
                self.reader_messages, self.reader_stamp = [], None
                self.message_count.set('')
                self.more_messages.state(['disabled'])
                self.title_var.set(r['title'] or r['id'])
                self.info.set('预览文件：' + r['path'])
                self.preview.set(str(error))
                self.set_text([('note', str(error))])
                self.launch_button.state(['disabled'])
                self.copy_button.state(['disabled'])
                self.delete_button.state(['disabled'])
                self.launch_status.set(str(error))
                self.target_menu.delete(0, 'end')
                return
            self.reader_cache = loaded
            self.current_iid = iid
            if not same:
                self.message_limit = MESSAGE_LIMIT
            self.reader_messages, self.reader_stamp = messages, stamp
            r['turns'] = sum(message['role'] == 'user' for message in messages)
            if not getattr(self, 'version_override', None):
                self.rows[iid] = r
            with contextlib.closing(ah.connect()) as db:
                r = titles.apply(db, r)
                member = conversations.lookup(db, r)
                self.version_options = {}
                if member:
                    heads = {item['id'] for item in conversations.tips(db, member['conversation'])}
                    for item in conversations.members(db, member['conversation']):
                        version_row = json.loads(item['row_json'])
                        kind = '接续版本' if item['parent_revision'] else '原会话'
                        if version_row.get('kind') == 'import':
                            kind = '已续聊副本' if item['baseline'] and item['head'] != item['baseline'] else '导入副本'
                        state = '并行分支' if item['id'] in heads and len(heads) > 1 else \
                                '当前版本' if item['id'] in heads else '历史版本'
                        label = version_row['source'] + ' / ' + version_row['id'][:13] + ' / ' + kind + ' / ' + state
                        self.version_options[label] = version_row
                        if item['id'] == member['id']:
                            self.version_value.set(label)
                self.version_box['values'] = list(self.version_options)
                origin = original_of(db, r)
            default = support.preferred_target(r, list(self.target_labels), self.preferences)
            self.title_var.set(r['title'] or '(无标题)')
            lines = [f"{r['tool']} · {r['source']} · {r['turns']} 轮",
                     f"更新于 {(r['updated'] or '')[:16].replace('T', ' ')}", r['id']]
            if origin is not r:
                lines.append(f"导入自 {origin['tool']} · {origin['source']} · {origin['id'][:13]}")
            reason = self.candidates.get(str(Path(r['path'])))
            if reason:
                lines.append('清理建议：' + reason)
            lines.append('预览文件：' + r['path'])
            self.info.set('\n'.join(lines))
            query = self.search_text()
            matched = query and any(query.casefold() in (message['text'] or '').casefold() for message in messages)
            if matched:
                self.setting_find_query = True
                try:
                    self.find_query.set(query)
                finally:
                    self.setting_find_query = False
                self.message_limit = len(messages)
            self.show_messages(position)
            target = previous_target if same and previous_target in self.target_labels else default
            cwd = previous_cwd if same else (r['cwd'] or '')
            with contextlib.closing(ah.connect()) as db:
                for candidate in dict.fromkeys([target, r['source'], *self.target_labels]):
                    if candidate not in self.target_labels:
                        continue
                    try:
                        launch_plan(db, r, candidate, cwd)
                    except (ValueError, OSError):
                        continue
                    target = candidate
                    break
            self.target.set(self.target_labels.get(target, ''))
            self.cwd.set(cwd)
            self.update_preview()
            if matched:
                self.find_message(1)

        def select_version(self, _event=None):
            row = self.version_options.get(self.version_value.get())
            if row and self.tree.selection():
                self.version_override = dict(row, logical=False)
                self.version_iid = self.tree.selection()[0]
                self.on_select()

        def set_text(self, parts, position=None):
            self.render_request = getattr(self, 'render_request', 0) + 1
            self.begin_text()
            for tag, chunk in parts:
                self.text.insert('end', chunk, tag)
            self.end_text(position)

        def begin_text(self):
            self.text.configure(state='normal')
            self.markdown_renderer.reset()
            self.tool_panels = []
            for _, name in self.message_marks:
                self.text.mark_unset(name)
            self.message_marks.clear()
            self.text.delete('1.0', 'end')

        def end_text(self, position=None):
            self.text.configure(state='disabled')
            self.highlight_matches()
            self.text.tag_raise('find')
            self.text.tag_raise('find_current')
            if position is None:
                self.text.see('end')
            else:
                self.text.yview(position)

        def current_plan(self, db=None):
            rows = self.selected()
            if len(rows) != 1:
                raise ValueError('请选择一条对话。')
            target = self.target_names.get(self.target.get())
            if not target:
                raise ValueError('请选择启动命令。')
            if self.reader_stamp is None or transcript_stamp(rows[0]) != self.reader_stamp:
                raise ValueError('会话已变化，请刷新历史后核对新增内容再启动。')
            if db is not None:
                return rows[0], launch_plan(db, rows[0], target, self.cwd.get())
            with contextlib.closing(ah.connect()) as db:
                return rows[0], launch_plan(db, rows[0], target, self.cwd.get())

        def update_preview(self):
            with contextlib.closing(ah.connect()) as db:
                self.show_launch_preview(db)

        def show_launch_preview(self, db):
            self.target_menu.delete(0, 'end')
            rows = self.selected()
            for name, label in self.target_labels.items():
                reason, description = '', '请先选择对话'
                if rows:
                    try:
                        option = launch_plan(db, rows[0], name, self.cwd.get())
                        description = option.get('description', '恢复选中版本')
                        if rows[0].get('logical') and rows[0].get('branches', 0) > 1:
                            reason = '存在多个分支，请先在版本选择中选择一个。'
                    except (ValueError, OSError) as error:
                        reason = str(error)
                else:
                    reason = description
                ready = self.readiness.get(name, dict(ready=False, message='请先检查启动环境。'))
                reason = reason or (ready['message'] if not ready['ready'] else '')
                text = f'{command_label(name)} · {reason or description}'
                self.target_menu.add_radiobutton(label=text, value=label, variable=self.target,
                                                state='disabled' if reason else 'normal', command=self.target_changed)
            try:
                _, plan = self.current_plan(db)
                current = self.selected()[0]
                if current.get('logical') and current.get('branches', 0) > 1:
                    raise ValueError('存在多个续聊分支，请先选择要继续的版本。')
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
                        start_terminal(prepared['cwd'], prepared['inner'], env=prepared['env'])
                    else:
                        start_terminal(prepared['cwd'], prepared['inner'])
                except OSError:
                    if self.launch_job:
                        launch_jobs.update(self.launch_job, state='failed', error='无法打开终端；请检查终端程序后重试。')
                    raise
                self.preferences['default_target'] = target
                if row['cwd']:
                    self.preferences['project_targets'][support.project_key(row['cwd'])] = target
                self.busy.set('终端已打开；请在终端确认恢复结果')
            self.background(prepare, done, label='正在准备选中的会话版本…', key='launch')

        # -------- deletion
        def delete_current(self):
            self.delete_rows(self.selected())

        def delete_checked(self):
            self.delete_rows([self.rows[i] for i in self.tree.get_children() if i in self.checked])

        def delete_rows(self, rows):
            if not rows:
                return
            chosen = {str(Path(r['path'])) for r in rows}
            groups = []
            with contextlib.closing(ah.connect()) as db:
                for r in rows:
                    targets = [dict(tool=r['tool'], source=r['source'], path=r['path'])]
                    if self.with_copies.get():
                        targets += [c for c in trash.related_copies(db, r['key'])
                                    if str(Path(c['path'])) not in chosen]
                    groups.append(((r['title'] or r['id'])[:80], targets))
            copies = sum(len(t) - 1 for _, t in groups)
            titles = '\n'.join(f"· [{r['source']}] {(r['title'] or r['id'])[:56]}（{r['turns']} 轮）" for r in rows[:10])
            more = f'\n…另 {len(rows) - 10} 条' if len(rows) > 10 else ''
            extra = f'\n\n另有 {copies} 个导入副本一并删除。' if copies else ''
            if not messagebox.askyesno('移到回收站', f'将 {len(rows)} 条对话移到回收站：\n\n{titles}{more}{extra}\n\n'
                                       f'{trash.RETENTION_DAYS} 天内可在“回收站”恢复，之后永久删除。', icon='warning'):
                return

            def work():
                with trash.locked_db(reindex=False) as db:
                    paths = {str(Path(t['path']).resolve()) for _, targets in groups for t in targets}
                    placeholders = ','.join('?' for _ in paths)
                    # Re-evaluate only affected duplicate IDs after their owner
                    # disappears; unrelated active histories need no full scan.
                    affected = paths | {r[0] for r in db.execute(
                        f'SELECT path FROM files WHERE session_key IN '
                        f'(SELECT session_key FROM files WHERE path IN ({placeholders}))', tuple(paths))}
                    report = trash.delete_many(db, groups)
                    ah.collect(db, only_paths=affected)
                    return report

            def done(report):
                self.busy.set(f"已移到回收站 {report['deleted']} 项")
                if report['skipped']:
                    lines = '\n\n'.join(f"· {s['title'][:50]}\n  {s['reason'][:160]}" for s in report['skipped'][:8])
                    more = f"\n\n…另 {len(report['skipped']) - 8} 条" if len(report['skipped']) > 8 else ''
                    messagebox.showwarning('部分未删除', f"已删除 {report['deleted']} 项，"
                                           f"{len(report['skipped'])} 项被跳过：\n\n{lines}{more}")
                self.projects_dirty = True
                self.refresh()
            self.background(work, done)

        # -------- trash tab
        def refresh_trash(self):
            selected = self.trash_tree.selection()
            position = self.trash_tree.yview()[0]
            self.trash_tree.delete(*self.trash_tree.get_children())
            now = datetime.datetime.now(datetime.timezone.utc)
            with contextlib.closing(ah.connect()) as db:
                self.trash_imported_keys = {r[0] for r in db.execute(
                    'SELECT dst_key FROM imports UNION SELECT dst_key FROM codex_imports')}
            self.trash_manifests = {}
            for m in trash.entries():
                local = lambda s: datetime.datetime.fromisoformat(s).astimezone().strftime('%Y-%m-%d %H:%M')
                summary = support.trash_summary(m, self.trash_imported_keys, now)
                self.trash_manifests[m['id']] = m
                self.trash_tree.insert('', 'end', iid=m['id'], tags=('soon',) if summary['soon'] else (), values=(
                    local(m['deleted_at']), local(m['expires_at']), summary['remaining'], summary['sources'],
                    summary['counts'], m['label']))
            self.trash_tree.selection_set([iid for iid in selected if iid in self.trash_manifests])
            self.trash_tree.yview_moveto(position)
            self.show_trash_details()

        def show_trash_details(self):
            lines = []
            for iid in self.trash_tree.selection():
                manifest = self.trash_manifests.get(iid)
                if not manifest:
                    continue
                summary = support.trash_summary(manifest, self.trash_imported_keys)
                lines.append(f"{manifest['label']} · {summary['counts']} · 剩余 {summary['remaining']}")
                for item in manifest['items']:
                    kind = support.trash_item_kind(item, self.trash_imported_keys)
                    lines.append(f"  [{kind}] {item['source']} · {item['sid']}\n"
                                 f"  恢复到：{item['path']}\n  工作目录：{item.get('cwd') or '未记录'}")
            text = '\n\n'.join(lines) if lines else (self.trash_restore_report or
                f'选择条目查看原会话与副本的位置。恢复回原连接、原文件位置，已有文件不会覆盖。\n'
                f'删除后保留 {trash.RETENTION_DAYS} 天；打开界面或定时收集时永久清除到期条目。\n'
                '旧回收站记录缺少的类型／工作目录会明确标为未记录。')
            self.trash_details.configure(state='normal')
            self.trash_details.delete('1.0', 'end')
            self.trash_details.insert('1.0', text)
            self.trash_details.configure(state='disabled')

        def restore(self):
            chosen = self.trash_tree.selection()
            if not chosen:
                return

            def work():
                with trash.locked_db() as db:
                    results = [trash.restore(db, entry) for entry in chosen]
                return results  # locked_db updates the index before returning to the UI

            def done(results):
                errors = [e for r in results for e in r['errors']]
                count = sum(r['restored'] for r in results)
                lines = [f'恢复结果：{count} 项成功，{len(errors)} 项未恢复。']
                for result in results:
                    for item in result['outcomes']:
                        status = '已恢复' if item['restored'] else '未恢复'
                        lines.append(f"[{status}] {item['source']} · {item['sid']}\n{item['path']}"
                                     + (f"\n原因：{item['reason']}" if item['reason'] else ''))
                self.trash_restore_report = '\n\n'.join(lines)
                if errors:
                    messagebox.showwarning('部分未恢复', f'{count} 项已恢复，{len(errors)} 项未恢复；'
                                           '完整结果可在回收站详情中查看。\n\n' + '\n'.join(errors[:8]))
                self.refresh_trash()
                self.trash_tree.selection_remove(*self.trash_tree.selection())
                self.show_trash_details()
                self.projects_dirty = True
                self.refresh()
                self.busy.set(f'已恢复 {count} 项，{len(errors)} 项未恢复；位置与原因见回收站详情')
            self.background(work, done, label='正在恢复会话并更新历史索引…', key='restore')

        def purge_now(self):
            chosen = self.trash_tree.selection()
            if not chosen or not messagebox.askyesno(
                    '永久删除', f'永久删除选中的 {len(chosen)} 个回收站条目？此操作无法恢复。', icon='warning'):
                return
            def work():
                with ah.collect_lock(wait=True) as locked:
                    if not locked:
                        raise ValueError('历史任务正在运行，请稍后重试。')
                    for entry in chosen:
                        trash.purge(entry)
            self.background(work, lambda result: self.refresh_trash(), label='清理回收站…', key='restore')

    if os.name == 'nt':
        with contextlib.suppress(Exception):
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)     # crisp text on high-DPI screens
    root = tk.Tk()
    style(root)
    app = App(root)

    def place_sashes():
        width = app.pane.winfo_width()
        if width > 1:
            for index, fraction in enumerate(app.preferences['panes']):
                app.pane.sashpos(index, int(width * fraction))
            if app.preferences['maximized'] and os.name == 'nt':
                root.state('zoomed')
    root.after(150, place_sashes)
    root.mainloop()


if __name__ == '__main__':
    try:
        run_ui()
    except Exception as error:
        support.log_error('Starting history viewer', error)
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f'{error}\n\n错误日志：{support.error_log_path()}', 'Agent History 启动失败', 0x10)
        raise
