"""Recycle bin for Codex and Claude Code conversations. Standard library only.

  list                         show recycle-bin entries and their expiry dates
  delete ID --source S         move one conversation (and optionally its imported copies) to the bin
  restore ENTRY                put an entry's conversations back, including native /resume rows
  purge [--entry E]            permanently remove expired entries (or one entry now)

Deleting moves the transcript into ~/.agent-history/trash and removes the native /resume
row; nothing is lost until the entry expires after RETENTION_DAYS. Import tombstones are kept,
so background sync does not recreate deleted copies.
"""
import argparse
from collections import defaultdict
import contextlib
import datetime
from functools import wraps
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import threading
import uuid

import agent_history as ah
import codex_import as ci
from setup_backend import atomic_json

RETENTION_DAYS = 30
SETTLE_SECONDS = 120     # a transcript written this recently may still be open
_bin_owner = threading.local()


@contextlib.contextmanager
def bin_lock(*, wait=True):
    """Serialize all bin mutations, including purge outside the collector lock.

    Callers may already hold collect.lock; never acquire it from inside this lock.
    Nested bin operations in the same thread reuse the OS lock.
    """
    root = ah.DATA.resolve()
    if getattr(_bin_owner, 'root', None) == root:
        yield True
        return
    with ah.file_lock(root / 'trash.lock', wait=wait) as locked:
        if not locked:
            yield False
            return
        previous = getattr(_bin_owner, 'root', None)
        _bin_owner.root = root
        try:
            yield True
        finally:
            _bin_owner.root = previous


def serialized_bin(work):
    @wraps(work)
    def run(*args, **kwargs):
        with bin_lock() as locked:
            if not locked:
                raise TimeoutError('回收站正在使用，请稍后重试。')
            return work(*args, **kwargs)
    return run


def trash_root():
    return ah.DATA / 'trash'


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


@contextlib.contextmanager
def locked_db(*, reindex=True, lock_timeout=None, cancel=None, on_wait=None, on_acquired=None):
    """Hold the collector lock so scheduled sync cannot race a delete or restore."""
    from history_app.locks import waiting_lock
    lock = ah.collect_lock(wait=True) if lock_timeout is None else waiting_lock(
        ah.DATA / 'collect.lock', lock_timeout, cancel=cancel, on_wait=on_wait, on_acquired=on_acquired)
    with lock as locked:
        if not locked:
            if lock_timeout is not None:
                raise TimeoutError('等待历史同步超时，尚未准备启动任务或打开终端；可稍后重试。')
            raise ValueError('另一个历史任务正在运行，请稍后再试。')
        db = ah.connect()
        try:
            if reindex:
                ah.collect(db)
            yield db
            if reindex:
                ah.collect(db)
        finally:
            db.close()


# ---------------------------------------------------------------- append-only caches

@contextlib.contextmanager
def exclusive_stream(path):
    """Deny concurrent writers while rewriting a small append-only cache."""
    if os.name != 'nt':
        with Path(path).open('r+b') as stream:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield stream
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt
    create = ctypes.WinDLL('kernel32', use_last_error=True).CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY), 'r+b') as stream:
        yield stream


def edit_cache(path, edit):
    """Apply edit(bytes) -> bytes under an exclusive handle; retry briefly if a writer holds it."""
    for attempt in range(10):
        try:
            with exclusive_stream(path) as stream:
                original = stream.read()
                updated = edit(original)
                if updated != original:
                    try:
                        stream.seek(0)
                        stream.write(updated)
                        stream.truncate()
                        stream.flush()
                        os.fsync(stream.fileno())
                    except BaseException:
                        stream.seek(0)
                        stream.write(original)
                        stream.truncate()
                        raise
                return True
        except PermissionError:
            time.sleep(0.2)
    return False


def remove_cache_lines(path, field, sid, on_save=None):
    """Remove one session's lines from a name/prompt cache; return them for restore (None if busy)."""
    path = Path(path)
    if not path.exists():
        return []
    removed = []

    def edit(data):
        removed.clear()
        keep = []
        for line in data.splitlines(keepends=True):
            try:
                matched = json.loads(line).get(field) == sid
            except (ValueError, AttributeError):
                matched = False
            (removed if matched else keep).append(line)
        if on_save:
            on_save([line.decode('utf-8', 'replace') for line in removed])
        return b''.join(keep)
    if not edit_cache(path, edit):
        return None
    return [line.decode('utf-8', 'replace') for line in removed]


