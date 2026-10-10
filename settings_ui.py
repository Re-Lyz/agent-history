"""Local configuration dialogs. Saving never launches an Agent or edits credentials."""
import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

import agent_history as ah
import connections
import launch_profiles as profiles
import launch_backend as backend
import setup_backend as setup
import entry_settings


class ProfileManager:
    def __init__(self, parent, on_changed=lambda: None):
        self.on_changed = on_changed
        self.window = tk.Toplevel(parent)
        self.window.title('启动配置管理')
        self.window.geometry('850x760')
        self.window.transient(parent)
        self.message = tk.StringVar()
        self.items, errors = profiles.load()
        self.builtin_keys = set()
        self.history_keys = set()
        for key, entry in {**connections.load(ah.ROOT)['connections'], 'claude': {}}.items():
            if entry.get('history_only'):
                self.history_keys.add(key)
            self.builtin_keys.add(key)
            item = dict(id=key, type='claude' if key == 'claude' else 'codex',
                        cwd_policy='chosen', args=[], resume_args=[], env={}, resume_tools=[],
                        connection='' if key == 'claude' else key)
            item.update(entry_settings.item(key, backend.command_label(key)))
            if key in self.history_keys:
                item.update(enabled=False, default=False, favorite=False)
            self.items[key] = item
        self.errors = errors
        body = ttk.Frame(self.window, padding=14)
        body.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(body, columns=('name', 'command'), show='headings', height=5, selectmode='browse')
        self.tree.heading('name', text='显示名称（★ 收藏，默认）')
        self.tree.heading('command', text='实际命令')
        self.tree.pack(fill='x')
        self.tree.bind('<<TreeviewSelect>>', self.select)
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=8)
        for label, command in [('新增', self.new), ('删除', self.remove), ('上移', lambda: self.move(-1)),
                               ('下移', lambda: self.move(1)), ('打开配置文件', self.open_file)]:
            ttk.Button(actions, text=label, command=command).pack(side='left', padx=3)
        self.values, self.form_widgets = {}, {}
        fields = [('id', '配置 ID', ''), ('name', '显示名称', ''), ('type', '类型', ('codex', 'claude', 'custom')),
                  ('connection', 'Codex 连接', tuple(connections.homes(ah.ROOT))),
                  ('program', '程序／脚本（内置入口可留空）', ''),
                  ('cwd_policy', '工作目录策略', ('chosen', 'session', 'fixed')), ('cwd', '固定工作目录', '')]
        form = ttk.Frame(body)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        for index, (key, label, options) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=index, column=0, sticky='w', pady=3)
            var = self.values[key] = tk.StringVar()
            widget = ttk.Combobox(form, textvariable=var, values=options, state='readonly', name=key) \
                if isinstance(options, tuple) else ttk.Entry(form, textvariable=var, name=key)
            widget.grid(row=index, column=1, sticky='ew', padx=8, pady=3)
            self.form_widgets[key] = widget
        self.texts = {}
        for key, label, height in [('args', '启动参数：每行一个参数', 2),
                                   ('resume_args', '恢复参数：每行一个，使用 {session_id}（内置可留空）', 2),
                                   ('env', '环境变量：JSON 对象，例如 {"TOKEN":{"from_env":"MY_TOKEN"}}', 2)]:
            ttk.Label(body, text=label).pack(anchor='w', pady=(6, 2))
            widget = self.texts[key] = tk.Text(body, height=height, wrap='none', name=key)
            widget.pack(fill='x')
        flags = ttk.Frame(body)
        flags.pack(fill='x', pady=8)
        self.flags = {}
        self.flag_widgets = {}
        for key, label in [('enabled', '启用'), ('favorite', '收藏'), ('default', '设为默认'),
                           ('codex', '自定义恢复 Codex'), ('claude', '自定义恢复 Claude')]:
            var = self.flags[key] = tk.BooleanVar()
            widget = self.flag_widgets[key] = ttk.Checkbutton(flags, text=label, variable=var)
            widget.pack(side='left', padx=4)
        footer = ttk.Frame(body)
        footer.pack(fill='x')
        ttk.Button(footer, text='保存配置', command=self.save).pack(side='left')
        ttk.Button(footer, text='测试配置（不发送模型请求）', command=self.check).pack(side='left', padx=8)
        ttk.Label(body, textvariable=self.message, wraplength=800).pack(fill='x', pady=8)
        self.populate()
        self.new()
        if errors:
            self.message.set('配置存在错误，请先修复文件：\n' + '\n'.join(errors))

    def populate(self, selected=None):
        self.tree.delete(*self.tree.get_children())
        for key, item in sorted(self.items.items(), key=lambda pair: pair[1]['order']):
            label = ('★ ' if item['favorite'] else '') + item['name'] + (' · 默认' if item['default'] else '')
            command = item.get('program') or backend.command_label(item.get('connection') or 'claude')
            if key in self.builtin_keys:
                entry = connections.load(ah.ROOT)['connections'].get(key, {})
                actual = entry.get('program') or ('claude' if key == 'claude' else
                          'codex' if entry.get('direct') or os.name != 'nt' else 'codexc ' + key)
                command = ('已有入口 · ' + str(actual) + ' · ' + str(entry.get('home') or 'Claude 项目目录'))
                if key in self.history_keys:
                    command = '仅历史来源 · ' + str(entry.get('home'))
            self.tree.insert('', 'end', iid=key, values=(label, command))
        if selected in self.items:
            self.tree.selection_set(selected)

    def new(self):
        self.editing = None
        self.tree.selection_remove(*self.tree.selection())
        self.fill(dict(type='codex', cwd_policy='chosen', enabled=True))

    def fill(self, item):
        builtin = getattr(self, 'editing', None) in self.builtin_keys
        for key, widget in self.form_widgets.items():
            widget.configure(state='disabled' if builtin and key != 'name' else
                             'readonly' if isinstance(widget, ttk.Combobox) else 'normal')
        for key, var in self.values.items():
            var.set(item.get(key, ''))
        for key, widget in self.texts.items():
            widget.configure(state='normal')
            widget.delete('1.0', 'end')
            widget.insert('1.0', json.dumps(item.get('env', {}), ensure_ascii=False) if key == 'env'
                          else '\n'.join(item.get(key, [])))
            widget.configure(state='disabled' if builtin else 'normal')
        for key, var in self.flags.items():
            var.set(key in item.get('resume_tools', []) if key in ('codex', 'claude') else item.get(key, False))
            self.flag_widgets[key].configure(state='disabled' if builtin and
                (key in ('codex','claude') or self.editing in self.history_keys and key in ('enabled','favorite','default')) else 'normal')

    def select(self, _event=None):
        selection = self.tree.selection()
        if selection:
            self.editing = selection[0]
            self.fill(self.items[self.editing])
            if self.editing in self.builtin_keys:
                self.message.set('仅历史来源，不参与终端启动。' if self.editing in self.history_keys else
                    '已有入口：可改显示名称、启用、收藏、默认和顺序；程序与连接由历史来源配置管理。删除会恢复显示设置。')

    def read(self):
        item = {key: var.get().strip() for key, var in self.values.items()}
        if item['type'] != 'codex':
            item.pop('connection', None)
        if not item['program']:
            item.pop('program')
        if not item.get('connection'):
            item.pop('connection', None)
        for key in ('args', 'resume_args'):
            item[key] = self.texts[key].get('1.0', 'end-1c').splitlines()
        try:
            item['env'] = json.loads(self.texts['env'].get('1.0', 'end-1c') or '{}')
        except ValueError as error:
            raise ValueError('env: JSON 格式错误：' + str(error)) from error
        for key in ('enabled', 'favorite', 'default'):
            item[key] = self.flags[key].get()
        item['resume_tools'] = [key for key in ('codex', 'claude') if self.flags[key].get()]
        item['order'] = self.items.get(self.editing, {}).get('order', len(self.items))
        return profiles.validate(item)

    def publish(self, items, selected=None):
        if self.errors:
            raise ValueError('请先修复配置文件中的无效条目。')
        profiles.save([item for key, item in items.items() if key not in self.builtin_keys])
        self.items = items
        for key in self.builtin_keys:
            entry_settings.save(key, items[key])
        self.populate(selected)
        self.on_changed()

    def save(self):
        try:
            if self.editing in self.builtin_keys:
                item = dict(self.items[self.editing], name=self.values['name'].get().strip())
                if not item['name']:
                    raise ValueError('name: 显示名称不能为空。')
                item.update({key: self.flags[key].get() for key in ('enabled', 'favorite', 'default')})
                items = {key: dict(value) for key, value in self.items.items()}
                if item['default']:
                    for value in items.values():
                        value['default'] = False
                items[self.editing] = item
                self.publish(items, self.editing)
                self.message.set('已有入口设置已保存。')
                return
            item = self.read()
            key = 'profile:' + item['id']
            if key != self.editing and key in self.items:
                raise ValueError('id: 配置 ID 已存在。')
            items = {k: dict(v) for k, v in self.items.items() if k != self.editing}
            if item['default']:
                for value in items.values():
                    value['default'] = False
            items[key] = item
            self.publish(items, key)
            self.editing = key
            self.message.set('配置已保存。可点击测试配置检查运行环境。')
        except (ValueError, OSError) as error:
            self.message.set(str(error))

    def remove(self):
        if self.editing:
            try:
                if self.editing in self.builtin_keys:
                    entry_settings.save(self.editing)
                    self.items[self.editing].update(entry_settings.item(self.editing, backend.command_label(self.editing)))
                    self.populate(self.editing)
                    self.on_changed()
                    self.message.set('已有入口恢复默认显示设置；登录和连接配置保留。')
                    return
                self.publish({k: v for k, v in self.items.items() if k != self.editing})
                self.new()
                self.message.set('配置已删除；历史对话保留。')
            except (ValueError, OSError) as error:
                self.message.set(str(error))

    def move(self, direction):
        keys = list(self.tree.get_children())
        if self.editing not in self.builtin_keys:
            keys = [key for key in keys if key not in self.builtin_keys]
        if self.editing not in keys:
            return
        index = keys.index(self.editing)
        other = index + direction
        if 0 <= other < len(keys):
            keys[index], keys[other] = keys[other], keys[index]
            items = {**self.items, **{key: dict(self.items[key], order=order) for order, key in enumerate(keys)}}
            try:
                self.publish(items, self.editing)
            except (ValueError, OSError) as error:
                self.message.set(str(error))

    def check(self):
        try:
            if self.editing in self.builtin_keys:
                import ui_support
                self.message.set(ui_support.launch_readiness(self.editing)['message'])
                return
            item = self.read()
            report = backend.check_profile(item)
            self.message.set(report['message'])
        except (ValueError, OSError) as error:
            self.message.set(str(error))

    def open_file(self):
        path = profiles.config_path()
        if path.exists() and os.name == 'nt':
            os.startfile(path)
        else:
            self.message.set('配置文件：' + str(path))


