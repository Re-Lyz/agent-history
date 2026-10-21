"""Owned temporary sessions: preserve native history before retiring a launch copy.

Every publication and cleanup is recoverable under the shared collector lock.
Legacy jobs retire their owned temporary. Single-current jobs also retire
explicitly related old versions after verifying that their history is covered.
"""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import uuid

import agent_history as ah
import codex_import as ci
import conversation_store as store
import launch_jobs as jobs


def get(db, identity):
    row = db.execute('SELECT * FROM temporary_handoffs WHERE id=?', (identity,)).fetchone()
    return dict(row) if row else None


def version(db, vid):
    row = db.execute('SELECT * FROM conversation_versions WHERE id=?', (vid,)).fetchone()
    if not row:
        raise ValueError('接续版本记录缺失，临时副本保留。')
    return dict(row)


def update(db, identity, **fields):
    allowed = {'state', 'error', 'temporary_version', 'temporary_row', 'final_revision',
               'durable_version', 'durable_row', 'cleanup_path'}
    if not fields.keys() <= allowed:
        raise ValueError('Invalid handoff fields.')
    with db:
        db.execute('UPDATE temporary_handoffs SET ' + ','.join(name + '=?' for name in fields) + ' WHERE id=?',
                   [*fields.values(), identity])


def native_input(db, source, tool, cwd):
    projected = store.projection(db, source, tool, cwd)
    if projected:
        return projected
    value = store.revision(db, source['head'])
    records = json.loads(json.dumps(value['records']))
    if value['row']['tool'] == 'codex':
        records = [record for index, record in enumerate(records) if record.get('type') != 'session_meta' or index == 0]
        records[0]['payload'].pop('history_base', None)
        records[0]['payload'].pop('forked_from_id', None)
        records[0]['payload']['cwd'] = cwd
    path = ah.DATA / 'handoffs' / 'projections' / (store.digest([source['head'], 'native-input', cwd]) + '.jsonl')
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_suffix('.tmp')
        staged.write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records), encoding='utf-8')
        os.replace(staged, path)
    return dict(value['row'], key=json.loads(source['row_json'])['key'], path=str(path), cwd=cwd)


def publish(db, source, target, cwd, intent, on_publish):
    """Fresh identity; old import tombstones and user-owned imports stay untouched."""
    selected = native_input(db, source, 'claude' if target == 'claude' else 'codex', cwd)
    if target == 'claude':
        if selected['tool'] == 'claude':
            row = store.publish_claude(db, source, selected, cwd, intent, on_publish=on_publish)
        else:
            sid = str(uuid.uuid4())
            row = dict(selected, tool='claude', source='claude', key='claude:' + sid, id=sid,
                       path=str(ah.claude_project_dir(cwd) / (sid + '.jsonl')), kind='import')
            store.record_publication(db, intent, row)
            on_publish(row)
            ah.import_claude(db, selected, cwd=cwd, sid=sid)
    else:
        home = dict(ah.codex_homes())[target]
        def planned(result):
            row = dict(selected, tool='codex', source=target, key='codex:' + result['session'],
                       id=result['session'], path=result['path'], kind='import')
            store.record_publication(db, intent, row)
            on_publish(row)
        result = ci.import_session(db, selected, ah.DATA / 'handoffs' / 'projection-home'
                                   if selected['tool'] == 'codex' else None, target, home,
                                   prefer_native=False, explicit_handoff=True, fresh=True, on_publish=planned)
        if result['status'] == 'busy':
            raise ValueError('临时会话发布受写锁或来源变化阻止，请重试。')
        row = dict(selected, tool='codex', source=target, key='codex:' + result['session'],
                   id=result['session'], path=result['path'], kind='import')
    child = store.register_target(db, source, row, target, cwd)
    with db:
        db.execute("UPDATE handoff_intents SET state='registered' WHERE identity=?", (intent,))
    return child