def restore_cache_lines(path, lines):
    if not lines:
        return
    body = ''.join(l if l.endswith('\n') else l + '\n' for l in lines).encode('utf-8')
    path = Path(path)
    if not path.exists():
        path.write_bytes(body)
        return
    def append(data):
        existing = set(data.splitlines())
        missing = b''.join(line + b'\n' for line in body.splitlines() if line not in existing)
        return data + (b'' if not data or data.endswith(b'\n') else b'\n') + missing
    if not edit_cache(path, append):
        raise OSError('历史缓存正在使用，请稍后重试恢复。')


# ---------------------------------------------------------------- native Codex state

def native_remove(home, sid, on_save=None):
    """Remove the /resume row of a thread whose rollout is gone; return the rows for restore."""
    try:
        state = ci.state_path(home)
    except ValueError:
        return {}
    db = sqlite3.connect(state, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (sid,)).fetchone()
        if not row or Path(row['rollout_path']).exists():
            return {}    # no row, or it points at another copy that still exists
        history = Path(home) / 'thread_history_1.sqlite'
        if history.exists():
            db.execute('ATTACH DATABASE ? AS history_cache', (str(history),))
        saved = {}
        with db:
            for (table,) in db.execute("SELECT name FROM main.sqlite_master WHERE type='table'").fetchall():
                cols = {c['name'] for c in db.execute(f'PRAGMA main.table_info({ci.quoted(table)})')}
                if table == 'threads':
                    where, args = 'id=?', (sid,)
                elif 'thread_id' in cols:
                    where, args = 'thread_id=?', (sid,)
                elif {'parent_thread_id', 'child_thread_id'} <= cols:
                    where, args = 'parent_thread_id=? OR child_thread_id=?', (sid, sid)
                else:
                    continue
                rows = db.execute(f'SELECT * FROM main.{ci.quoted(table)} WHERE {where}', args).fetchall()
                if rows:
                    saved[table] = [dict(r) for r in rows]
                    if on_save:
                        on_save({'rows': saved})
                    db.execute(f'DELETE FROM main.{ci.quoted(table)} WHERE {where}', args)
            if history.exists():
                # The paginated projection is rebuilt from the rollout; it need not be saved.
                for (table,) in db.execute("SELECT name FROM history_cache.sqlite_master WHERE type='table'").fetchall():
                    cols = [r['name'] for r in db.execute(f'PRAGMA history_cache.table_info({ci.quoted(table)})')]
                    if 'thread_id' in cols:
                        db.execute(f'DELETE FROM history_cache.{ci.quoted(table)} WHERE thread_id=?', (sid,))
        return {'rows': saved}
    finally:
        db.close()


def native_restore(home, native):
    if not native.get('rows'):
        return
    db = sqlite3.connect(ci.state_path(home), timeout=30)
    try:
        with db:
            for table, rows in native['rows'].items():
                cols = {c[1] for c in db.execute(f'PRAGMA table_info({ci.quoted(table)})')}
                for row in rows:
                    values = {k: v for k, v in row.items() if k in cols}
                    if values:
                        db.execute(f'INSERT OR IGNORE INTO {ci.quoted(table)}({",".join(map(ci.quoted, values))}) '
                                   f'VALUES({",".join("?" for _ in values)})', list(values.values()))
    finally:
        db.close()


def fork_index(db):
    """(source, parent id) -> fork transcripts. Native forks read their parent's rollout,
    so deleting the parent alone would break their resume. Built once per batch."""
    index = defaultdict(list)
    # Discover headers directly: newly created forks must protect their parent
    # even when the message index has not been refreshed yet.
    for tool, source, path, _titles in ah.discover():
        if tool != 'codex':
            continue
        with contextlib.suppress(ValueError, OSError):
            base = (ci.first_meta(path).get('history_base') or {}).get('thread_id')
            if base:
                index[(source, base)].append(str(path.resolve()))
    return index