class SetupWizard:
    def __init__(self, parent, on_changed=lambda: None):
        self.on_changed = on_changed
        self.window = tk.Toplevel(parent)
        self.window.title('首次配置／历史来源')
        self.window.geometry('850x520')
        self.window.transient(parent)
        body = ttk.Frame(self.window, padding=16)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='只选自己使用的 Codex／Claude 历史目录；也可手动添加。程序尚未安装时仍可查看历史，安装并登录 Agent 后才能继续。',
                  wraplength=800).pack(anchor='w')
        self.tree = ttk.Treeview(body, columns=('check', 'tool', 'name', 'home'), show='headings', height=9)
        for col, label, width in [('check', '接入', 45), ('tool', '类型', 70), ('name', '显示名称', 140), ('home', '历史目录', 460)]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, stretch=col == 'home')
        self.tree.pack(fill='both', expand=True, pady=10)
        self.tree.bind('<Button-1>', self.toggle)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        self.tree.bind('<space>', lambda e: self.toggle_selected())
        self.rows, self.checked = {}, set()
        self.message = tk.StringVar()
        try:
            report = setup.discover()
            selected = setup.load_sources()
            for row in report['candidates']:
                key = self.add(row)
                if selected is None or (row['connection'] in selected['codex'] if row['tool'] == 'codex'
                                        else row['home'] in selected['claude']):
                    self.checked.add(key)
                    self.paint(key)
            self.message.set('程序检测：' + '；'.join(tool + (' 已找到' if path else ' 未找到')
                                                  for tool, path in report['programs'].items()))
        except (ValueError, OSError) as error:
            self.message.set(str(error))
        form = ttk.Frame(body)
        form.pack(fill='x')
        self.name, self.tool = tk.StringVar(), tk.StringVar(value='codex')
        ttk.Label(form, text='所选显示名称').pack(side='left')
        ttk.Entry(form, textvariable=self.name).pack(side='left', padx=8)
        ttk.Button(form, text='更新名称', command=self.rename).pack(side='left')
        ttk.Combobox(form, textvariable=self.tool, values=('codex', 'claude'), state='readonly', width=9).pack(side='left', padx=8)
        ttk.Button(form, text='添加目录…', command=self.browse).pack(side='left')
        ttk.Label(body, textvariable=self.message, wraplength=800).pack(fill='x', pady=12)
        ttk.Button(body, text='保存并索引历史', command=self.apply).pack(side='right')
        ttk.Button(body, text='稍后配置', command=self.window.destroy).pack(side='right', padx=8)
        ttk.Button(body, text='首次使用指引', command=self.guide).pack(side='left')

    def guide(self):
        window = tk.Toplevel(self.window)
        window.title('首次使用指引')
        window.geometry('800x600')
        text = tk.Text(window, wrap='word', padx=14, pady=12)
        scroll = ttk.Scrollbar(window, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); text.pack(fill='both', expand=True)
        text.insert('1.0', (Path(__file__).parent / 'docs/first-use.md').read_text(encoding='utf-8'))
        text.configure(state='disabled')

    def add(self, row):
        key = str(len(self.rows))
        self.rows[key] = row
        self.tree.insert('', 'end', iid=key, values=('☐', row['tool'], row['name'], row['home']))
        return key

    def paint(self, key):
        self.tree.set(key, 'check', '☑' if key in self.checked else '☐')

    def toggle(self, event):
        if self.tree.identify_column(event.x) == '#1':
            key = self.tree.identify_row(event.y)
            if key:
                (self.checked.remove if key in self.checked else self.checked.add)(key)
                self.paint(key)

    def toggle_selected(self):
        for key in self.tree.selection():
            (self.checked.remove if key in self.checked else self.checked.add)(key)
            self.paint(key)

    def select(self, _event=None):
        if self.tree.selection():
            self.name.set(self.rows[self.tree.selection()[0]]['name'])

    def rename(self):
        if self.tree.selection():
            key = self.tree.selection()[0]
            self.rows[key]['name'] = self.name.get().strip()
            self.tree.set(key, 'name', self.name.get().strip())

    def browse(self):
        directory = filedialog.askdirectory(parent=self.window)
        if directory:
            key = self.add(dict(tool=self.tool.get(), name=Path(directory).name,
                                home=directory, connection='', registered=False))
            self.checked.add(key)
            self.paint(key)

    def apply(self):
        try:
            self.rename()
            setup.configure([row for key, row in self.rows.items() if key in self.checked])
            self.on_changed()
            self.window.destroy()
        except (ValueError, OSError) as error:
            self.message.set(str(error))
