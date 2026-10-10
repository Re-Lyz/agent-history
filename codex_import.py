"""Independent native Codex snapshots, indexed for the built-in resume picker.

The native SQLite schema is internal. Copy columns supported by the installed
version; let Codex rebuild its paginated history projection from the rollout.
No authentication files are read or copied, and no model turns are started.
"""
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import tomllib
import uuid

from native_codex import CodexRPC

SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_imports(
 dst_key TEXT PRIMARY KEY, target TEXT NOT NULL, src_key TEXT NOT NULL,
 dst_path TEXT NOT NULL, created TEXT NOT NULL, src_stamp TEXT NOT NULL,
 dst_hash TEXT NOT NULL, dst_size INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS codex_imports_source ON codex_imports(target,src_key,created);
"""


def quoted(value):
    return '"' + value.replace('"', '""') + '"'


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def state_path(home):
    files = list(Path(home).glob('state_*.sqlite'))
    if not files:
        raise ValueError(f'No native Codex state database in {home}; start this connection once first.')
    return max(files, key=lambda p: int(p.stem.split('_')[-1]))


def config(home):
    result = {}
    for name in ('config.toml', 'profile.toml'):
        path = Path(home) / name
        if path.exists():
            result.update(tomllib.loads(path.read_text(encoding='utf-8-sig')))
    if result.get('sqlite_home'):
        raise ValueError('Native import currently requires SQLite state under CODEX_HOME (no sqlite_home override).')
    return result


def source_row(home, sid):
    try:
        with contextlib.closing(readonly(state_path(home))) as db:
            row = db.execute('SELECT * FROM threads WHERE id=?', (sid,)).fetchone()
            return dict(row) if row else {}
    except (ValueError, sqlite3.Error):
        return {}


def records(path, byte_limit=None, ordinal_limit=None):
    """Read a stable prefix of complete JSONL records, without loading the file."""
    with Path(path).open('rb') as stream:
        size = min(os.fstat(stream.fileno()).st_size,
                   byte_limit if byte_limit is not None else 2**63)
        ordinal = 0
        while stream.tell() < size and (ordinal_limit is None or ordinal < ordinal_limit):
            line = stream.readline(size - stream.tell())
            if not line.endswith(b'\n'):
                break
            # Native import must not silently discard a corrupt middle record.
            yield json.loads(line)
            ordinal += 1


def first_meta(path):
    record = next(records(path), None)
    if not record or record.get('type') != 'session_meta':
        raise ValueError(f'Missing native session metadata: {path}')
    return record['payload']


def find_parent(home, sid):
    row = source_row(home, sid)
    if row and Path(row['rollout_path']).is_file():
        return Path(row['rollout_path'])
    matches = [p for folder in ('sessions', 'archived_sessions')
               for p in (Path(home) / folder).rglob(f'*{sid}.jsonl')]
    if len(matches) != 1:
        raise ValueError(f'Cannot materialize native fork: missing/ambiguous ancestor {sid}')
    return matches[0]


def ancestry(home, path, seen=()):
    meta = first_meta(path)
    sid = meta.get('id') or meta.get('session_id')
    if sid in seen or len(seen) >= 64:
        raise ValueError('Cycle or excessive depth in native fork history.')
    base = meta.get('history_base')
    result = []
    if base:
        parent = find_parent(home, base['thread_id'])
        result = ancestry(home, parent, (*seen, sid))
        # An ancestor's own local records are bounded by the fork marker.
        result[-1] = (parent, base.get('end_byte_offset'), base.get('end_ordinal_exclusive'))
    result.append((Path(path), None, None))
    return result


def source_stamp(chain):
    return json.dumps([(str(p), p.stat().st_size, p.stat().st_mtime_ns, b, o)
                       for p, b, o in chain], separators=(',', ':'))


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def writer_lock(home, sid):
    """Respect native per-thread writer locks, including a resumed idle thread."""
    folder = Path(home) / 'thread-writer-locks'
    folder.mkdir(exist_ok=True)
    try:
        handle = (folder / f'{sid}.lock').open('a+b')
    except OSError:
        yield False
        return
    try:
        if os.name == 'nt':
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
        yield True
    finally:
        handle.close()


def snapshot(chain, output, sid, session, provider, model):
    meta = dict(first_meta(chain[-1][0]))
    meta.update(id=sid, session_id=sid, source='cli', model_provider=provider,
                thread_source='user', agent_history_import={'source': session['key']})
    for key in ('creator_user_id', 'creator_account_id', 'history_base', 'forked_from_id',
                'forked_from_ordinal_exclusive'):
        meta.pop(key, None)
    # A flattened fork is a new standalone root, with no live goal continuation.
    with Path(output).open('wb') as stream:
        def emit(record):
            stream.write((json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
        emit({'type': 'session_meta', 'timestamp': meta.get('timestamp') or session['started'], 'payload': meta})
        for path, byte_limit, ordinal_limit in chain:
            local_id = first_meta(path).get('id') or first_meta(path).get('session_id')
            for record in records(path, byte_limit, ordinal_limit):
                if record.get('type') == 'session_meta':
                    continue
                if record.get('type') == 'turn_context' and model:
                    record['payload']['model'] = model
                payload = record.get('payload')
                if isinstance(payload, dict):
                    for key in ('thread_id', 'session_id'):
                        if payload.get(key) == local_id:
                            payload[key] = sid
                emit(record)
        stream.flush()
        os.fsync(stream.fileno())


def register_native(home, sid, path, session, origin, cfg):
    """One native transaction: metadata and invalidation of cached history."""
    native = state_path(home)
    db = sqlite3.connect(native, timeout=30)
    db.row_factory = sqlite3.Row
    history = Path(home) / 'thread_history_1.sqlite'
    try:
        columns = {c['name']: c for c in db.execute('PRAGMA table_info(threads)')}
        if not {'id', 'rollout_path', 'model_provider', 'source'}.issubset(columns):
            raise ValueError('Unsupported native Codex threads schema.')
        if history.exists():
            db.execute('ATTACH DATABASE ? AS history_cache', (str(history),))
        timestamp = int(time.time())
        title = f"[{session['source']}] {session['title'] or session['id']}"[:200]
        values = dict(origin)
        values.update(id=sid, rollout_path=str(path), source='cli', thread_source='user',
                      model_provider=cfg.get('model_provider', 'openai'), cwd=session['cwd'],
                      title=title, name=title, preview=session['title'] or session['id'],
                      first_user_message=origin.get('first_user_message') or session['title'] or session['id'],
                      has_user_event=1, archived=0, archived_at=None, tokens_used=0,
                      model=cfg.get('model') or origin.get('model'),
                      reasoning_effort=cfg.get('model_reasoning_effort') or origin.get('reasoning_effort'),
                      approval_mode=cfg.get('approval_policy', 'on-request'),
                      sandbox_policy=json.dumps({'type': cfg.get('sandbox_mode', 'workspace-write')}),
                      agent_path=None, agent_nickname=None, agent_role=None, project_id=None,
                      thread_section_id=None, section_position=None, section_entered_at_ms=None,
                      is_pinned=0, creator_user_id=None, creator_account_id=None)
        for key in ('created_at', 'updated_at', 'recency_at'):
            values.setdefault(key, timestamp)
        for key in ('created_at_ms', 'updated_at_ms', 'recency_at_ms'):
            values.setdefault(key, timestamp * 1000)
        # Keep history_mode from the actual rollout; old transcripts default to legacy.
        values.setdefault('history_mode', 'legacy')
        for name, col in columns.items():
            if col['notnull'] and col['dflt_value'] is None and name not in values:
                raise ValueError(f'Unsupported required Codex column: {name}')
        values = {k: v for k, v in values.items() if k in columns}
        with db:
            if history.exists():
                tables = [r[0] for r in db.execute("SELECT name FROM history_cache.sqlite_master WHERE type='table'")]
                for table in tables:
                    cols = [r['name'] for r in db.execute(f'PRAGMA history_cache.table_info({quoted(table)})')]
                    if 'thread_id' in cols:
                        db.execute(f'DELETE FROM history_cache.{quoted(table)} WHERE thread_id=?', (sid,))
            names = ','.join(quoted(k) for k in values)
            updates = ','.join(f'{quoted(k)}=excluded.{quoted(k)}' for k in values if k != 'id')
            db.execute(f'INSERT INTO threads({names}) VALUES({",".join("?" for _ in values)}) '
                       f'ON CONFLICT(id) DO UPDATE SET {updates}', list(values.values()))
    finally:
        db.close()


def backup_native(db, target_name, target_home):
    """Keep the native state before this connection's first import."""
    if db.execute('SELECT 1 FROM codex_imports WHERE target=? LIMIT 1', (target_name,)).fetchone():
        return
    data_file = db.execute('PRAGMA database_list').fetchone()[2]
    root = Path(data_file).parent / 'native-backups' / target_name
    if root.exists() and any(root.iterdir()):
        return
    backup = root / datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True, exist_ok=True)
    for path in (state_path(target_home), Path(target_home) / 'thread_history_1.sqlite'):
        if path.exists():
            with contextlib.closing(readonly(path)) as source, contextlib.closing(sqlite3.connect(backup / path.name)) as dest:
                source.backup(dest)