# ---------------------------------------------------------------- items

def within(path, roots):
    path = Path(path).resolve()
    for root in roots:
        with contextlib.suppress(ValueError):
            path.relative_to(Path(root).resolve())
            return path
    raise ValueError(f'不在已知历史目录中，拒绝处理：{path}')


def describe(db, tool, source, path):
    """Normalize a deletion target to (tool, source, home, sid, path, key, title)."""
    if tool == 'codex':
        homes = dict(ah.codex_homes())
        if source not in homes:
            raise ValueError('未知连接：' + source)
        home = homes[source]
        path = within(path, [home / 'sessions', home / 'archived_sessions'])
        sid = ah.codex_session_id(path)
        if not sid:
            raise ValueError(f'无法读取会话 ID：{path}')
    elif tool == 'claude':
        homes = [h.resolve() for h in ah.claude_homes()]
        path = within(path, [h / 'projects' for h in homes])
        home, sid = next(h for h in homes if path.is_relative_to(h)), path.stem
    else:
        raise ValueError('未知工具：' + tool)
    key = f'{tool}:{sid}'
    row = db.execute('SELECT title, kind, cwd FROM sessions WHERE key=?', (key,)).fetchone()
    cwd = (row and row['cwd']) or ''
    if tool == 'codex':
        cwd = ci.first_meta(path).get('cwd') or cwd
    return dict(tool=tool, source=source, home=str(Path(home).resolve()), sid=sid, path=str(path), key=key,
                title=(row and row['title']) or sid, kind=(row and row['kind']) or '', cwd=cwd)


def related_copies(db, key):
    """Imported copies made from this session that still exist (other Codex connections and Claude)."""
    out = []
    for r in db.execute('SELECT target, dst_key, dst_path FROM codex_imports WHERE src_key=?', (key,)).fetchall():
        if Path(r['dst_path']).exists():
            out.append(dict(tool='codex', source=r['target'], path=r['dst_path']))
    for r in db.execute('SELECT dst_key, dst_path FROM imports WHERE src_key=?', (key,)).fetchall():
        if Path(r['dst_path']).exists():
            out.append(dict(tool='claude', source='claude', path=r['dst_path']))
    return out


def remove_item(db, item, folder, index, excluded, forks, save):
    path = Path(item['path'])
    if not path.is_file():
        raise ValueError(f'文件已不存在：{path}')
    if time.time() - path.stat().st_mtime < SETTLE_SECONDS:
        raise ValueError(f'“{item["title"]}”两分钟内刚有写入，可能仍在使用；请关闭对应窗口后再删除。')
    stored = f'{index:02d}-{path.name}'
    record = dict(item, stored=stored, size=path.stat().st_size, sha256=ci.file_hash(path), native={}, caches={})
    def save_native(native):
        record['native'] = native
        save(record)
    def save_cache(cache, field):
        def journal(lines):
            record['caches'][str(cache)] = lines
            save(record)
        if remove_cache_lines(cache, field, item['sid'], on_save=journal) is None:
            raise OSError('历史缓存正在使用，请稍后重试。')
    if item['tool'] == 'codex':
        home = Path(item['home'])
        children = [p for p in forks.get((item['source'], item['sid']), []) if p not in excluded]
        if children:
            raise ValueError(f'“{item["title"]}”被 {len(children)} 个原生分支会话引用，删除会破坏它们的恢复：\n'
                             + '\n'.join(children[:3]))
        with ci.writer_lock(home, item['sid']) as free:
            if not free:
                raise ValueError(f'“{item["title"]}”正在 Codex 中打开，请先关闭。')
            save(record)
            shutil.move(str(path), str(folder / stored))
            try:
                record['native'] = native_remove(home, item['sid'], on_save=save_native)
            except BaseException:
                shutil.move(str(folder / stored), str(path))
                raise
        for name, field in (('session_index.jsonl', 'id'), ('history.jsonl', 'session_id')):
            save_cache(home / name, field)
    else:
        import handoff_lifecycle
        if handoff_lifecycle.claude_running(dict(item, id=item['sid'])):
            raise ValueError('这条对话正在 Claude 中使用，请先退出对应的 Agent。')
        sidecar = path.with_suffix('')
        if sidecar.is_dir():
            record['sidecar'] = str(sidecar)
        save(record)
        shutil.move(str(path), str(folder / stored))
        sidecar = path.with_suffix('')          # subagent transcripts and tool results
        if sidecar.is_dir():
            shutil.move(str(sidecar), str(folder / (stored + '.d')))
            record['sidecar'] = str(sidecar)
        cache = Path(item['home']) / 'history.jsonl'
        save_cache(cache, 'sessionId')
    ah.forget_file(db, item['path'])
    db.commit()
    save(record)
    return record


