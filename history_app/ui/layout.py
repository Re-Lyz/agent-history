"""Layout behavior for the history window; dependencies are bound per window."""
import trash
import markdown_preview as markdown


def create_mixin(bindings, desktop):
    tk = desktop.tk
    ttk = desktop.ttk
    P = desktop.palette
    card = desktop.card

    class LayoutController:
        def build(self):
            root = self.root
            top = ttk.Frame(root, padding=(16, 14, 16, 6))
            top.pack(fill='x')
            tk.Label(top, text='Agent History', bg=P['bg'], fg=P['text'],
                     font=(bindings.FONT, 15, 'bold')).pack(side='left', padx=(0, 18))
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
            ttk.Button(top, text='重试失败索引', style='Soft.TButton',
                       command=self.retry_failed_indexes).pack(side='left')
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
            ttk.Button(audit_bar, text='诊断与维护', command=self.open_maintenance).pack(side='right', padx=8)
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
            self.tree.heading('check', text=bindings.CHECK_OFF, command=self.toggle_all)
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
            self.find_query.trace_add('write', lambda *args: self.schedule_find())
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
            self.render_info = tk.StringVar(value='网页打开 · 本地文件定位 · 图片不加载' if enabled else
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
            self.text = tk.Text(reader_viewport, wrap='word', state='disabled', font=(bindings.FONT, 10), bg=P['card'],
                                fg=P['text'], relief='flat', padx=22, pady=12, spacing1=0, spacing3=0,
                                highlightthickness=0, insertbackground=P['text'], selectbackground=P['accent_soft'])
            self.text.tag_configure('head_user', foreground=P['user'], font=(bindings.FONT, 9, 'bold'), spacing1=10)
            self.text.tag_configure('head_other', foreground=P['assistant'], font=(bindings.FONT, 9, 'bold'), spacing1=10)
            self.text.tag_configure('body', lmargin1=2, lmargin2=2, spacing2=1)
            self.text.tag_configure('tool', foreground=P['muted'], font=('Consolas', 9),
                                    lmargin1=10, lmargin2=10)
            self.text.tag_configure('note', foreground=P['muted'], justify='center')
            self.text.tag_configure('empty', foreground=P['muted'], justify='center', spacing1=120)
            self.text.tag_configure('find', background='#f8e5ab')
            self.text.tag_configure('find_current', background='#e9be62')
            self.markdown_renderer = markdown.TkRenderer(self.text, palette=P, font=bindings.FONT,
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

            bindings.settle_geometry(reader_viewport,
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
            bindings.SelectableText(inner, variable=self.title_var, name='action_title',
                           font=(bindings.FONT, 12, 'bold'), bg=P['card'], fg=P['text']).pack(fill='x')
            bindings.SelectableText(inner, variable=self.info, name='action_info',
                           font=(bindings.FONT, 9), bg=P['card'], fg=P['muted']).pack(fill='x', pady=(6, 0))
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
            bindings.SelectableText(inner, variable=self.launch_status, name='action_status',
                           font=(bindings.FONT, 9), bg=P['card'], fg=P['muted']).pack(fill='x', pady=(0, 8))
            ttk.Label(inner, text='工作目录', style='Section.TLabel').pack(anchor='w')
            row = tk.Frame(inner, bg=P['card'])
            row.pack(fill='x', pady=(4, 12))
            self.cwd = tk.StringVar()
            self.cwd.trace_add('write', lambda *a: self.update_preview())
            ttk.Button(row, text='浏览', style='Soft.TButton', command=self.browse).pack(side='right', padx=(6, 0))
            ttk.Entry(row, textvariable=self.cwd).pack(side='left', fill='x', expand=True)
            ttk.Label(inner, text='将执行', style='Section.TLabel').pack(anchor='w')
            self.preview = tk.StringVar()
            self.preview_label = bindings.SelectableText(inner, variable=self.preview, name='action_preview',
                                                font=('Consolas', 9), bg=P['code_bg'], fg=P['text'],
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
            ttk.Button(inner, text='取消未接管启动', command=self.cancel_unclaimed_launch).pack(fill='x', pady=(6, 0))
            ttk.Button(inner, text='同步已切出的原对话', command=self.sync_switched_session).pack(fill='x', pady=(6, 0))
            ttk.Button(inner, text='保留分支并完成同步', command=lambda: self.retry_handoff(allow_branch=True)).pack(fill='x', pady=(6, 0))
            ttk.Button(inner, text='启动记录', command=self.show_launch_reports).pack(fill='x', pady=(6, 0))
            ttk.Button(inner, text='查看错误日志', style='Soft.TButton', command=self.open_log).pack(fill='x', pady=(6, 0))
            tk.Frame(inner, bg=P['line'], height=1).pack(fill='x', pady=18)
            self.with_copies = tk.BooleanVar(value=True)
            ttk.Checkbutton(inner, text='同时删除关联副本', variable=self.with_copies,
                            style='Card.TCheckbutton').pack(anchor='w')
            bindings.SelectableText(inner, text='包括导入到其他连接或 Claude 的副本。', font=(bindings.FONT, 9),
                           bg=P['card'], fg=P['muted']).pack(fill='x', pady=(4, 0))
            self.delete_button = ttk.Button(inner, text='删除这条对话', style='Soft.TButton', command=self.delete_current)
            self.delete_button.pack(fill='x', pady=(8, 0))
            bindings.SelectableText(inner, text=f'删除后在回收站保留 {trash.RETENTION_DAYS} 天，可随时恢复。',
                           font=(bindings.FONT, 9), bg=P['card'], fg=P['muted']).pack(fill='x', pady=(8, 0))
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
            bindings.settle_geometry(canvas, apply_width)
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
            self.trash_details = tk.Text(detail_body, name='trash_details', height=7, wrap='word', state='disabled', bg=P['code_bg'],
                                         fg=P['text'], font=(bindings.FONT, 10), relief='flat', padx=12, pady=8)
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

    return LayoutController
