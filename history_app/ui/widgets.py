"""Small readonly widgets shared by the history viewer."""
import tkinter as tk


class SelectableText(tk.Text):
    """A selectable label that wraps and grows inside its parent's scroll area."""

    def __init__(self, parent, *, variable=None, text='', **options):
        super().__init__(parent, height=1, width=1, wrap='word', state='disabled',
                         relief='flat', borderwidth=0, highlightthickness=0,
                         cursor='xterm', exportselection=False, **options)
        self.variable = variable
        self._fit_job = None
        self._width = None
        self._trace = variable.trace_add('write', self._refresh) if variable is not None else None
        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label='复制选中文本', command=self.copy_selection)
        self.menu.add_command(label='复制全部文本', command=self.copy_all)
        self.menu.add_separator()
        self.menu.add_command(label='全选', command=self.select_all)
        self.bind('<Control-c>', self.copy_selection)
        self.bind('<Control-C>', self.copy_selection)
        self.bind('<<Copy>>', self.copy_selection)
        self.bind('<Control-a>', self.select_all)
        self.bind('<Control-A>', self.select_all)
        self.bind('<Button-3>', self._popup)
        self.bind('<Configure>', self._resized)
        self.bind('<Destroy>', self._dispose)
        self._replace(variable.get() if variable is not None else text)

    def _refresh(self, *_args):
        self._replace(self.variable.get())

    def _replace(self, value):
        if value == self.get('1.0', 'end-1c'):
            return
        self.configure(state='normal')
        self.delete('1.0', 'end')
        self.insert('1.0', value)
        self.configure(state='disabled')
        self._schedule_fit()

    def _resized(self, event):
        if event.width != self._width:
            self._width = event.width
            self._schedule_fit()

    def _schedule_fit(self):
        if self._fit_job is None:
            self._fit_job = self.after_idle(self._fit_height)

    def _fit_height(self):
        self._fit_job = None
        lines = self.count('1.0', 'end-1c', 'displaylines')
        height = (lines[0] if lines else 0) + 1
        if int(self.cget('height')) != height:
            self.configure(height=height)

    def copy_selection(self, _event=None):
        if self.tag_ranges('sel'):
            self.clipboard_clear()
            self.clipboard_append(self.get('sel.first', 'sel.last'))
        return 'break'

    def copy_all(self):
        self.clipboard_clear()
        self.clipboard_append(self.get('1.0', 'end-1c'))

    def select_all(self, _event=None):
        self.focus_set()
        self.tag_add('sel', '1.0', 'end-1c')
        return 'break'

    def _popup(self, event):
        self.menu.entryconfigure(0, state='normal' if self.tag_ranges('sel') else 'disabled')
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return 'break'

    def _dispose(self, event):
        if event.widget is not self:
            return
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
        if self._trace is not None:
            self.variable.trace_remove('write', self._trace)