def write_manifest(folder, manifest):
    atomic_json(folder / 'manifest.json', manifest)


@serialized_bin
def delete(db, targets, label=None, excluded=(), forks=None, skip_errors=False):
    """Move targets [{tool, source, path}] into one recycle-bin entry. Returns the manifest.

    `excluded` are other paths being deleted in the same batch (a fork may go with its parent).
    With skip_errors, an item that cannot be removed is listed in manifest['skipped'] instead."""
    items = [describe(db, t['tool'], t['source'], t['path']) for t in targets]
    unique = {i['path']: i for i in items}
    items = list(unique.values())
    if not items:
        raise ValueError('没有选择要删除的对话。')
    forks = fork_index(db) if forks is None else forks
    deleted = utcnow()
    entry = deleted.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
    folder = trash_root() / entry
    folder.mkdir(parents=True)
    manifest = dict(id=entry, label=label or items[0]['title'], deleted_at=deleted.isoformat(),
                    expires_at=(deleted + datetime.timedelta(days=RETENTION_DAYS)).isoformat(), items=[])
    skipped = []
    def save(record):
        if record not in manifest['items']:
            manifest['items'].append(record)
        write_manifest(folder, manifest)
    try:
        for index, item in enumerate(items):
            try:
                record = remove_item(db, item, folder, index, set(unique) | set(excluded), forks, save)
            except (ValueError, OSError) as error:
                if not skip_errors:
                    raise
                skipped.append(dict(path=item['path'], title=item['title'], reason=str(error)))
                continue
    finally:
        manifest['items'] = [r for r in manifest['items'] if (folder / r['stored']).exists() or
                             (folder / (r['stored'] + '.d')).exists()]
        if manifest['items']:
            write_manifest(folder, manifest)
        elif not any(p.name != 'manifest.json' for p in folder.iterdir()):
            (folder / 'manifest.json').unlink(missing_ok=True)
            folder.rmdir()
    return dict(manifest, skipped=skipped)


def delete_many(db, groups):
    """Delete [(label, targets)] as one recycle-bin entry per conversation, so each can be
    restored on its own. Items that cannot be removed are skipped; the rest still go."""
    forks = fork_index(db)
    resolved = lambda group: {str(Path(t['path']).resolve()) for t in group}
    pending = set().union(*(resolved(g) for _, g in groups)) if groups else set()
    # Forks first: if one is skipped, its parent must then be kept as well.
    is_fork = lambda group: bool(any(str(Path(group[0]['path']).resolve()) in v for v in forks.values()))
    report = dict(entries=[], deleted=0, skipped=[])
    for label, group in sorted(groups, key=lambda g: not is_fork(g[1])):
        try:
            manifest = delete(db, group, label, pending, forks, skip_errors=True)
        except (ValueError, OSError) as error:
            manifest = dict(items=[], skipped=[dict(path=group[0]['path'], title=label, reason=str(error))])
        for item in manifest['skipped']:
            pending.discard(str(Path(item['path']).resolve()))
        report['skipped'] += manifest['skipped']
        if manifest['items']:
            report['entries'].append(manifest['id'])
            report['deleted'] += len(manifest['items'])
    return report


def entries():
    out = []
    for path in sorted(trash_root().glob('*/manifest.json'), reverse=True):
        with contextlib.suppress(ValueError, OSError):
            out.append(json.loads(path.read_text(encoding='utf-8')))
    return out


