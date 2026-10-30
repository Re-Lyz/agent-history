"""Listing behavior for the history window; dependencies are bound per window."""
import contextlib
import datetime
import json
from pathlib import Path
import agent_history as ah
import trash
import ui_support as support
import connections
import conversation_store as conversations
import conversation_titles as titles


def create_mixin(bindings, desktop):
    simpledialog = desktop.simpledialog

    class ListingController:
        def title_for_display(self, row):
            return titles.display(row, self.source_box['values'])

        def sort_list(self, column):
            self.sort_descending = not self.sort_descending if self.sort_column == column else column == 'time'
            self.sort_column = column
            if hasattr(self, 'all_rows'):
                if getattr(self, 'list_total', len(self.all_rows)) > len(self.all_rows):
                    self.refresh()
                    return
                selected = next(iter(self.tree.selection()), None)
                rows = support.sort_history(self.all_rows, self.sort_column, self.sort_descending)
                self.display_rows(rows, selected, self.tree.yview()[0])
            else:
                self.refresh()

        def refresh(self):
            self.list_request = getattr(self, 'list_request', 0) + 1
            request = self.list_request
            source = '' if self.source.get() == '全部来源' else self.source.get()
            query = self.search_text()
            imports, internal = self.show_imports.get(), self.show_internal.get()
            only = set(self.candidates) if self.only_review.get() else None
            project_label = self.project.get()
            project_map, projects_dirty = dict(self.project_map), self.projects_dirty
            column, descending = self.sort_column, self.sort_descending
            selected = next(iter(self.tree.selection()), None)
            selected_row = self.rows.get(selected)
            keep_paths = {row['path'] for iid, row in self.rows.items() if iid in self.checked or iid == selected}
            list_position = self.tree.yview()[0]
            filters = (source, query, imports, internal, project_label,
                       None if only is None else tuple(sorted(only)))
            if filters != getattr(self, 'previous_filters', None):
                self.list_limit = bindings.LIST_PAGE
            self.previous_filters = filters
            limit = self.list_limit
            cancelled = lambda: request != self.list_request
            if not query:
                self.version_override = None
                self.version_iid = None

            def load():
                # All filesystem/database work runs off Tk. A scoped snapshot
                # shares resolved homes across both project and conversation lists.
                with connections.snapshot(), contextlib.closing(ah.connect()) as db:
                    choices = bindings.project_choices(db) if projects_dirty else project_map
                    label = project_label
                    if label not in choices:
                        label = next((name for name, path in choices.items() if path is not None and
                                      support.project_key(path) == support.project_key(label)), '全部项目')
                    rows = bindings.list_rows(db, source, query, imports, internal, only, limit=limit,
                                     project=choices.get(label), sort_by=column, descending=descending,
                                     indexed=True, logical=True, cancel=cancelled, keep_paths=keep_paths)
                    selection = selected
                    member = conversations.lookup(db, selected_row) if selected_row and not query else None
                    # Search keeps the matching physical version, even if another
                    # branch is the conversation's current display version.
                    if not query and not imports and only is None:
                        replacement = next((row for row in rows if member and
                                            row.get('conversation') == member['conversation']), None)
                        if replacement:
                            selection = f"{replacement['source']}|{replacement['path']}"
                    elif member:
                        owner = db.execute("SELECT durable_version FROM temporary_handoffs "
                                           "WHERE temporary_version=? AND state='cleaned'", (member['id'],)).fetchone()
                        if owner:
                            durable = db.execute('SELECT row_json FROM conversation_versions WHERE id=?',
                                                 (owner['durable_version'],)).fetchone()
                            native = json.loads(durable['row_json']) if durable else None
                            replacement = next((row for row in rows if native and row['path'] == native['path']), None)
                            if replacement:
                                selection = f"{replacement['source']}|{replacement['path']}"
                    return rows, selection, choices, label

            def done(result):
                if request != self.list_request:
                    return
                rows, selection, choices, label = result
                self.list_total = getattr(rows, 'total', len(rows))
                self.list_limit = max(self.list_limit, len(rows))
                self.project_map = choices
                self.project_box['values'] = list(choices)
                self.project.set(label)
                self.projects_dirty = False
                current_selection = next(iter(self.tree.selection()), None)
                if current_selection != selected:
                    if current_selection and not any(
                            current_selection == f"{row['source']}|{row['path']}" for row in rows):
                        # A selection made while this query ran can name a file
                        # just replaced by sync. Resolve that conversation in a
                        # fresh query before deleting its selected tree node.
                        self.refresh()
                        return
                    selection = current_selection
                self.display_rows(support.sort_history(rows, self.sort_column, self.sort_descending),
                                  selection, list_position)
                if getattr(self, 'initial_list_pending', False):
                    self.initial_list_pending = False
                    if not rows or not (ah.ROOT / 'connections.json').exists():
                        self.setup_prompted = True
                        self.notice('暂无历史，请通过配置向导选择本机历史目录。')
                        self.setup_sources()

            self.background(load, done, label='正在搜索历史…' if query else '正在读取历史…',
                            key='history-list', replace=True, refresh_preview=False)

        def display_rows(self, rows, selected, list_position):
            self.all_rows = rows
            order = (self.sort_column, self.sort_descending)
            if order != getattr(self, 'previous_sort', order):
                keep = self.checked | ({selected} if selected else set())
                last = max((index + 1 for index, row in enumerate(rows)
                            if f"{row['source']}|{row['path']}" in keep), default=0)
                self.list_limit = max(self.list_limit, last)
            self.previous_sort = order
            total = getattr(self, 'list_total', len(rows))
            has_more = total > self.list_limit
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
                title = (self.title_for_display(r) or r['cwd'] or r['id']).replace('\n', ' ')[:120]
                values = (bindings.CHECK_OFF, when, r['source'], r['turns'], title)
                if self.tree.exists(iid):
                    if tuple(map(str, self.tree.item(iid, 'values'))) != tuple(map(str, values)):
                        self.tree.item(iid, values=values)
                    if self.tree.index(iid) != index:
                        self.tree.move(iid, '', index)
                else:
                    self.tree.insert('', index, iid=iid, values=values)
            self.checked &= set(self.rows)      # keep ticks that are still visible
            for iid in self.rows:
                self.paint(iid)
            if not self.busy.get() or self.busy.get() in ('正在读取历史…', '正在搜索历史…'):
                self.busy.set(f'{len(rows)} 条')
            self.update_checks()
            if selected in self.rows:
                if self.tree.selection() != (selected,):
                    self.tree.selection_set(selected)
                self.tree.focus(selected)
                self.tree.yview_moveto(list_position)
            self.on_select()

        def paint(self, iid):
            on = iid in self.checked
            self.tree.set(iid, 'check', bindings.CHECK_ON if on else bindings.CHECK_OFF)
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
            self.tree.heading('check', text=bindings.CHECK_ON if full else bindings.CHECK_OFF)

        def selected(self):
            selected = [self.rows[i] for i in self.tree.selection() if i in self.rows]
            if len(selected) == 1 and getattr(self, 'version_override', None) and getattr(self, 'version_iid', None) == self.tree.selection()[0]:
                return [self.version_override]
            return selected

        def rename_current(self):
            rows = self.selected()
            if len(rows) != 1:
                return
            row = rows[0]
            value = simpledialog.askstring('修改对话名称',
                '修改整条逻辑对话名称，并同步已有 Codex 原生名称。\n正在使用的原生会话保留并提示待同步；留空恢复自动标题。',
                initialvalue=self.title_for_display(row), parent=self.root)
            if value is None:
                return
            def work():
                with trash.locked_db(reindex=False) as db:
                    return titles.rename_and_sync(db, row, value)
            def done(result):
                self.refresh()
                synced = sum(item['state'] == 'synced' for item in result['native'])
                pending = len(result['native']) - synced
                message = f'名称已保存；Codex 原生名称已同步 {synced} 项。'
                if pending:
                    reasons = '；'.join(dict.fromkeys(item.get('error', '待重试') for item in result['native'] if item['state'] != 'synced'))
                    message += f'另有 {pending} 项待同步：{reasons}'
                if result.get('index_pending'):
                    message += '本机标题缓存待刷新，请点击“刷新历史”重试。'
                self.notice(message)
            self.background(work, done, label='保存并同步对话名称…', key='rename', refresh_preview=False)

        def load_more_rows(self):
            self.list_limit += bindings.LIST_PAGE
            self.refresh()

    return ListingController
