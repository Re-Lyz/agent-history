"""Reader behavior for the history window; dependencies are bound per window."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import time
import webbrowser
import agent_history as ah
import ui_support as support
import markdown_preview as markdown
import conversation_store as conversations
import conversation_titles as titles
import tool_details


def create_mixin(bindings, desktop):
    tk = desktop.tk
    P = desktop.palette

    class ReaderController:
        def focus_find(self, event=None):
            self.find_entry.focus_set()
            self.find_entry.selection_range(0, 'end')
            return 'break'

        def load_earlier(self):
            anchor = self.reader_anchor()
            self.message_limit += bindings.MESSAGE_LIMIT
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
            elif markdown.local_path(url) is not None:
                try:
                    path = Path(markdown.local_path(url)).expanduser()
                    if not path.is_absolute():
                        rows = self.selected()
                        base = rows[0].get('cwd') if rows else None
                        if not base:
                            raise ValueError('这条对话未记录工作目录，无法定位相对路径。')
                        path = Path(base) / path
                    path = path.resolve()
                    if not path.exists():
                        raise ValueError('本地路径不存在：' + str(path))
                    if os.name == 'nt':
                        command = ['explorer.exe', str(path)] if path.is_dir() else ['explorer.exe', '/select,', str(path)]
                    else:
                        command = ['xdg-open', str(path if path.is_dir() else path.parent)]
                    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    self.render_info.set('已定位本地路径：' + str(path))
                except (OSError, ValueError) as error:
                    self.render_info.set(str(error))
            return 'break'

        def show_messages(self, position=None, prepared=None, request=None, anchor=None):
            if request is not None and request != getattr(self, 'render_request', 0):
                return
            if request is None:
                cached = getattr(self, 'reader_cache', None)
                view = (self.current_iid, cached[0]['path'] if cached else None,
                        self.reader_stamp, self.message_limit,
                        self.preview_mode.get(), self.find_query.get())
                if view == getattr(self, 'reader_view', None):
                    return
                self.reader_view = view
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
                self.background(prepare, lambda result: self.show_messages(position, result, request, anchor),
                                label='准备对话排版…', refresh_preview=False)
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
                            panel = bindings.ToolPanel(self, rows, key, self.tool_states, P)
                            panel.mark = mark
                            self.tool_panels.append(panel)
                            self.markdown_renderer.widgets.append(panel.frame)
                            self.text.window_create('end', window=panel.frame, padx=4, pady=2)
                            self.text.insert('end', '\n', 'body')
                    else:
                        for tag, chunk in bindings.render_messages([row], None):
                            if tag == 'body' and self.preview_mode.get() == 'Markdown':
                                if prepared:
                                    yield from self.markdown_renderer.iter_render(row['text'] or '',
                                        prepared[0].get(index), prepared[1])
                                else:
                                    yield from self.markdown_renderer.iter_render(row['text'] or '')
                            else:
                                if self.preview_mode.get() == 'Markdown' and tag.startswith('head_'):
                                    chunk = chunk.lstrip('\n')
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
                        if time.perf_counter() >= deadline:
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
                        physical = bindings.physical_row(r)
                        stamp = bindings.transcript_stamp(physical)
                        messages = bindings.file_messages(physical)
                        if stamp != bindings.transcript_stamp(physical):
                            raise ValueError('会话在读取过程中变化，请刷新后再查看。')
                        return physical, messages, stamp, None
                    except (ValueError, OSError) as error:
                        return r, [], None, error
                stamp = bindings.transcript_stamp(r)
                size = sum(entry[1] for entry in json.loads(stamp)) if isinstance(stamp, str) else stamp[0]
                if loaded is None:
                    cached = getattr(self, 'reader_cache', None)
                    if cached and cached[0]['path'] == r['path'] and cached[2] == stamp:
                        loaded = (bindings.physical_row(r), *cached[1:])
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
                self.message_limit = bindings.MESSAGE_LIMIT
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
                    members = conversations.members(db, member['conversation'])
                    for item in members:
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
                origin = bindings.original_of(db, r)
            default = support.preferred_target(r, list(self.target_labels), self.preferences)
            self.title_var.set(r['title'] or '(无标题)')
            lines = [f"{r['tool']} · {r['source']} · {r['turns']} 轮",
                     f"更新于 {(r['updated'] or '')[:16].replace('T', ' ')}", r['id']]
            if r.get('model'):
                lines.append('模型：' + r['model'])
            if bindings.KIND_LABELS.get(r['kind']):
                lines.append('会话类型：' + bindings.KIND_LABELS[r['kind']])
            if member:
                lines.append(f'版本：{len(members)} 个 · 分支：{len(heads)} 个')
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
                        bindings.launch_plan(db, r, candidate, cwd)
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
            self.reader_view = None
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

    return ReaderController
