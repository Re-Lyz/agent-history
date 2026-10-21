"""One managed writer and one current native transcript per logical conversation.

Caller holds the collector lock. Immutable revision snapshots remain the recovery
record; retirement journals survive a process exit between file and SQL changes.
"""
import contextlib
import json
import os
from pathlib import Path

import agent_history as ah
import codex_import as ci
import conversation_store as store
import launch_jobs as jobs


def versions(db, conversation):
    return [dict(row) for row in db.execute(
        'SELECT * FROM conversation_versions WHERE conversation=?', (conversation,))]


@contextlib.contextmanager
def idle_native_versions(db, conversation):
    """Hold each native session once, even if several versions alias its lock."""
    import handoff_lifecycle as lifecycle
    homes, seen = dict(ah.codex_homes()), set()
    with contextlib.ExitStack() as locks:
        for item in versions(db, conversation):
            row = json.loads(item['row_json'])
            if not Path(row['path']).is_file():
                continue
            if row['tool'] == 'codex':
                if row['source'] not in homes:
                    raise ValueError('原生来源已不可用，无法核对会话状态。')
                key = (row['tool'], str(homes[row['source']].resolve()), row['id'])
            else:
                key = (row['tool'], str(Path(row['path']).resolve()))
            if key in seen:
                continue
            seen.add(key)
            if not locks.enter_context(lifecycle.session_lock(row)) or lifecycle.claude_running(row):
                yield False
                return
        yield True


def release(db, identity):
    with db:
        db.execute('DELETE FROM conversation_runs WHERE job=?', (identity,))


def reservation_reason(db, source, identity=None, *, wait=True):
    """Read-only explanation shared by launch validation and the UI preview."""
    import handoff_lifecycle as lifecycle
    lease = db.execute('SELECT * FROM conversation_runs WHERE conversation=?', (source['conversation'],)).fetchone()
    if lease and lease['job'] != identity:
        try:
            report = jobs.read(lease['job'], wait=wait)
        except TimeoutError:
            return '启动状态正在更新，请稍后刷新。'
        except (OSError, ValueError):
            return '这条对话有未完成的启动记录，请先检查启动记录并恢复同步。'
        owned = lifecycle.get(db, lease['job'])
        if report['state'] == 'cancelled' and report.get('sync_state') == 'not-needed' or \
                report.get('session_detached') and report.get('sync_state') == 'collected' or \
                owned and owned['state'] == 'cleaned' or not owned and (
                report['state'] == 'cancelled' or report['state'] in ('exited', 'failed', 'interrupted')
                and report.get('sync_state') in ('collected', 'not-needed')):
            return ''
        if report['state'] == 'prepared' and jobs.process_matches(report, 'preparation_pid'):
            try:
                with idle_native_versions(db, source['conversation']) as free:
                    if not free:
                        return '原生会话正在 Agent 中使用，但启动记录尚未确认接管；请在原终端继续，切勿重复启动。'
            except (OSError, ValueError):
                return '无法核对原生会话是否正在使用，已保留启动预约；请检查原终端。'
            return '这条对话已准备启动，等待终端接管；若终端未启动，可取消未接管启动后重试。'
        if jobs.process_matches(report):
            return '这条对话正在使用，请先正常退出对应的 Agent，再重试同步。'
        return '这条对话的启动进程已结束，等待核验已保存内容；请点击“重试同步／刷新续聊”。'
    return ''


def cancel_prepared(db, identity):
    """Caller holds collect.lock. Keep every native file and immutable revision."""
    import handoff_lifecycle as lifecycle
    report = jobs.read(identity)
    if report['state'] != 'prepared' or report.get('attempts', 0) != 0:
        raise ValueError('终端已接管或任务已结束，不能取消未接管启动。')
    lease = db.execute('SELECT * FROM conversation_runs WHERE job=?', (identity,)).fetchone()
    if not lease:
        raise ValueError('此启动没有对应的会话预约，请检查启动记录。')
    with idle_native_versions(db, lease['conversation']) as free:
        if not free:
            raise ValueError('原生会话仍在 Agent 中使用，未取消预约。请先确认原终端已切走或退出。')
        jobs.cancel_prepared(identity)
        if lifecycle.get(db, identity):
            lifecycle.update(db, identity, state='cancelled', error='')
        release(db, identity)
    return dict(cancelled=True, files_preserved=True)