@serialized_bin
def restore(db, entry):
    folder = trash_root() / entry
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    remaining, errors, restored = [], [], 0
    outcomes = []
    for record in manifest['items']:
        original = Path(record['path'])
        stored = folder / record['stored']
        try:
            if stored.exists() and (stored.stat().st_size != record['size'] or
                                    ci.file_hash(stored) != record['sha256']):
                raise ValueError('回收站文件完整性校验失败，已保留恢复资料。')
            if original.exists():
                if not (record.get('restoring') or not stored.exists()) or ci.file_hash(original) != record['sha256']:
                    raise ValueError(f'原位置已有文件，未覆盖：{original}')
            else:
                record['restoring'] = True
                write_manifest(folder, manifest)
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(stored), str(original))
            if ci.file_hash(original) != record['sha256']:
                raise ValueError('恢复文件完整性校验失败，已保留恢复资料。')
            if record.get('sidecar') and (folder / (record['stored'] + '.d')).is_dir():
                if Path(record['sidecar']).exists():
                    raise ValueError('侧目录已有内容，未覆盖：' + record['sidecar'])
                shutil.move(str(folder / (record['stored'] + '.d')), record['sidecar'])
            if record['tool'] == 'codex':
                native_restore(record['home'], record.get('native') or {})
            for cache, lines in (record.get('caches') or {}).items():
                restore_cache_lines(cache, lines)
            restored += 1
            outcomes.append(dict(source=record['source'], sid=record['sid'], path=str(original),
                                 restored=True, reason=''))
        except (ValueError, OSError, sqlite3.Error) as error:
            remaining.append(record)
            errors.append(str(error))
            outcomes.append(dict(source=record['source'], sid=record['sid'], path=str(original),
                                 restored=False, reason=str(error)))
    if remaining:
        manifest['items'] = remaining
        write_manifest(folder, manifest)
    else:
        shutil.rmtree(folder)
    return dict(restored=restored, errors=errors, outcomes=outcomes)


def purge(entry=None, now=None):
    """Permanently remove one entry, or every entry past its expiry date."""
    with bin_lock(wait=False) as locked:
        if not locked:
            if entry is not None:
                raise ValueError('回收站正在恢复或整理，请稍后重试。')
            return []
        return _purge(entry, now)


def _purge(entry, now):
    now = now or utcnow()
    removed = []
    for manifest in entries():
        if entry is None and any(item.get('restoring') for item in manifest['items']):
            continue  # Failed recovery journals remain available for retry.
        expired = datetime.datetime.fromisoformat(manifest['expires_at']) <= now
        if manifest['id'] == entry or (entry is None and expired):
            shutil.rmtree(trash_root() / manifest['id'])
            removed.append(manifest['id'])
    return removed


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    d = sub.add_parser('delete')
    d.add_argument('id')
    d.add_argument('--source', required=True, help='Codex connection name, or claude')
    d.add_argument('--with-copies', action='store_true', help='also delete imported copies of this session')
    r = sub.add_parser('restore')
    r.add_argument('entry')
    p = sub.add_parser('purge')
    p.add_argument('--entry', help='remove this entry now instead of only expired ones')
    args = ap.parse_args()
    if args.cmd == 'list':
        for m in entries():
            print(f"{m['id']}  到期 {m['expires_at'][:10]}  {len(m['items'])} 项  {m['label']}")
        return
    if args.cmd == 'purge':
        return print(json.dumps(purge(args.entry), ensure_ascii=False))
    with locked_db() as db:
        if args.cmd == 'restore':
            return print(json.dumps(restore(db, args.entry), ensure_ascii=False))
        if args.source == 'claude':
            row = ah.resolve(db, args.id)
            targets = [dict(tool='claude', source='claude', path=row['path'])]
        else:
            row = ah.resolve_codex(db, args.source, args.id)
            targets = [dict(tool='codex', source=args.source, path=row['path'])]
        if args.with_copies:
            targets += related_copies(db, row['key'])
        manifest = delete(db, targets)
        print(f"已移到回收站 {manifest['id']}（{len(manifest['items'])} 项，{manifest['expires_at'][:10]} 到期）")


if __name__ == '__main__':
    if sys.stdout:
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except ValueError as error:
        print(error, file=sys.stderr)
        sys.exit(1)