def prepare(db, source, target, cwd, *, policy='single'):
    identity = uuid.uuid4().hex
    owner = db.execute('SELECT * FROM temporary_handoffs WHERE temporary_version=?', (source['id'],)).fetchone()
    origin = version(db, owner['origin_version']) if owner and policy == 'legacy' else source
    store.observe(db, origin)
    origin = version(db, origin['id'])
    with db:
        db.execute('INSERT INTO temporary_handoffs(id,source_version,source_revision,origin_version,origin_revision,target,cwd,state,policy) '
                   'VALUES(?,?,?,?,?,?,?,?,?)', (identity, source['id'], source['head'], origin['id'], origin['head'], target, cwd, 'preparing', policy))
    jobs.create(json.loads(source['row_json']), dict(session='pending', cwd=cwd, kind='handoff'), target, identity=identity)
    jobs.update(identity, handoff_state='preparing')
    intent = store.begin_intent(db, source, target, cwd, request_id=identity)
    try:
        child = publish(db, source, target, cwd, intent,
                        lambda row: update(db, identity, temporary_row=json.dumps(row, ensure_ascii=False)))
    except (OSError, ValueError, sqlite3.Error) as error:
        update(db, identity, state='failed', error=str(error))
        jobs.update(identity, state='failed', error=str(error), sync_state='failed')
        raise
    update(db, identity, temporary_version=child['id'], state='prepared')
    return identity, json.loads(child['row_json'])


def recover_version(db, item, field):
    vid = item[field + '_version']
    if vid:
        return version(db, vid)
    planned = item[field + '_row']
    if not planned:
        return None
    row = json.loads(planned)
    if not Path(row['path']).is_file():
        return None
    if row['tool'] == 'codex':
        home = dict(ah.codex_homes())[row['source']]
        native = ci.source_row(home, row['id'])
        if not native:
            # A process may have died between the immutable file and native SQL publication.
            ci.register_native(home, row['id'], Path(row['path']), row, ci.first_meta(row['path']), ci.config(home))
        elif not ci.same_path(native['rollout_path'], row['path']):
            raise ValueError('已发布会话指向其他原生文件，保留临时副本。')
    parent = version(db, item['source_version'] if field == 'temporary' else item['temporary_version'])
    parent['head'] = item['source_revision'] if field == 'temporary' else item['final_revision']
    child = store.register_target(db, parent, row, row['source'], row['cwd'])
    update(db, item['id'], **{field + '_version': child['id']})
    return child


def session_lock(row):
    if row['tool'] == 'codex':
        return ci.writer_lock(dict(ah.codex_homes())[row['source']], row['id'])
    return contextlib.nullcontext(True)


def complete_file(path):
    with Path(path).open('rb') as stream:
        if not stream.seek(0, 2):
            raise ValueError('临时转录为空，保留副本等待检查。')
        stream.seek(-1, 2)
        if stream.read(1) != b'\n':
            raise ValueError('临时转录包含未完成记录，补全后可重试同步。')
    try:
        with Path(path).open('r', encoding='utf-8-sig') as stream:
            for line in stream:
                if line.strip() and not isinstance(json.loads(line), dict):
                    raise ValueError('Invalid transcript record.')
    except (UnicodeError, ValueError) as error:
        raise ValueError('临时转录包含损坏记录，保留副本等待检查。') from error


def claude_running(row):
    """Only return a boolean, never expose command lines or environment values."""
    if row['tool'] != 'claude':
        return False
    if os.name != 'nt':
        return claude_running_posix(row)
    # Native Claude does not hold Codex's per-thread lock. Check idle resumed CLI processes too.
    sid = row['id'].replace("'", "''")
    script = "$s='" + sid + "'; $p=Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { " \
             "$_.ProcessId -ne $PID -and $_.Name -match '^(node|claude)\\.exe$' -and " \
             "$_.CommandLine -and $_.CommandLine.Contains($s) -and $_.CommandLine -match 'claude' }; " \
             "if ($p) { 'busy' } else { 'free' }"
    try:
        result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-Command', script],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('无法检查 Claude 会话进程，文件保留。') from error
    if result.returncode:
        raise ValueError('无法检查 Claude 会话进程，临时副本保留。')
    return result.stdout.strip() == 'busy'