def detach_session(db, identity):
    """Explicit native /resume recovery; retain all native files and revisions.

    Caller holds collect.lock. Normal exit may finalize cleanup later, provided
    no newer owner exists. A live terminal is never reported as exited.
    """
    import handoff_lifecycle as lifecycle
    report = jobs.read(identity)
    if report['state'] != 'running':
        raise ValueError('此任务不是运行中状态；未接管启动请使用取消入口，已退出任务请重试同步。')
    if not report.get('native_session_observed'):
        raise ValueError('此任务尚未记录原生会话接管，无法区分启动中与已切出；请在原终端正常退出后同步。')
    lease = db.execute('SELECT * FROM conversation_runs WHERE job=?', (identity,)).fetchone()
    if not lease:
        if report.get('session_detached'):
            return dict(synced=True, files_preserved=True)
        raise ValueError('此任务没有对应的会话预约，请检查启动记录。')
    owned = lifecycle.get(db, identity)
    current = lifecycle.version(db, owned['temporary_version'] if owned else report['version_id'])
    row = json.loads(current['row_json'])
    with idle_native_versions(db, lease['conversation']) as free:
        if not free:
            raise ValueError('原对话仍持有原生写锁或仍在 Agent 中运行，不能认定已切出。')
        if not Path(row['path']).is_file():
            raise ValueError('原会话文件已不存在，保留记录等待检查。')
        lifecycle.complete_file(row['path'])
        store.observe(db, current)
        current = lifecycle.version(db, current['id'])
        latest(db, current)
        jobs.detach_session(identity)
        store.activate(db, current)
        if owned:
            lifecycle.update(db, identity, state='detached', error='')
        release(db, identity)
    return dict(synced=True, files_preserved=True)


def available(db, source, identity=None):
    """Include hidden physical versions and prepared launches, not just UI tips."""
    import handoff_lifecycle as lifecycle
    store.ensure(db)
    reason = reservation_reason(db, source, identity)
    if reason:
        raise ValueError(reason)
    lease = db.execute('SELECT * FROM conversation_runs WHERE conversation=?', (source['conversation'],)).fetchone()
    if lease and lease['job'] != identity:
        release(db, lease['job'])
    for item in versions(db, source['conversation']):
        row = json.loads(item['row_json'])
        if not Path(row['path']).is_file():
            continue
        with lifecycle.session_lock(row) as free:
            if not free or lifecycle.claude_running(row):
                raise ValueError('这条对话正在 Agent 中使用，请先退出当前 Agent 再切换。')


def latest(db, source):
    heads = store.tips(db, source['conversation'])
    if len(heads) > 1:
        raise ValueError('这条对话有多个续聊分支，请先合并分支，再继续单主会话。')
    if heads:
        ids = lambda item: json.loads(item['event_ids'] or '[]')
        if ids(source) != ids(heads[0]):
            raise ValueError('所选版本不是最新主对话，请刷新并选择最新版本。')


def reserve(db, source, identity):
    available(db, source, identity)
    with db:
        db.execute('INSERT OR REPLACE INTO conversation_runs VALUES (?,?,?)',
                   (source['conversation'], identity, source['id']))
    jobs.update(identity, single_head=True, conversation=source['conversation'], version_id=source['id'])


