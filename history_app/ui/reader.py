"""Reader behavior for the history window; dependencies are bound per window."""
import contextlib
import bisect
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
from history_app.reader_data import MessagePage, checkpoint
from history_app import performance


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
            window = getattr(self, 'reader_window', None)
            if window:
                stop = window[0]
                if stop:
                    self.reader_window = (max(0, stop - bindings.MESSAGE_LIMIT), stop)
                    self.show_messages()
                return
            if getattr(self.reader_messages, 'has_more', False):
                request = self.reader_request
                physical, messages, stamp, error = self.reader_cache
                cancel = lambda: request != self.reader_request
                def read():
                    checkpoint(cancel)
                    if bindings.transcript_stamp(physical) != stamp:
                        raise ValueError('会话已变化，请刷新后加载更早消息。')
                    page = bindings.file_message_page(physical, bindings.MESSAGE_LIMIT,
                                                      cursor=messages.cursor, cancel=cancel)
                    if bindings.transcript_stamp(physical) != stamp:
                        raise ValueError('会话在读取过程中变化，请刷新后再查看。')
                    return page
                def done(page):
                    if cancel():
                        return
                    merged = MessagePage([*page, *messages], page.cursor)
                    self.reader_messages = merged
                    self.reader_cache = physical, merged, stamp, None
                    self.message_limit = len(merged)
                    shifted = (anchor[0] + len(page), *anchor[1:]) if anchor else None
                    self.show_messages(anchor=(shifted, False), prepend=len(page))
                self.background(read, done, label='加载更早消息…', key='reader-page', refresh_preview=False)
                return
            self.message_limit += bindings.MESSAGE_LIMIT
            self.show_messages(anchor=(anchor, False))

        def load_full_reader(self):
            request = self.reader_request
            if getattr(self, 'reader_full_loading', None) == request:
                return
            self.reader_full_loading = request
            physical, messages, stamp, error = self.reader_cache
            cancel = lambda: request != self.reader_request
            anchor = self.reader_anchor()
            def read():
                try:
                    checkpoint(cancel)
                    if bindings.transcript_stamp(physical) != stamp:
                        raise ValueError('会话已变化，请刷新后搜索。')
                    result = bindings.file_messages(physical, cancel=cancel)
                    if bindings.transcript_stamp(physical) != stamp:
                        raise ValueError('会话在读取过程中变化，请刷新后再查看。')
                    return result
                except Exception:
                    if self.reader_full_loading == request:
                        self.reader_full_loading = None
                    raise
            def done(result):
                if cancel():
                    return
                self.reader_full_loading = None
                self.reader_messages = result
                self.reader_cache = physical, result, stamp, None
                self.reader_total = len(result)
                self.rendered_range = (0, 0)
                self.reader_view = None
                self.find_status.set('正在定位搜索结果…')
                self.find_search_key = None
                self.highlight_matches()
            self.background(read, done, label='搜索完整对话…', key='reader-full', replace=True, refresh_preview=False)

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
            self.render_info.set('内容已复制')

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

        def show_messages(self, position=None, prepared=None, request=None, anchor=None, prepend=0):
            if request is not None and request != getattr(self, 'render_request', 0):
                return
            if request is None:
                cached = getattr(self, 'reader_cache', None)
                view = (self.current_iid, cached[0]['path'] if cached else None,
                        self.reader_stamp, self.message_limit,
                        self.preview_mode.get(), getattr(self, 'reader_window', None))
                if view == getattr(self, 'reader_view', None):
                    return
                self.reader_view = view
                self.render_request = getattr(self, 'render_request', 0) + 1
            request = self.render_request
            count = len(self.reader_messages)
            start, stop = getattr(self, 'reader_window', None) or (max(0, count - self.message_limit), count)
            if prepend:
                start, stop = 0, prepend
            shown = min(count, stop - start)
            more = (shown < count and not prepend) or getattr(self.reader_messages, 'has_more', False)
            if getattr(self, 'reader_window', None) and not prepend:
                more = start > 0
            large = sum(len(row['text'] or '') for row in self.reader_messages[start:stop]) > 50_000
            math = any(markdown.formulas.might_have_math(row['text'] or '') for row in self.reader_messages[start:stop]
                       if row['role'] in ('user', 'assistant', 'agent'))
            if (large or math) and prepared is None and self.preview_mode.get() == 'Markdown':
                messages = list(self.reader_messages)
                math_options = self.markdown_renderer.math_options()
                def prepare():
                    result, highlights, rasters = {}, {}, {}
                    pixels = 0
                    for index in range(start, stop):
                        checkpoint(lambda: request != self.render_request)
                        row = messages[index]
                        if row['role'] in ('user', 'assistant', 'agent'):
                            elements = markdown.parse(row['text'] or '')
                            result[index] = elements
                            for element in elements:
                                checkpoint(lambda: request != self.render_request)
                                if isinstance(element, markdown.Code):
                                    key = (element.text, element.language)
                                    highlights[key] = tuple(markdown.highlighted(*key))
                                cells = (span for row in element.rows for cell in row for span in cell) \
                                    if isinstance(element, markdown.Table) else (element,)
                                for span in cells:
                                    if isinstance(span, markdown.Formula) and span not in rasters:
                                        checkpoint(lambda: request != self.render_request)
                                        raster = markdown.formulas.render(span, **math_options) if pixels < 8_000_000 else None
                                        rasters[span] = raster
                                        if raster:
                                            pixels += raster.width * raster.height
                    return result, highlights, rasters
                self.background(prepare, lambda result: self.show_messages(position, result, request, anchor, prepend),
                                label='准备对话排版…', key='markdown', replace=True, refresh_preview=False)
                return
            total = getattr(self, 'reader_total', None) or (None if more else count)
            displayed = count if prepend else shown
            label = f'{displayed} / {total}' if total is not None else str(displayed)
            window_note = f' · 第 {start + 1}–{stop} 条' if getattr(self, 'reader_window', None) else ''
            self.message_count.set(f'已显示 {label} 条消息{window_note} · 工具可展开详情')
            self.more_messages.state(['!disabled'] if more else ['disabled'])
            self.rendering = True
            started = time.monotonic()
            self.rendered_range = (0, count) if prepend else (start, stop)
            if prepend:
                self.text.configure(state='normal')
                old_marks = list(self.message_marks)
                if old_marks:
                    self.text.delete('1.0', old_marks[0][1])
                for index, name in reversed(old_marks):
                    temporary = 'old_' + name
                    self.text.mark_set(temporary, name)
                    self.text.mark_unset(name)
                self.message_marks = []
                for index, name in old_marks:
                    renamed = f'message_{index + prepend}'
                    self.text.mark_set(renamed, 'old_' + name)
                    self.text.mark_gravity(renamed, 'right')
                    self.text.mark_unset('old_' + name)
                    self.message_marks.append((index + prepend, renamed))
                renamed_marks = {name: f'message_{index + prepend}' for index, name in old_marks}
                for panel in self.tool_panels:
                    panel.mark = renamed_marks.get(panel.mark, panel.mark)
                self.text.mark_set('reader_insert', '1.0')
                self.text.mark_gravity('reader_insert', 'right')
            else:
                self.begin_text()
            self.markdown_renderer.index = 'reader_insert' if prepend else 'end'
            if more and not prepend:
                self.text.insert('end', f'（显示最近 {shown} 条；可加载更早消息）\n\n', 'note')
            groups = tool_details.groups(self.reader_messages[start:stop])
            destination = 'reader_insert' if prepend else 'end'
            def render():
                for index in range(start, stop):
                    row = self.reader_messages[index]
                    mark = f'message_{index}'
                    self.text.mark_set(mark, destination if prepend else 'end-1c')
                    self.text.mark_gravity(mark, 'left')
                    self.message_marks.append((index, mark))
                    if row['role'] in ('tool_call', 'tool_output') and self.preview_mode.get() == 'Markdown':
                        paired = groups.get(index - start)
                        if paired is not None:
                            rows = [self.reader_messages[number + start] for number in paired]
                            locator = row.get('detail', {})
                            key = str((locator.get('path') or self.current_iid,
                                       locator.get('offset', locator.get('ordinal', index)), row['role'], locator.get('block')))
                            panel = bindings.ToolPanel(self, rows, key, self.tool_states, P)
                            panel.mark = mark
                            self.tool_panels.append(panel)
                            self.markdown_renderer.widgets.append(panel.frame)
                            self.text.window_create(destination, window=panel.frame, padx=4, pady=2)
                            self.text.insert(destination, '\n', 'body')
                    else:
                        for tag, chunk in bindings.render_messages([row], None):
                            if tag == 'body' and self.preview_mode.get() == 'Markdown':
                                if prepared:
                                    yield from self.markdown_renderer.iter_render(row['text'] or '',
                                        prepared[0].get(index), prepared[1], prepared[2])
                                else:
                                    yield from self.markdown_renderer.iter_render(row['text'] or '')
                            else:
                                if self.preview_mode.get() == 'Markdown' and tag.startswith('head_'):
                                    chunk = chunk.lstrip('\n')
                                self.text.insert(destination, chunk, tag)
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
                    self.rendering = False
                    self.message_marks.sort()
                    for _, name in self.message_marks:
                        self.text.mark_gravity(name, 'left')
                    self.markdown_renderer.index = 'end'
                    performance.record('render', time.monotonic() - started, messages=shown)
                    self.end_text(position)
                    if anchor:
                        self.restore_reader_anchor(*anchor)
            batch()

        def schedule_find(self):
            if getattr(self, 'setting_find_query', False):
                return
            previous = getattr(self, 'find_after', None)
            if previous:
                self.root.after_cancel(previous)
            self.find_request = getattr(self, 'find_request', 0) + 1
            self.find_search_key = None
            self.find_targets, self.find_totals = [], []
            if not self.find_query.get():
                self.highlight_matches()
            else:
                self.find_status.set('正在查找…')
                def find():
                    self.find_after = None
                    self.highlight_matches()
                self.find_after = self.root.after(150, find)

        def highlight_matches(self):
            if getattr(self, 'setting_find_query', False):
                return
            self.text.tag_remove('find', '1.0', 'end')
            self.text.tag_remove('find_current', '1.0', 'end')
            self.find_matches, self.find_index = [], -1
            query = self.find_query.get()
            self.markdown_renderer.highlight_math(query)
            if query and getattr(self.reader_messages, 'has_more', False):
                self.find_status.set('正在搜索完整对话…')
                self.load_full_reader()
                return
            if not query:
                self.find_status.set('Ctrl+F 搜索此对话')
                self.find_targets, self.find_totals = [], []
                return
            key = (self.reader_request, query, len(self.reader_messages))
            if key != getattr(self, 'find_search_key', None):
                self.find_search_key = key
                request = getattr(self, 'find_request', 0)
                messages = tuple(self.reader_messages)
                cancel = lambda: request != getattr(self, 'find_request', 0) or key != self.find_search_key
                def search():
                    targets, totals, total = [], [], 0
                    for index, row in enumerate(messages):
                        checkpoint(cancel)
                        count = (row['text'] or '').casefold().count(query.casefold())
                        if count:
                            total += count
                            targets.append(index)
                            totals.append(total)
                    return targets, totals
                def done(result):
                    if cancel():
                        return
                    self.find_targets, self.find_totals = result
                    self.find_global_index = -1
                    if self.find_targets:
                        self.find_message(1)
                    else:
                        self.find_status.set('0 处')
                self.background(search, done, label='定位搜索结果…', key='reader-search', replace=True, refresh_preview=False)
                return
            if getattr(self, 'rendering', False):
                return
            start = '1.0'
            count = tk.IntVar()
            render_request = self.render_request
            def scan(start):
                if key != self.find_search_key or render_request != self.render_request:
                    return
                for _ in range(100):
                    match = self.text.search(query, start, stopindex='end', nocase=True, exact=True, count=count, elide=True)
                    if not match:
                        self.finish_find()
                        return
                    end = self.text.index(f'{match}+{count.get()}c')
                    self.find_matches.append((match, end))
                    self.text.tag_add('find', match, end)
                    start = end
                self.root.after(1, lambda: scan(start))
            scan(start)
            tools = []
            for panel in getattr(self, 'tool_panels', []):
                if any(query.casefold() in row['text'].casefold() for row in panel.rows):
                    if not panel.expanded:
                        panel.toggle()
                    if panel.loaded:
                        panel.highlight()
                    tools.append(panel)
            if tools and not self.find_matches:
                panel = tools[0]
                def locate():
                    if panel.frame.winfo_exists():
                        self.text.see(panel.mark)
                self.root.after_idle(locate)
                self.find_status.set(f'{len(tools)} 个工具详情匹配（展开读取完整记录）')
                return
        def finish_find(self):
            total = self.find_totals[-1] if getattr(self, 'find_totals', None) else len(self.find_matches)
            self.find_status.set(f'{total} 处')
            if getattr(self, 'pending_find', False):
                self.pending_find = False
                self.locate_find()

        def locate_find(self):
            if not getattr(self, 'find_targets', None):
                return
            target = bisect.bisect_right(self.find_totals, self.find_global_index)
            index = self.find_targets[target]
            mark = next((name for number, name in self.message_marks if number == index), None)
            if not mark:
                return
            following = next((name for number, name in self.message_marks if number > index), 'end-1c')
            matches = [(start, end) for start, end in self.find_matches
                       if self.text.compare(start, '>=', mark) and self.text.compare(start, '<', following)]
            occurrence = self.find_global_index - (self.find_totals[target - 1] if target else 0)
            self.text.tag_remove('find_current', '1.0', 'end')
            if matches:
                start, end = matches[min(occurrence, len(matches) - 1)]
                self.text.tag_add('find_current', start, end)
                self.text.see(start)
            else:
                self.text.see(mark)
            self.find_status.set(f'{self.find_global_index + 1} / {self.find_totals[-1]}')

        def find_message(self, direction):
            if getattr(self, 'find_totals', None):
                total = self.find_totals[-1]
                self.find_global_index = (getattr(self, 'find_global_index', -1) + direction) % total
                target = self.find_targets[bisect.bisect_right(self.find_totals, self.find_global_index)]
                start, stop = getattr(self, 'rendered_range', (0, 0))
                self.pending_find = True
                if not start <= target < stop:
                    start = max(0, min(target - bindings.MESSAGE_LIMIT // 2,
                                       len(self.reader_messages) - bindings.MESSAGE_LIMIT))
                    self.reader_window = (start, min(len(self.reader_messages), start + bindings.MESSAGE_LIMIT))
                    self.find_status.set('正在显示搜索结果…')
                    self.show_messages()
                elif getattr(self, 'rendering', False):
                    return
                else:
                    self.highlight_matches()
                return
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
                self.preview_request = getattr(self, 'preview_request', 0) + 1
                self.find_search_key = None
                selection = next(iter(self.tree.selection()), None)
                if selection != self.current_iid:
                    self.reader_window = None
                    self.find_targets, self.find_totals = [], []
                    self.render_request = getattr(self, 'render_request', 0) + 1
                    self.reader_view = None
                    self.rendering = False
            read_request = self.reader_request
            cancel = lambda: read_request != self.reader_request
            read_query = self.search_text()
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
                        messages = bindings.file_messages(physical, cancel=cancel) if read_query else \
                            bindings.file_message_page(physical, bindings.MESSAGE_LIMIT, cancel=cancel)
                        if stamp != bindings.transcript_stamp(physical):
                            raise ValueError('会话在读取过程中变化，请刷新后再查看。')
                        return physical, messages, stamp, None
                    except (ValueError, OSError) as error:
                        return r, [], None, error
                if loaded is None:
                    cached = getattr(self, 'reader_cache', None)
                    def cached_read():
                        checkpoint(cancel)
                        try:
                            stamp = bindings.transcript_stamp(r)
                            if cached and cached[0]['path'] == r['path'] and cached[2] == stamp:
                                return (bindings.physical_row(r), *cached[1:])
                            return read()
                        except (ValueError, OSError) as error:
                            return r, [], None, error
                    self.reader_stamp = None
                    self.title_var.set(self.title_for_display(r) or r['id'])
                    self.launch_button.state(['disabled'])
                    self.copy_button.state(['disabled'])
                    request = self.reader_request
                    self.background(cached_read, lambda result: self.on_select(result, request),
                                    label='读取对话…', key='reader', replace=True, refresh_preview=False)
                    return
                r, messages, stamp, error = loaded
                if error:
                    raise error
            except (ValueError, OSError) as error:
                self.reader_messages, self.reader_stamp = [], None
                self.message_count.set('')
                self.more_messages.state(['disabled'])
                self.title_var.set(self.title_for_display(r) or r['id'])
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
            self.reader_full_loading = None
            self.current_iid = iid
            if not same:
                self.message_limit = bindings.MESSAGE_LIMIT
            self.reader_messages, self.reader_stamp = messages, stamp
            self.reader_total = r.get('message_count') if r.get('view_stamp') == json.dumps(stamp) else None
            if not getattr(messages, 'has_more', False):
                self.reader_total = len(messages)
                r['turns'] = sum(message['role'] == 'user' for message in messages)
            if not getattr(self, 'version_override', None):
                self.rows[iid] = r
            self.load_selection_metadata(r, read_request)
            default = support.preferred_target(r, list(self.target_labels), self.preferences)
            self.title_var.set(self.title_for_display(r) or '(无标题)')
            lines = [f"{r['tool']} · {r['source']} · {r['turns']} 轮",
                     f"更新于 {(r['updated'] or '')[:16].replace('T', ' ')}", r['id']]
            if r.get('model'):
                lines.append('模型：' + r['model'])
            if bindings.KIND_LABELS.get(r['kind']):
                lines.append('会话类型：' + bindings.KIND_LABELS[r['kind']])
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
            self.show_messages(position)
            if matched:
                self.highlight_matches()
            target = previous_target if same and previous_target in self.target_labels else default
            cwd = previous_cwd if same else (r['cwd'] or '')
            self.choose_preview_target = not same
            self.target.set(self.target_labels.get(target, ''))
            self.cwd.set(cwd)
            self.update_preview()

        def load_selection_metadata(self, row, request):
            cancel = lambda: request != self.reader_request
            self.version_options = {}
            self.version_box['values'] = []
            def work():
                checkpoint(cancel)
                with contextlib.closing(ah.connect()) as db:
                    titled = titles.apply(db, row)
                    member = conversations.lookup(db, titled)
                    options, selected, heads, members = {}, '', set(), []
                    if member:
                        members = conversations.members(db, member['conversation'])
                        heads = {item['id'] for item in conversations.tips(db, member['conversation'], _items=members)}
                        for item in members:
                            checkpoint(cancel)
                            version_row = dict(json.loads(item['row_json']), conversation=member['conversation'])
                            kind = '接续版本' if item['parent_revision'] else '原会话'
                            if version_row.get('kind') == 'import':
                                kind = '已续聊副本' if item['baseline'] and item['head'] != item['baseline'] else '导入副本'
                            state = '并行分支' if item['id'] in heads and len(heads) > 1 else \
                                    '当前版本' if item['id'] in heads else '历史版本'
                            label = version_row['source'] + ' / ' + version_row['id'][:13] + ' / ' + kind + ' / ' + state
                            options[label] = version_row
                            if item['id'] == member['id']:
                                selected = label
                    origin = bindings.original_of(db, titled)
                    extra = [f'版本：{len(members)} 个 · 分支：{len(heads)} 个'] if member else []
                    failure = db.execute('SELECT stage,error FROM index_failures WHERE path=?', (row['path'],)).fetchone()
                    if failure:
                        extra.append('索引待重试：' + failure['error'])
                    if origin is not titled:
                        extra.append(f"导入自 {origin['tool']} · {origin['source']} · {origin['id'][:13]}")
                    return titled, options, selected, extra
            def done(result):
                if cancel():
                    return
                titled, self.version_options, selected, extra = result
                displayed = self.title_for_display(titled)
                self.title_var.set(displayed or '(无标题)')
                if displayed != (titled['title'] or '').strip():
                    extra.append('原始标题：' + titled['title'])
                self.version_box['values'] = list(self.version_options)
                self.version_value.set(selected)
                if extra:
                    self.info.set(self.info.get() + '\n' + '\n'.join(extra))
            self.background(work, done, label='读取版本信息…', key='selection-details', replace=True, refresh_preview=False)

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
