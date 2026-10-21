"""Embedded, independently scrollable tool panels for the Tk conversation reader."""
import tkinter as tk
from tkinter import ttk
import tool_details


class ToolPanel:
    def __init__(self, app, rows, identity, states, palette):
        self.app, self.rows, self.identity, self.states = app, rows, identity, states
        self.palette = palette
        self.frame = tk.Frame(app.text, bg=palette['card'], borderwidth=0, highlightthickness=0)
        self.label = rows[0].get('tool_name') or '工具详情'
        self.status = ' · 调用与输出' if len(rows) > 1 else ' · 单独记录'
        self.button = ttk.Button(self.frame, style='ToolToggle.TButton', command=self.toggle)
        self.button.pack(anchor='w')
        self.body = self.text = None
        self.loaded = False
        self.loading = False
        self.expanded = False
        self.button.configure(text='▸ ' + self.label + self.status)
        query = app.find_query.get().casefold()
        if states.get(identity) or (query and any(query in row['text'].casefold() for row in rows)):
            self.toggle()

    def create_body(self):
        if self.body is not None:
            return
        palette = self.palette
        self.body = tk.Frame(self.frame, bg=palette['card'], borderwidth=0, highlightthickness=0)
        self.text = tk.Text(self.body, height=10, width=42, wrap='word', state='disabled',
                            font=('Consolas', 9), relief='flat', borderwidth=0, highlightthickness=0,
                            bg=palette['code_bg'], fg=palette['text'])
        scroll = ttk.Scrollbar(self.body, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.text.pack(fill='both', expand=True)
        for event in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.text.bind(event, self.wheel)

    def wheel(self, event):
        direction = -1 if event.num == 4 or event.delta > 0 else 1
        self.text.yview_scroll(direction * max(1, abs(event.delta) // 120), 'units')
        return 'break'

    def write(self, value):
        if not self.frame.winfo_exists():
            return
        self.write_request = getattr(self, 'write_request', 0) + 1
        request = self.write_request
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.configure(state='disabled')
        def batch(offset=0):
            if request != self.write_request or not self.frame.winfo_exists():
                return
            self.text.configure(state='normal')
            self.text.insert('end', value[offset:offset+32_768])
            self.text.configure(state='disabled')
            if offset+32_768 < len(value):
                self.app.root.after(1, lambda: batch(offset+32_768))
                return
            query = self.app.find_query.get()
            if query:
                found = self.text.search(query, '1.0', stopindex='end', nocase=True)
                if found:
                    self.text.tag_configure('match', background='#f6e3a4')
                    self.text.tag_add('match', found, f'{found}+{len(query)}c')
                    self.text.see(found)
        batch()

    def toggle(self):
        anchor = self.app.reader_anchor()
        self.expanded = not self.expanded
        self.states[self.identity] = self.expanded
        if self.expanded:
            self.create_body()
            self.body.pack(fill='both', expand=True)
            if not self.loaded and not self.loading:
                self.loading = True
                self.write('正在读取完整工具记录…')
                def work():
                    try:
                        return '\n\n'.join(('调用参数' if row['role'] == 'tool_call' else '工具输出') + '\n' +
                            (tool_details.read(row['detail']) if row.get('detail') else
                             row['text'] + '\n（仅有摘要，无原记录定位）') for row in self.rows)
                    except (OSError, ValueError) as error:
                        return str(error)
                def done(value):
                    if self.frame.winfo_exists():
                        self.loading = False
                        self.loaded = True
                        self.write(value)
                self.app.background(work, done, label='读取工具详情…', key=None, refresh_preview=False)
        else:
            self.body.pack_forget()
        self.button.configure(text=('▾ ' if self.expanded else '▸ ') + self.label + self.status)
        if anchor:
            self.app.root.after_idle(lambda: self.app.restore_reader_anchor(anchor))