def claim(db, identity):
    """Recheck the reservation in the independent terminal process before exec."""
    report = jobs.read(identity)
    store.ensure(db)
    reservation = db.execute('SELECT * FROM conversation_runs WHERE job=?', (identity,)).fetchone()
    if reservation and not report.get('single_head'):
        # SQL reservation commits before the separate atomic job-report write.
        report = jobs.update(identity, single_head=True, conversation=reservation['conversation'],
                             version_id=reservation['version'])
    if not report.get('single_head'):
        return  # Already-running legacy jobs retain their original cleanup contract.
    import handoff_lifecycle as lifecycle
    owned = lifecycle.get(db, identity)
    if owned and owned['state'] == 'cleaned':
        raise ValueError('接续任务已完成，请从最新主会话重新启动。')
    source = db.execute('SELECT * FROM conversation_versions WHERE id=?', (report['version_id'],)).fetchone()
    if not source or not Path(json.loads(source['row_json'])['path']).is_file():
        raise ValueError('启动版本已删除或已被接替，请从最新主对话重新启动。')
    source = dict(source)
    lease = db.execute('SELECT * FROM conversation_runs WHERE conversation=?', (source['conversation'],)).fetchone()
    if lease and lease['job'] != identity:
        raise ValueError('这条对话已由另一个启动任务占用。')
    if report['state'] == 'running' and jobs.process_matches(report):
        raise ValueError('这个启动任务已经在运行。')
    store.observe(db, source)
    source = dict(db.execute('SELECT * FROM conversation_versions WHERE id=?', (source['id'],)).fetchone())
    latest(db, source)
    reserve(db, source, identity)


def candidates(db, current):
    """Only retire exact canonical prefixes, never infer coverage from timestamps."""
    ids = [event['id'] for event in store.revision(db, current['head'])['events']]
    result = []
    from setup_backend import load_sources, source_enabled
    scope = load_sources()
    for item in versions(db, current['conversation']):
        if item['id'] == current['id'] or item['state'] == 'merge-pending':
            continue
        row = json.loads(item['row_json'])
        if not Path(row['path']).is_file():
            continue
        if not source_enabled(row, scope):
            raise ValueError('旧会话来源已停用，文件保留；重新接入后可重试同步。')
        store.observe(db, item)
        item = dict(db.execute('SELECT * FROM conversation_versions WHERE id=?', (item['id'],)).fetchone())
        other = [event['id'] for event in store.revision(db, item['head'])['events']]
        if not covered(db, item['id'], other, ids):
            raise ValueError('其他版本有未合入的新增内容，已保留两侧文件；请先合并分支。')
        result.append(item)
    return result


def covered(db, vid, events, final, seen=()):
    """Follow explicit, signature-checked merges, including prior merged spines."""
    if store.prefix(events, final):
        return True
    if vid in seen:
        return False
    for merge in db.execute('SELECT * FROM conversation_merges WHERE source_version=?', (vid,)):
        if store.prefix(events, json.loads(merge['source_events'])) and covered(
                db, merge['target_version'], json.loads(merge['target_events']), final, (*seen, vid)):
            return True
    return False