def claude_running_posix(row, proc=Path('/proc')):
    """Check current-user native CLI arguments without exposing their contents."""
    if not proc.is_dir():
        raise ValueError('无法检查 Claude 会话进程，文件保留。')
    uid = os.getuid() if hasattr(os, 'getuid') else None
    for directory in proc.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if uid is not None and directory.stat().st_uid != uid:
                continue
            args = (directory / 'cmdline').read_bytes().decode('utf-8', errors='replace').split('\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            raise ValueError('无法检查 Claude 会话进程，文件保留。') from error
        program = Path(args[0]).name if args else ''
        if program in ('claude', 'node', 'nodejs') and any('claude' in arg for arg in args) and \
                any(row['id'] in arg for arg in args):
            return True
    return False


def verify_native(db, child, final):
    """Check the stored native content and Codex resume without a model request."""
    from launch_backend import file_messages
    row = json.loads(child['row_json'])
    complete_file(row['path'])
    records, events = store.read_native(row)
    if not records or store.digest(records) != store.revision(db, child['head'])['native_hash']:
        raise ValueError('承接版本内容发生变化，保留临时副本。')
    target_ids = [event['id'] for event in store.revision(db, child['head'])['events']]
    if target_ids != [event['id'] for event in final['events']]:
        raise ValueError('承接版本未包含完整接续修订，保留临时副本。')
    if row['tool'] == 'codex':
        from native_codex import CodexRPC
        home = dict(ah.codex_homes())[row['source']]
        native = ci.source_row(home, row['id'])
        if not native or not ci.same_path(native['rollout_path'], row['path']):
            raise ValueError('承接版本的原生恢复位置不一致。')
        with CodexRPC(home) as rpc:
            result = rpc.call('thread/resume', dict(threadId=row['id']))
            if result['thread']['id'] != row['id']:
                raise ValueError('原平台未恢复预期承接版本。')
            page = rpc.call('thread/turns/list', dict(threadId=row['id'], limit=100, itemsView='full'))
            text = json.dumps(page['data'], ensure_ascii=False)
            cursor = page.get('nextCursor')
            while cursor:
                page = rpc.call('thread/turns/list', dict(threadId=row['id'], limit=100, itemsView='full', cursor=cursor))
                text += json.dumps(page['data'], ensure_ascii=False)
                cursor = page.get('nextCursor')
            for message in file_messages(row):
                if message['role'] in ('user', 'assistant', 'agent') and message['text']:
                    if json.dumps(message['text'], ensure_ascii=False)[1:-1] not in text:
                        raise ValueError('原平台恢复后缺少承接文字，临时副本保留。')
    else:
        if not events or any(record.get('sessionId') not in (None, row['id']) for record in records):
            raise ValueError('Claude 承接版本内容或原生 ID 校验失败。')
    # The read/resume operation may append native metadata; keep the new stable hash.
    store.observe(db, child)
    return version(db, child['id'])


def retire(db, item, temporary, durable):
    """Quarantine before native removal; resume an interrupted cleanup idempotently."""
    import trash
    row = json.loads(temporary['row_json'])
    path = Path(row['path'])
    home = dict(ah.codex_homes())[row['source']] if row['tool'] == 'codex' else path.parent.parent.parent
    roots = [home / 'sessions', home / 'archived_sessions'] if row['tool'] == 'codex' else [home / 'projects']
    trash.within(path, roots)
    folder = ah.DATA / 'handoffs' / 'cleanup' / item['id']
    saved = folder / path.name
    folder.mkdir(parents=True, exist_ok=True)
    update(db, item['id'], state='cleaning', cleanup_path=str(saved))
    with session_lock(row) as free:
        if not free or claude_running(row):
            raise ValueError('临时目标仍在使用，已同步但暂不回收。')
        if row['tool'] == 'codex' and trash.fork_index(db).get((row['source'], row['id'])):
            raise ValueError('临时会话被原生分支引用，保留副本。')
        if path.exists():
            complete_file(path)
            records, _ = store.read_native(row)
            if store.digest(records) != store.revision(db, temporary['head'])['native_hash']:
                raise ValueError('临时副本在清理前发生变化，请重新同步。')
            os.replace(path, saved)
        elif not saved.exists():
            raise ValueError('临时文件缺失且没有清理记录，不能确认已保存其全部内容。')
        complete_file(saved)
        records, _ = store.read_native(dict(row, path=str(saved)))
        if store.digest(records) != store.revision(db, temporary['head'])['native_hash']:
            raise ValueError('待清理文件与已保存修订不一致，保留文件等待检查。')
        if row['tool'] == 'codex':
            try:
                trash.native_remove(home, row['id'])
            except BaseException:
                if saved.exists() and not path.exists():
                    os.replace(saved, path)
                raise
            if ci.source_row(home, row['id']):
                raise ValueError('临时目标仍在原生列表中，等待重试清理。')
            caches = [('session_index.jsonl', 'id'), ('history.jsonl', 'session_id')]
        else:
            caches = [('history.jsonl', 'sessionId')]
        for name, field in caches:
            if trash.remove_cache_lines(home / name, field, row['id']) is None:
                raise ValueError('原生历史缓存正在使用，等待重试清理。')
        ah.forget_file(db, str(path))
        with db:
            db.execute("UPDATE conversation_versions SET present=0,state='retired' WHERE id=?", (temporary['id'],))
        store.activate(db, durable)
        update(db, item['id'], state='cleaned', error='')
    saved.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        folder.rmdir()


def finish(db, identity, *, manual=False, allow_branch=False):
    """Caller holds collector lock; no cleanup until durable native verification succeeds."""
    item = get(db, identity)
    if not item:
        return None
    if item.get('policy') == 'single':
        import single_conversation
        return single_conversation.finish(db, identity, manual=manual)
    report = jobs.read(identity)
    if item['state'] == 'cleaned':
        if item['cleanup_path']:
            Path(item['cleanup_path']).unlink(missing_ok=True)
        jobs.update(identity, handoff_state='cleaned', sync_state='collected')
        return item
    if report['state'] in ('running', 'prepared') or (report['state'] == 'interrupted' or report.get('exit_code', 1) != 0) and not manual:
        raise ValueError('终端尚未正常结束，临时副本保留；结束后可重试同步。')
    if report.get('pid') and jobs.process_matches(report) and report['pid'] != os.getpid():
        raise ValueError('终端报告进程仍在运行，暂不回收临时会话。')
    from setup_backend import load_sources, source_enabled
    scope = load_sources()
    origin = version(db, item['origin_version'])
    origin_row = json.loads(origin['row_json'])
    temp_row = json.loads(item['temporary_row']) if item['temporary_row'] else \
        json.loads(version(db, item['temporary_version'])['row_json'])
    if not all(source_enabled(row, scope) for row in (origin_row, temp_row)):
        raise ValueError('接续来源已停用，重新接入后可重试同步。')
    temporary = recover_version(db, item, 'temporary')
    if not temporary:
        raise ValueError('临时副本尚未完整发布，保留记录以便检查。')
    item = get(db, identity)
    temp_row = json.loads(temporary['row_json'])
    # A missing original is not authority to silently recreate a deleted conversation.
    if not Path(origin_row['path']).is_file():
        raise ValueError('原平台会话已删除，临时副本保留；请选择要保留的分支。')
    with session_lock(origin_row) as free:
        if not free or claude_running(origin_row):
            raise ValueError('原平台会话正在使用，临时副本保留，关闭后可重试同步。')
        store.observe(db, origin)
        origin = version(db, origin['id'])
        previous_events = store.revision(db, item['origin_revision'])['events']
        current_events = store.revision(db, origin['head'])['events']
        if current_events != previous_events and not allow_branch:
            raise ValueError('原平台也有新增内容；保留两侧分支，请选择“保留分支并完成同步”。')
        if item['state'] != 'cleaning' or Path(temp_row['path']).exists():
            with session_lock(temp_row) as free:
                if not free or claude_running(temp_row):
                    raise ValueError('临时目标仍在使用，结束后可重试同步。')
                if not Path(temp_row['path']).is_file():
                    raise ValueError('临时副本被删除，不能自动清理或覆盖删除记录。')
                complete_file(temp_row['path'])
                store.observe(db, temporary)
            temporary = version(db, temporary['id'])
            if item['final_revision'] and temporary['head'] != item['final_revision'] and item['durable_row']:
                # A continuation after a failed sync is a new publication, not an overwrite.
                update(db, identity, durable_version=None, durable_row=None)
                item = get(db, identity)
            update(db, identity, final_revision=temporary['head'], state='preserving', error='')
        item = get(db, identity)
        final = store.revision(db, item['final_revision'])
        durable = recover_version(db, item, 'durable')
        if not durable:
            target = origin_row['source']
            intent = store.begin_intent(db, temporary, target, origin_row['cwd'], request_id=identity + '-return-' + final['id'])
            durable = publish(db, temporary, target, origin_row['cwd'], intent,
                              lambda row: update(db, identity, durable_row=json.dumps(row, ensure_ascii=False)))
            update(db, identity, durable_version=durable['id'])
        durable = verify_native(db, durable, final)
        jobs.update(identity, handoff_state='preserved', durable_session=json.loads(durable['row_json'])['id'],
                    durable_source=origin_row['source'])
        item = get(db, identity)
        # Other launches depending on this physical temporary must finish first.
        for other in db.execute("SELECT id FROM temporary_handoffs WHERE source_version=? AND id<>? AND state<>'cleaned'",
                                (temporary['id'], identity)):
            try:
                pending = jobs.read(other['id'])
            except (OSError, ValueError):
                raise ValueError('其他接续任务仍引用临时会话，保留副本。')
            if pending['state'] in ('prepared', 'running', 'interrupted'):
                raise ValueError('其他接续任务仍引用临时会话，完成后可重试清理。')
        retire(db, item, temporary, durable)
    ah.collect(db)
    jobs.update(identity, handoff_state='cleaned', sync_state='collected', sync_error='')
    return get(db, identity)


def retry_pending(db, *, manual=False, allow_branch=False, identity=None):
    result = dict(completed=0, pending=0)
    query = 'SELECT id FROM temporary_handoffs WHERE id=?' if identity else \
        "SELECT id FROM temporary_handoffs WHERE state<>'cleaned'"
    for (key,) in db.execute(query, (identity,) if identity else ()).fetchall():
        try:
            if manual:
                jobs.reconcile(identity=key)
            report = jobs.read(key)
            if report['state'] in ('prepared', 'running'):
                result['pending'] += 1
                continue
            finish(db, key, manual=manual, allow_branch=allow_branch)
            result['completed'] += 1
        except (OSError, ValueError, sqlite3.Error, RuntimeError, TimeoutError) as error:
            update(db, key, error=str(error))
            with contextlib.suppress(OSError, ValueError):
                jobs.update(key, handoff_state='pending', sync_state='failed', sync_error=str(error))
            result['pending'] += 1
    # Same-connection resumes also reserve a logical conversation. Recover their
    # final snapshots / interrupted cleanup through the same UI retry entry.
    import single_conversation
    native = db.execute('SELECT r.job FROM conversation_runs r LEFT JOIN temporary_handoffs h ON h.id=r.job '
                        'WHERE h.id IS NULL' + (' AND r.job=?' if identity else ''), (identity,) if identity else ()).fetchall()
    for (key,) in native:
        try:
            if manual:
                jobs.reconcile(identity=key)
            if jobs.read(key)['state'] in ('prepared', 'running'):
                result['pending'] += 1
                continue
            single_conversation.finish(db, key, manual=manual)
            result['completed'] += 1
        except (OSError, ValueError, sqlite3.Error, RuntimeError, TimeoutError) as error:
            jobs.update(key, sync_state='failed', sync_error=str(error))
            result['pending'] += 1
    return result