def import_session(db, session, source_home, target_name, target_home, dry_run=False, prefer_native=True,
                   explicit_handoff=False, fresh=False, on_publish=None):
    is_claude = session['tool'] == 'claude'
    kinds = ('main', 'import') if explicit_handoff else ('main',)
    if not is_claude and (session['tool'] != 'codex' or session['kind'] not in kinds):
        raise ValueError('Native Codex import accepts original Codex main sessions or explicit Claude conversations only.')
    target_home = Path(target_home).resolve()
    source_home = Path(source_home).resolve() if source_home is not None else None
    cfg = config(target_home)
    if not is_claude and target_home == source_home and not fresh:
        return {'status': 'native', 'session': session['id'], 'path': session['path']}
    chain = [(Path(session['path']), None, None)] if is_claude else ancestry(source_home, session['path'])
    def current_stamp():
        stamp = source_stamp(chain)
        return json.dumps([stamp, session['cwd'], 'claude-text-v1']) if is_claude else stamp
    stamp = current_stamp()
    conversion = None
    if is_claude:
        import claude_to_codex
        conversion = claude_to_codex.preview(session)
    latest = None if fresh else db.execute('SELECT * FROM codex_imports WHERE target=? AND src_key=? '
                        'ORDER BY created DESC,rowid DESC LIMIT 1', (target_name, session['key'])).fetchone()
    status, sid, target = 'new', str(uuid.uuid4()), None
    if latest:
        target = Path(latest['dst_path'])
        if not target.exists():
            return {'status': 'deleted', 'session': latest['dst_key'].split(':', 1)[1]}
        untouched = target.stat().st_size == latest['dst_size'] and file_hash(target) == latest['dst_hash']
        if latest['src_stamp'] == stamp and untouched:
            unchanged = {'status': 'unchanged', 'session': latest['dst_key'].split(':', 1)[1], 'path': str(target)}
            if conversion is not None:
                unchanged['conversion'] = conversion
            if dry_run:
                return unchanged
            with writer_lock(target_home, unchanged['session']) as locked:
                if not locked or target.stat().st_size != latest['dst_size'] or file_hash(target) != latest['dst_hash']:
                    return dict(unchanged, status='busy')
                return unchanged
        if untouched:
            status, sid = 'refreshed', latest['dst_key'].split(':', 1)[1]
        else:
            status, target = 'forked', None
    elif not fresh and not is_claude and prefer_native and source_row(target_home, session['id']):
        # Some historical CODEX_HOMEs are copies. Do not duplicate an existing native id.
        return {'status': 'native', 'session': session['id']}
    if target is None:
        now = datetime.datetime.now()
        target = target_home / 'sessions' / now.strftime('%Y/%m/%d') / f'rollout-{now:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl'
    summary = dict(status=status, session=sid, path=str(target), source=session['id'], target=target_name)
    if conversion is not None:
        summary['conversion'] = conversion
    if dry_run:
        return summary
    state_path(target_home)  # Fail before writing files when the connection has not been initialized.
    backup_native(db, target_name, target_home)
    origin = {} if is_claude else source_row(source_home, session['id'])
    if not origin:
        origin['history_mode'] = 'legacy' if is_claude else first_meta(chain[-1][0]).get('history_mode', 'legacy')
    target.parent.mkdir(parents=True, exist_ok=True)
    with writer_lock(target_home, sid) as locked:
        if not locked:
            return dict(summary, status='busy')
        # Recheck after acquiring the native lock; preserve any concurrent continuation.
        if status == 'refreshed' and (target.stat().st_size != latest['dst_size'] or file_hash(target) != latest['dst_hash']):
            return dict(summary, status='busy')
        with tempfile.TemporaryDirectory(prefix='.agent-history-', dir=target.parent) as scratch:
            prepared = Path(scratch) / 'snapshot.jsonl'
            if is_claude:
                summary['conversion'] = claude_to_codex.snapshot(session, prepared, sid,
                                                               cfg.get('model_provider', 'openai'), cfg.get('model'))
            else:
                snapshot(chain, prepared, sid, session, cfg.get('model_provider', 'openai'), cfg.get('model'))
            if current_stamp() != stamp:
                return dict(summary, status='busy')  # Source changed while taking its snapshot.
            if on_publish:
                on_publish(summary)
            old = Path(scratch) / 'previous.jsonl'
            if target.exists():
                os.replace(target, old)
            os.replace(prepared, target)
            try:
                register_native(target_home, sid, target, session, origin, cfg)
                with db:
                    db.execute('INSERT OR REPLACE INTO codex_imports VALUES(?,?,?,?,?,?,?,?)',
                               ('codex:' + sid, target_name, session['key'], str(target),
                                datetime.datetime.now(datetime.timezone.utc).isoformat(), stamp,
                                file_hash(target), target.stat().st_size))
            except BaseException:
                target.unlink(missing_ok=True)
                if old.exists():
                    os.replace(old, target)
                raise
    # session_index is a name cache; native state remains authoritative for /resume.
    with (target_home / 'session_index.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'id': sid, 'thread_name': f"[{session['source']}] {session['title'] or session['id']}",
                                 'updated_at': session['updated']}, ensure_ascii=False) + '\n')
    return summary


def sync(db, target_name, homes, days=30, min_turns=2, dry_run=False, settle_seconds=120):
    if days < 1 or min_turns < 1 or settle_seconds < 0:
        raise ValueError('days/min-turns must be positive; settle-seconds must be nonnegative.')
    if target_name not in homes:
        raise ValueError('Unknown target connection: ' + target_name)
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
    rows = db.execute("SELECT * FROM sessions WHERE tool='codex' AND kind='main' AND updated>=? AND turns>=? "
                      'ORDER BY updated', (since, min_turns)).fetchall()
    report = dict(new=[], refreshed=[], forked=[], unchanged=0, native=0, deleted=0, busy=0, failed=[])
    for session in rows:
        if session['source'] not in homes:
            continue
        try:
            if time.time() - Path(session['path']).stat().st_mtime < settle_seconds:
                report['busy'] += 1
                continue
            result = import_session(db, session, homes[session['source']], target_name,
                                    homes[target_name], dry_run)
            status = result['status']
            if isinstance(report[status], list):
                report[status].append(result)
            else:
                report[status] += 1
        except (ValueError, OSError, sqlite3.Error) as error:
            report['failed'].append({'source': session['id'], 'error': str(error)})
    return report