def retire_one(db, identity, item, current, forks, pending_paths):
    import handoff_lifecycle as lifecycle
    import trash
    row = json.loads(item['row_json'])
    path = Path(row['path'])
    homes = dict(ah.codex_homes())
    if row['tool'] == 'codex':
        home = homes[row['source']]
        trash.within(path, [home / 'sessions', home / 'archived_sessions'])
    else:
        home = next((home for home in ah.claude_homes() if path.resolve().is_relative_to((home / 'projects').resolve())), None)
        if home is None:
            raise ValueError('旧会话不在已注册 Claude 历史目录中。')
    journal = db.execute('SELECT * FROM conversation_retirements WHERE job=? AND version=?',
                         (identity, item['id'])).fetchone()
    if journal and journal['state'] == 'done':
        return
    saved = ah.DATA / 'handoffs' / 'cleanup' / identity / (item['id'] + '.jsonl')
    snapshot = store.revision(db, journal['snapshot'] if journal else item['head'])
    with lifecycle.session_lock(row) as free:
        if not free or lifecycle.claude_running(row):
            raise ValueError('旧会话仍在使用，主对话已保存，等待安全清理。')
        if any(child not in pending_paths for child in forks.get((row['source'], row['id']), [])):
            raise ValueError('旧会话被其他原生分支引用，暂不清理。')
        if path.exists():
            lifecycle.complete_file(path)
            records, _ = store.read_native(row)
            if journal and not saved.exists() and snapshot['id'] != item['head']:
                latest = store.revision(db, item['head'])
                if covered(db, item['id'], [event['id'] for event in latest['events']],
                           [event['id'] for event in store.revision(db, current['head'])['events']]):
                    snapshot = latest
                    with db:
                        db.execute('UPDATE conversation_retirements SET snapshot=? WHERE job=? AND version=?',
                                   (item['head'], identity, item['id']))
            if store.digest(records) != snapshot['native_hash']:
                raise ValueError('旧会话在清理前发生变化，已保留文件，请重新同步。')
        elif not journal or not saved.is_file():
            raise ValueError('旧会话清理文件缺失，停止回收等待检查。')
        saved.parent.mkdir(parents=True, exist_ok=True)
        if row['tool'] == 'codex':
            native = ci.source_row(home, row['id'])
            if native and not ci.same_path(native['rollout_path'], path):
                raise ValueError('旧会话的原生索引指向其他文件，停止清理。')
        if not journal:
            with db:
                db.execute('INSERT INTO conversation_retirements VALUES (?,?,?,?,?)',
                           (identity, item['id'], item['head'], str(saved), 'planned'))
        if path.exists():
            if saved.exists():
                raise ValueError('旧会话原位置与隔离位置同时存在，停止清理。')
            path.replace(saved)
        lifecycle.complete_file(saved)
        records, _ = store.read_native(dict(row, path=str(saved)))
        if store.digest(records) != snapshot['native_hash']:
            raise ValueError('隔离文件与备份修订不一致，停止清理。')
        if row['tool'] == 'codex':
            try:
                trash.native_remove(home, row['id'])
            except BaseException:
                if saved.exists() and not path.exists():
                    saved.replace(path)
                raise
            if ci.source_row(home, row['id']):
                raise ValueError('旧会话仍在原生索引中，等待重试清理。')
            caches = [('session_index.jsonl', 'id'), ('history.jsonl', 'session_id')]
        else:
            caches = [('history.jsonl', 'sessionId')]
        for name, field in caches:
            if trash.remove_cache_lines(home / name, field, row['id']) is None:
                raise ValueError('旧会话历史缓存正在使用，等待重试清理。')
        ah.forget_file(db, str(path))
        with db:
            db.execute("UPDATE conversation_versions SET present=0,state='retired' WHERE id=?", (item['id'],))
            db.execute("UPDATE conversation_retirements SET state='done' WHERE job=? AND version=?",
                       (identity, item['id']))
        saved.unlink(missing_ok=True)


def compact(db, identity, current):
    import handoff_lifecycle as lifecycle
    import trash
    selected = candidates(db, current)
    # A crash after moving a file must still finish native/cache retirement.
    for journal in db.execute("SELECT * FROM conversation_retirements WHERE job=? AND state<>'done'", (identity,)):
        snapshot = store.revision(db, journal['snapshot'])
        if not covered(db, journal['version'], [event['id'] for event in snapshot['events']],
                       [event['id'] for event in store.revision(db, current['head'])['events']]):
            raise ValueError('主会话重写后不再覆盖隔离中的旧文件，停止清理。')
        if journal['version'] not in {item['id'] for item in selected}:
            selected.append(lifecycle.version(db, journal['version']))
    paths = {str(Path(json.loads(item['row_json'])['path']).resolve()) for item in selected}
    forks = trash.fork_index(db)
    rows = {item['id']: json.loads(item['row_json']) for item in selected}
    current_row = json.loads(current['row_json'])
    if any(all(row[key] == current_row[key] for key in ('tool', 'source', 'id')) for row in rows.values()):
        raise ValueError('旧版本与主会话共用原生 ID，停止清理，避免破坏恢复索引。')
    def depth(item, seen=()):
        if item['id'] in seen:
            raise ValueError('原生分支引用形成循环，停止清理。')
        children = forks.get((rows[item['id']]['source'], rows[item['id']]['id']), [])
        return 1 + max((depth(other, (*seen, item['id'])) for other in selected
                       if str(Path(rows[other['id']]['path']).resolve()) in children), default=0)
    for item in sorted(selected, key=depth):
        retire_one(db, identity, item, current, forks, paths)
    # Recover deletion of an already-committed quarantine file after a crash.
    for journal in db.execute("SELECT * FROM conversation_retirements WHERE job=? AND state='done'", (identity,)):
        saved = Path(journal['saved'])
        expected = ah.DATA / 'handoffs' / 'cleanup' / identity / (journal['version'] + '.jsonl')
        if saved.resolve() != expected.resolve():
            raise ValueError('清理日志的文件位置不一致。')
        saved.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        (ah.DATA / 'handoffs' / 'cleanup' / identity).rmdir()
    store.activate(db, current)


