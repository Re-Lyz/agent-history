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


from launch_backend import (file_messages, file_message_page, physical_row, transcript_stamp, launch_targets,
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
from ui_widgets import SelectableText


from history_app.history_queries import review_candidates, list_rows, recovery_job, render_messages
from history_app import history_queries


def project_choices(db):
    return history_queries.project_choices(db, list_fn=list_rows)


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
        s.configure('ToolToggle.TButton', background=P['card'], foreground=P['muted'],
                    padding=(6, 4), borderwidth=0, relief='flat', width=-1)
        s.layout('ToolToggle.TButton', [('Button.padding', {'sticky': 'nswe', 'children': [
            ('Button.label', {'sticky': 'nswe'})]})])
        s.map('ToolToggle.TButton', background=[('active', P['accent_soft'])],
              foreground=[('active', P['text'])])
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

    from types import SimpleNamespace
    import sys
    from . import layout, workers, launch, listing, reader, recycling
    bindings = sys.modules[__name__]
    desktop = SimpleNamespace(tk=tk, ttk=ttk, messagebox=messagebox, filedialog=filedialog,
                              simpledialog=simpledialog, palette=P, card=card)

    class App(*(module.create_mixin(bindings, desktop) for module in
                (layout, workers, launch, listing, reader, recycling))):
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
            self.pending_jobs = {}
            self.idle_notice = None
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
            root.bind('<Destroy>', lambda event: self.read_workers.close()
                      if event.widget is root and hasattr(self, 'read_workers') else None, add='+')
            root.bind('<Configure>', self.remember_geometry, add='+')
            root.report_callback_exception = self.callback_error
            self.busy = tk.StringVar(value='正在索引历史…')
            self.build()
            root.after(100, self.poll)
            root.after(1000, self.poll_launch_report)
            self.background(self.initial_load, self.after_initial, label='正在索引历史…', key='initial')
            # Render the last collected read model while discovery runs. Opening
            # or launching a row still verifies its current native file and lock.
            self.refresh()
            root.bind('<Control-f>', self.focus_find)

        def remember_geometry(self, event):
            if event.widget is self.root and self.root.state() == 'normal':
                self.normal_geometry = self.root.geometry()

        def close(self):
            maintenance_window = getattr(self, 'maintenance_window', None)
            if maintenance_window and maintenance_window.window.winfo_exists() and maintenance_window.running:
                maintenance_window.window.lift()
                self.busy.set('维护任务尚在运行，请等待完成后关闭。')
                return
            try:
                if not getattr(self, 'preserve_restored_preferences', False):
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
                self.reader_request = getattr(self, 'reader_request', 0) + 1
                self.list_request = getattr(self, 'list_request', 0) + 1
                self.render_request = getattr(self, 'render_request', 0) + 1
                if hasattr(self, 'read_workers'):
                    self.read_workers.close()
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


    if os.name == 'nt':
        with contextlib.suppress(Exception):
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)     # crisp text on high-DPI screens
    root = tk.Tk()
    style(root)
    app = App(root)
    root.history_app = app

    def place_sashes():
        width = app.pane.winfo_width()
        if width > 1:
            for index, fraction in enumerate(app.preferences['panes']):
                app.pane.sashpos(index, int(width * fraction))
            if app.preferences['maximized'] and os.name == 'nt':
                root.state('zoomed')
    root.after(150, place_sashes)
    root.mainloop()


def main():
    try:
        run_ui()
    except Exception as error:
        support.log_error('Starting history viewer', error)
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f'{error}\n\n错误日志：{support.error_log_path()}', 'Agent History 启动失败', 0x10)
        raise
