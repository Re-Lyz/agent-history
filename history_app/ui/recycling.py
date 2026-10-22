"""Recycling behavior for the history window; dependencies are bound per window."""
import contextlib
import datetime
from pathlib import Path
import agent_history as ah
import trash
import ui_support as support
import conversation_titles as titles


def create_mixin(bindings, desktop):
    messagebox = desktop.messagebox

    class RecyclingController:
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

    return RecyclingController