def finish(db, identity, *, manual=False):
    """Promote the used native file; never publish a redundant return copy."""
    import handoff_lifecycle as lifecycle
    item = lifecycle.get(db, identity)
    report = jobs.read(identity)
    if report['state'] == 'cancelled' and report.get('sync_state') == 'not-needed':
        raise ValueError('启动已取消，会话文件保留；不会自动清理这些文件。')
    if not item and not report.get('version_id'):
        reservation = db.execute('SELECT * FROM conversation_runs WHERE job=?', (identity,)).fetchone()
        if not reservation:
            raise ValueError('启动任务的主会话登记缺失，请检查启动记录。')
        report = jobs.update(identity, single_head=True, conversation=reservation['conversation'],
                             version_id=reservation['version'])
    if item and item['state'] == 'cleaned':
        jobs.update(identity, handoff_state='cleaned', sync_state='collected', sync_error='')
        release(db, identity)
        return item
    abnormal = report['state'] != 'exited' or report.get('exit_code') != 0
    if report['state'] in ('prepared', 'running') or item and abnormal and not manual:
        raise ValueError('Agent 尚未正常结束，文件保留；退出后可重试同步。')
    if report.get('pid') != os.getpid() and jobs.process_matches(report):
        raise ValueError('Agent 进程仍在运行，暂不清理。')
    current = lifecycle.recover_version(db, item, 'temporary') if item else lifecycle.version(db, report['version_id'])
    if not current or not Path(json.loads(current['row_json'])['path']).is_file():
        raise ValueError('最新会话已删除，停止同步；不会恢复已删除的文件。')
    lease = db.execute('SELECT job FROM conversation_runs WHERE conversation=?', (current['conversation'],)).fetchone()
    if lease and lease['job'] != identity:
        raise ValueError('原对话已由另一启动任务接管，旧任务保留文件，停止清理。')
    row = json.loads(current['row_json'])
    from setup_backend import load_sources, source_enabled
    if not source_enabled(row, load_sources()):
        raise ValueError('当前历史来源已停用，请重新接入后同步。')
    with lifecycle.session_lock(row) as free:
        if not free or lifecycle.claude_running(row):
            raise ValueError('当前 Agent 会话仍在使用，请先退出后同步。')
        lifecycle.complete_file(row['path'])
        store.observe(db, current)
        current = lifecycle.version(db, current['id'])
    # Native readback needs its own writer lock, so verify before acquiring ours.
    selected = candidates(db, current)
    if not item and abnormal and not manual:
        # A same-connection CLI failure does not need a migration. Collect the
        # already-native file; defer any retirement until explicit recovery.
        if selected:
            raise ValueError('Agent 非正常结束，旧文件保留；可手动重试同步。')
        store.activate(db, current)
        jobs.update(identity, sync_state='collected', sync_error='')
        release(db, identity)
        return None
    if item or selected:
        current = lifecycle.verify_native(db, current, store.revision(db, current['head']))
    if item:
        lifecycle.update(db, identity, state='preserving', final_revision=current['head'],
                         durable_version=current['id'], durable_row=current['row_json'])
    jobs.update(identity, handoff_state='preserved', durable_session=row['id'], durable_source=row['source'])
    with lifecycle.session_lock(row) as free:
        if not free or lifecycle.claude_running(row):
            raise ValueError('主会话重新被打开，已保存内容，等待安全清理。')
        records, _ = store.read_native(row)
        if store.digest(records) != store.revision(db, current['head'])['native_hash']:
            raise ValueError('主会话在验证后发生变化，暂不清理。')
        compact(db, identity, current)
        if item:
            lifecycle.update(db, identity, state='cleaned', error='')
        jobs.update(identity, handoff_state='cleaned', sync_state='collected', sync_error='')
        release(db, identity)
    ah.collect(db)
    return lifecycle.get(db, identity) if item else None
