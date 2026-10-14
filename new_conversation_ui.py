"""New conversation dialog; Agent execution is delegated to the main UI worker."""
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

import launch_backend as backend


class NewConversationDialog:
    def __init__(self, parent, on_launch, *, target='', cwd='', on_settings=lambda: None):
        self.on_launch = on_launch
        self.target = target
        self.plan = None
        self.launching = self.cancelled = False
        self.window = tk.Toplevel(parent)
        self.window.title('新增对话')
        self.window.geometry('780x560')
        self.window.minsize(560, 440)
        self.window.transient(parent)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Escape>', lambda event: self.close())
        body = ttk.Frame(self.window, padding=18)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='选择启动命令和工作目录，在独立终端开始新对话。').pack(anchor='w', pady=(0, 16))
        ttk.Label(body, text='启动命令').pack(anchor='w')
        self.target_label = tk.StringVar()
        self.target_box = ttk.Menubutton(body, textvariable=self.target_label, name='new_launch_target')
        self.menu = tk.Menu(self.target_box, tearoff=False)
        self.target_box.configure(menu=self.menu)
        self.target_box.pack(fill='x', pady=(4, 12))
        ttk.Label(body, text='工作目录').pack(anchor='w')
        row = ttk.Frame(body)
        row.pack(fill='x', pady=(4, 12))
        self.cwd = tk.StringVar(value=cwd or str(Path.home()))
        self.directory = ttk.Entry(row, textvariable=self.cwd, name='new_working_directory')
        self.directory.pack(side='left', fill='x', expand=True)
        self.browse_button = ttk.Button(row, text='浏览目录', command=self.browse)
        self.browse_button.pack(side='right', padx=(8, 0))
        ttk.Label(body, text='将执行（模型沿用所选命令的配置）').pack(anchor='w')
        preview_frame = ttk.Frame(body)
        preview_frame.pack(fill='both', expand=True, pady=(4, 8))
        self.preview = tk.Text(preview_frame, height=7, wrap='word', state='disabled', padx=10, pady=8)
        scroll = ttk.Scrollbar(preview_frame, command=self.preview.yview)
        self.preview.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.preview.pack(side='left', fill='both', expand=True)
        self.message = tk.StringVar()
        ttk.Label(body, textvariable=self.message, wraplength=710).pack(fill='x', pady=(0, 8))
        actions = ttk.Frame(body)
        actions.pack(fill='x')
        self.check_button = ttk.Button(actions, text='重新检查', command=self.reload)
        self.check_button.pack(side='left')
        self.settings_button = ttk.Button(actions, text='启动配置', command=on_settings)
        self.settings_button.pack(side='left', padx=6)
        self.copy_button = ttk.Button(actions, text='复制命令', command=self.copy)
        self.copy_button.pack(side='left')
        ttk.Button(actions, text='取消', command=self.close).pack(side='right')
        self.start_button = ttk.Button(actions, text='打开终端并新建', style='Accent.TButton', command=self.start)
        self.start_button.pack(side='right', padx=8)
        self.cwd.trace_add('write', lambda *args: self.update_preview())
        self.reload()

    def alive(self):
        return not self.cancelled and bool(self.window.winfo_exists())

    def close(self):
        self.cancelled = True
        self.window.destroy()

    def reload(self):
        if self.launching:
            return
        self.menu.delete(0, 'end')
        self.options = {}
        try:
            targets = backend.launch_targets()
        except (ValueError, OSError, KeyError) as error:
            self.message.set(str(error))
            targets = []
        for key in targets:
            try:
                label = backend.command_label(key)
                ready = backend.new_readiness(key)
            except (ValueError, OSError, KeyError) as error:
                label, ready = key, dict(ready=False, message=str(error))
            self.options[key] = (label, ready)
            status = '可启动新对话' if ready['ready'] else ready['message'].replace('\n', '；')
            self.menu.add_command(label=label + ' · ' + status,
                state='normal' if ready['ready'] else 'disabled', command=lambda key=key: self.choose(key))
        available = [key for key, (_, ready) in self.options.items() if ready['ready']]
        if self.target not in available:
            self.target = next(iter(available or self.options), '')
        self.target_label.set(self.options[self.target][0] if self.target else '请先配置启动入口')
        self.update_preview()

    def choose(self, key):
        self.target = key
        self.target_label.set(self.options[key][0])
        self.update_preview()

    def update_preview(self):
        if self.launching:
            return
        self.plan = None
        text = ''
        try:
            if not self.target:
                raise ValueError('暂无启动入口，请先添加启动配置。')
            ready = self.options[self.target][1]
            if not ready['ready']:
                raise ValueError(ready['message'])
            plan = backend.new_plan(self.target, self.cwd.get())
            text = plan['preview']
            self.plan = plan
            self.message.set('将开始新会话；终端结束后采集已接入来源的历史，也可点击“刷新历史”查看。')
        except (ValueError, OSError, KeyError) as error:
            self.message.set(str(error))
        self.preview.configure(state='normal')
        self.preview.delete('1.0', 'end')
        self.preview.insert('1.0', text)
        self.preview.configure(state='disabled')
        for button in (self.start_button, self.copy_button):
            button.state(['!disabled'] if self.plan else ['disabled'])

    def browse(self):
        folder = filedialog.askdirectory(parent=self.window, initialdir=self.cwd.get() if Path(self.cwd.get()).is_dir()
                                         else str(Path.home()))
        if folder:
            self.cwd.set(folder)

    def copy(self):
        if self.plan:
            self.window.clipboard_clear()
            self.window.clipboard_append(self.plan['preview'])
            self.message.set('启动命令已复制。')

    def start(self):
        if self.launching or self.plan is None:
            return
        self.launching = True
        for widget in (self.start_button, self.target_box, self.directory, self.browse_button,
                       self.check_button, self.settings_button, self.copy_button):
            widget.state(['disabled'])
        self.message.set('正在检查并准备新会话…')
        self.on_launch(self, self.target, self.cwd.get(), self.plan)

    def finish(self, error=None):
        if not self.alive():
            return
        if error is None:
            self.close()
            return
        self.launching = False
        for widget in (self.target_box, self.directory, self.browse_button, self.check_button, self.settings_button):
            widget.state(['!disabled'])
        self.update_preview()
        self.message.set(str(error))
