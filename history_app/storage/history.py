"""Unified Codex + Claude Code conversation history. Standard library only.

  collect                      incrementally index every Codex connection and Claude Code project
  sessions [--grep T]          list indexed sessions
  search QUERY                 full-text search (terms under 3 chars fall back to substring match)
  show ID [--tail N]           print one session's messages
  export ID -o FILE            write one session as Markdown
  import-claude ID [--cwd D]   copy a Codex session into Claude Code so /resume can open it
  mcp                          stdio MCP server (search_history, list_sessions, read_session)

Source transcripts are only read, never modified. The database lives in ~/.agent-history.
"""
import argparse
import contextlib
import datetime
import json
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid
import codex_import
import connections
from history_app import performance

from history_app.paths import SOURCE

ROOT = Path(os.environ.get('AGENT_HISTORY_SHARED_ROOT', SOURCE.parent)).resolve()
DATA = Path(os.environ.get('AGENT_HISTORY_HOME', Path.home() / '.agent-history'))
CLAUDE_HOMES = [Path.home() / '.claude']
CLIP = 4000          # stored length of tool calls/outputs
SNIPPET = 90
IMPORT_MARK = 'agent-history import'
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, tool TEXT, source TEXT, session_key TEXT,
    size INTEGER, mtime REAL, offset INTEGER, seq INTEGER);
CREATE TABLE IF NOT EXISTS sessions(key TEXT PRIMARY KEY, id TEXT, tool TEXT, source TEXT, path TEXT,
    cwd TEXT, title TEXT, started TEXT, updated TEXT, turns INTEGER DEFAULT 0, model TEXT,
    kind TEXT DEFAULT 'main', imported_from TEXT, version TEXT);
CREATE INDEX IF NOT EXISTS sessions_id ON sessions(id);
CREATE TABLE IF NOT EXISTS messages(rowid INTEGER PRIMARY KEY, session_key TEXT, seq INTEGER,
    role TEXT, text TEXT, ts TEXT, offset INTEGER);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_key, seq);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages',
    content_rowid='rowid', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text); END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text); END;
CREATE TABLE IF NOT EXISTS imports(dst_key TEXT PRIMARY KEY, src_key TEXT, dst_path TEXT, created TEXT,
    src_updated TEXT, dst_size INTEGER);
CREATE TABLE IF NOT EXISTS file_fingerprints(path TEXT PRIMARY KEY, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_views(path TEXT PRIMARY KEY, stamp TEXT NOT NULL,
    metadata TEXT NOT NULL, turns INTEGER NOT NULL, search_text TEXT NOT NULL);
"""

SEARCH_SCHEMA = """
CREATE INDEX IF NOT EXISTS files_session ON files(session_key);
CREATE VIRTUAL TABLE IF NOT EXISTS file_views_fts USING fts5(search_text,
    content='file_views', content_rowid='rowid', tokenize='trigram', detail='none');
CREATE TABLE IF NOT EXISTS file_view_search_coverage(view_rowid INTEGER PRIMARY KEY);
CREATE TRIGGER IF NOT EXISTS file_views_bi BEFORE INSERT ON file_views BEGIN
    INSERT INTO file_views_fts(file_views_fts,rowid,search_text)
        SELECT 'delete',v.rowid,v.search_text FROM file_views v
        JOIN file_view_search_coverage c ON c.view_rowid=v.rowid WHERE v.path=new.path;
    DELETE FROM file_view_search_coverage WHERE view_rowid IN
        (SELECT rowid FROM file_views WHERE path=new.path); END;
CREATE TRIGGER IF NOT EXISTS file_views_ai AFTER INSERT ON file_views BEGIN
    INSERT INTO file_views_fts(rowid,search_text) VALUES(new.rowid,new.search_text);
    INSERT INTO file_view_search_coverage VALUES(new.rowid) ON CONFLICT(view_rowid) DO NOTHING; END;
CREATE TRIGGER IF NOT EXISTS file_views_ad AFTER DELETE ON file_views BEGIN
    INSERT INTO file_views_fts(file_views_fts,rowid,search_text)
        SELECT 'delete',old.rowid,old.search_text WHERE EXISTS
        (SELECT 1 FROM file_view_search_coverage WHERE view_rowid=old.rowid);
    DELETE FROM file_view_search_coverage WHERE view_rowid=old.rowid; END;
CREATE TRIGGER IF NOT EXISTS file_views_au AFTER UPDATE ON file_views BEGIN
    INSERT INTO file_views_fts(file_views_fts,rowid,search_text)
        SELECT 'delete',old.rowid,old.search_text WHERE EXISTS
        (SELECT 1 FROM file_view_search_coverage WHERE view_rowid=old.rowid);
    INSERT INTO file_views_fts(rowid,search_text) VALUES(new.rowid,new.search_text);
    INSERT INTO file_view_search_coverage VALUES(new.rowid) ON CONFLICT(view_rowid) DO NOTHING; END;
"""


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / 'history.db', timeout=30)
    db.row_factory = sqlite3.Row
    try:
        if db.execute('PRAGMA user_version').fetchone()[0] >= 5:
            return db
        # First-window discovery and cached-list reads can open concurrently.
        # Recheck under the shared schema lock instead of rebuilding twice.
        with file_lock(DATA / 'schema.lock', True) as locked:
            if not locked:
                raise TimeoutError('History schema upgrade is busy.')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version < 5:
                upgrade(db, version)
        return db
    except Exception:
        db.close()
        raise


def upgrade(db, version):
    import conversation_titles
    if version >= 2:
        from .read_model import SCHEMA as READ_SCHEMA
        db.executescript('BEGIN IMMEDIATE;\n' + conversation_titles.SCHEMA + READ_SCHEMA +
                        '\nCREATE INDEX IF NOT EXISTS files_session ON files(session_key);\nPRAGMA user_version=5;\nCOMMIT;')
        return
    db.executescript(SCHEMA)
    db.executescript(codex_import.SCHEMA)
    import conversation_store
    db.executescript(conversation_store.SCHEMA)
    conversation_store.ensure(db)
    columns = [c[1] for c in db.execute('PRAGMA table_info(imports)')]
    for column, kind in (('src_updated', 'TEXT'), ('dst_size', 'INTEGER')):
        if column not in columns:
            db.execute(f'ALTER TABLE imports ADD COLUMN {column} {kind}')
    db.executescript(conversation_titles.SCHEMA)
    from .read_model import SCHEMA as READ_SCHEMA
    db.executescript('BEGIN IMMEDIATE;\n' + READ_SCHEMA +
                    '\nCREATE INDEX IF NOT EXISTS files_session ON files(session_key);\nPRAGMA user_version=5;\nCOMMIT;')


from history_app.locks import file_lock


def collect_lock(wait):
    return file_lock(DATA / 'collect.lock', wait)


def clip(value, limit=CLIP):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f'\n...[{len(text) - limit} chars truncated]'


def injected(text):
    # Harness-inserted context (<environment_context>, <system-reminder>, AGENTS.md, ...).
    return text.startswith(('<', '# AGENTS.md'))


# ---------------------------------------------------------------- sources

def codex_homes():
    yield from connections.homes(ROOT).items()


def codex_sessions(db, source=None, query='', limit=30, include_imports=False):
    """List each connection's files, even when content is indexed only once."""
    if source and source not in connections.homes(ROOT):
        raise ValueError('Unknown source connection: ' + source)
    kinds = ('main', 'import') if include_imports else ('main',)
    rows = db.execute('''SELECT s.*, f.source AS file_source, f.path AS file_path,
                         f.mtime AS modified FROM files f JOIN sessions s ON s.key=f.session_key
                         WHERE f.tool='codex' ORDER BY f.mtime DESC, f.path''')
    result, seen = [], set()
    for raw in rows:
        r = dict(raw)
        if (source and r['file_source'] != source) or r['kind'] not in kinds:
            continue
        r['source'], r['path'] = r.pop('file_source'), r.pop('file_path')
        key = (r['source'], r['id'])
        if key in seen:
            continue
        seen.add(key)
        r['archived'] = 'archived_sessions' in Path(r['path']).parts
        if query.casefold() not in ' '.join(str(r[k] or '') for k in ('title', 'cwd', 'id')).casefold():
            continue
        result.append(r)
        if limit is not None and len(result) >= limit:
            break
    return result


def resolve_codex(db, source, ref, include_imports=True):
    rows = codex_sessions(db, source, limit=None, include_imports=include_imports)
    ref = ref.removeprefix('codex:')
    matches = rows[:1] if ref == 'latest' else [r for r in rows if r['id'].startswith(ref)]
    if len(matches) != 1:
        raise ValueError('Session not found or ambiguous; specify a source and a full session ID.')
    row = matches[0]
    # Copies of the same ID may differ from the globally indexed copy.
    row['cwd'] = codex_import.first_meta(row['path']).get('cwd') or row['cwd']
    return row


def session_titles(home):
    titles = {}
    index = home / 'session_index.jsonl'
    if index.exists():
        for line in index.read_text(encoding='utf-8', errors='replace').splitlines():
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                titles[item.get('id')] = item.get('thread_name') or ''
    return titles


def discover():
    """Yield (tool, source, path, titles) for every transcript file."""
    import setup_backend
    selected = setup_backend.load_sources()
    for name, home in codex_homes():
        if selected is not None and name not in selected['codex']:
            continue
        titles = session_titles(home)
        for folder in ('sessions', 'archived_sessions'):
            for path in (home / folder).rglob('*.jsonl'):
                yield 'codex', name, path, titles
    for home in claude_homes():
        for path in (home / 'projects').glob('*/*.jsonl'):
                yield 'claude', 'claude', path, {}


def discover_paths(paths):
    """Resolve exact affected paths without enumerating unrelated directories.

    Respect the same enabled sources and directory boundaries as discover.
    This is indexing only; destructive callers still discover native forks.
    """
    import setup_backend
    selected = setup_backend.load_sources()
    candidates = [Path(path) for path in sorted(paths) if Path(path).match('*.jsonl')]
    for name, home in codex_homes():
        if selected is not None and name not in selected['codex']:
            continue
        roots = [home / 'sessions', home / 'archived_sessions']
        owned = [path for path in candidates if any(path.is_relative_to(root) for root in roots)]
        if owned:
            titles = session_titles(home)
            for path in owned:
                if path.is_file():
                    yield 'codex', name, path, titles
    for home in claude_homes():
        root = home / 'projects'
        for path in candidates:
            if path.is_relative_to(root) and len(path.relative_to(root).parts) == 2 and path.is_file():
                yield 'claude', 'claude', path, {}


def claude_homes():
    import setup_backend
    selected = setup_backend.load_sources()
    return [Path(path) for path in selected['claude']] if selected is not None else CLAUDE_HOMES


def read_new_lines(path, offset):
    """Yield (offset, record) for complete lines after offset; partial trailing lines wait."""
    with path.open('rb') as stream:
        stream.seek(offset)
        size = os.fstat(stream.fileno()).st_size
        while stream.tell() < size:
            start = stream.tell()
            line = stream.readline(size - start)
            if not line.endswith(b'\n'):
                return
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                yield start, json.loads(line)
            yield stream.tell(), None  # progress marker


# ---------------------------------------------------------------- parsers
# Each parser yields ('meta', dict) or ('msg', role, text, ts).

def parse_codex(record):
    kind, p, ts = record.get('type'), record.get('payload') or {}, record.get('timestamp')
    if kind == 'session_meta':
        source = p.get('source')
        sub = 'main'
        if p.get('agent_history_import'):
            sub = 'import'
        elif 'guardian' in json.dumps(source) or p.get('thread_source') == 'guardian_review':
            sub = 'guardian'
        elif isinstance(source, dict) and 'subagent' in source:
            sub = 'subagent'
        yield 'meta', dict(id=p.get('id') or p.get('session_id'), cwd=p.get('cwd'),
                           started=p.get('timestamp') or ts, kind=sub, version=p.get('cli_version'))
    elif kind == 'turn_context' and p.get('model'):
        yield 'meta', dict(model=p['model'])
    elif kind == 'compacted':
        yield 'msg', 'compacted', clip(p.get('message') or '[Codex compacted the history here]'), ts
    elif kind == 'response_item':
        item = p.get('type')
        if item == 'message' and p.get('role') in ('user', 'assistant'):
            text = '\n'.join(c.get('text', '') for c in p.get('content', []) if c.get('text')).strip()
            if text and not injected(text):
                yield 'msg', p['role'], text, ts
        elif item == 'agent_message':
            yield 'msg', 'agent', clip(p.get('content') or p.get('message') or p), ts
        elif item in ('function_call', 'custom_tool_call', 'local_shell_call', 'web_search_call'):
            args = p.get('arguments') or p.get('input') or p.get('action') or ''
            yield 'msg', 'tool_call', clip(f"{p.get('name', item)}: {clip(args, CLIP)}"), ts
        elif item in ('function_call_output', 'custom_tool_call_output'):
            yield 'msg', 'tool_output', clip(codex_output_text(p.get('output'))), ts


def codex_output_text(output):
    if isinstance(output, list):
        return '\n'.join(x.get('text', '') for x in output if isinstance(x, dict))
    if isinstance(output, dict):
        return str(output.get('content') or output.get('output') or json.dumps(output, ensure_ascii=False))
    return str(output or '')


def parse_claude(record):
    kind, ts = record.get('type'), record.get('timestamp')
    if kind in ('ai-title', 'custom-title', 'summary'):
        title = record.get('customTitle') or record.get('aiTitle') or record.get('summary')
        if title:
            yield 'meta', dict(title=title, title_rank=2 if kind == 'custom-title' else 1)
        return
    if kind not in ('user', 'assistant') or record.get('isSidechain'):
        return
    yield 'meta', dict(id=record.get('sessionId'), cwd=record.get('cwd'), started=ts,
                       version=record.get('version'), model=(record.get('message') or {}).get('model'))
    content = (record.get('message') or {}).get('content')
    if isinstance(content, str):
        if not injected(content.strip()) and not record.get('isMeta'):
            yield 'msg', kind, content.strip(), ts
        return
    for block in content or []:
        btype = block.get('type')
        if btype == 'text' and block.get('text', '').strip() and not injected(block['text'].strip()):
            yield 'msg', kind, block['text'].strip(), ts
        elif btype == 'tool_use':
            yield 'msg', 'tool_call', clip(f"{block.get('name')}: {clip(block.get('input'), CLIP)}"), ts
        elif btype == 'tool_result':
            body = block.get('content')
            if isinstance(body, list):
                body = '\n'.join(x.get('text', '') for x in body if isinstance(x, dict))
            yield 'msg', 'tool_output', clip(body or ''), ts


PARSERS = {'codex': parse_codex, 'claude': parse_claude}


# ---------------------------------------------------------------- collector

def prefix_hash(path, size):
    return prefix_digest(path, size).hexdigest()


def prefix_digest(path, size, start=0, state=None):
    value = state.copy() if state is not None else hashlib.sha256()
    with Path(path).open('rb') as stream:
        stream.seek(start)
        size -= start
        while size:
            chunk = stream.read(min(size, 1024 * 1024))
            if not chunk:
                raise ValueError('会话在校验期间被截断。')
            value.update(chunk)
            size -= len(chunk)
    return value


class CollectionResult(int):
    def __new__(cls, changed, failed=()):
        value = super().__new__(cls, changed)
        value.failed = list(failed)
        return value


def require_collected(result):
    if getattr(result, 'failed', None):
        raise ValueError(f'对话内容已保留，{len(result.failed)} 个文件的索引尚未完成；请重试索引。')
    return result


def index_failure(db, path, stage, error):
    value = dict(path=path, stage=stage, error=str(error))
    with db:
        db.execute('INSERT INTO index_failures VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET '
                   'stage=excluded.stage,error=excluded.error,updated=excluded.updated',
                   (path, stage, str(error), datetime.datetime.now(datetime.timezone.utc).isoformat()))
    return value


@connections.snapshot()
def collect(db, verbose=False, only_paths=None):
    started = time.monotonic()
    only_paths = {str(Path(path).resolve()) for path in only_paths} if only_paths is not None else None
    seen, changed, failed, verified, unreadable = set(), 0, [], {}, set()
    discovered = discover() if only_paths is None else discover_paths(only_paths)
    for tool, source, path, titles in discovered:
        key = str(path)
        if only_paths is not None and key not in only_paths:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            stat = path.stat()
        except OSError as error:
            failed.append(index_failure(db, key, 'read', error))
            unreadable.add(key)
            continue
        row = db.execute('SELECT * FROM files WHERE path=?', (key,)).fetchone()
        if row and row['size'] == stat.st_size and row['mtime'] == stat.st_mtime:
            if tool == 'codex' and row['session_key'] and row['seq'] >= 0:
                refresh_title(db, row['session_key'], titles)
            continue
        db.execute('SAVEPOINT collect_file')
        try:
            if row and row['seq'] < 0:
                row = None               # duplicate copy that changed: decide ownership again
            fingerprint = db.execute('SELECT hash FROM file_fingerprints WHERE path=?', (key,)).fetchone()
            digest = prefix_digest(path, row['offset']) if row and stat.st_size >= row['offset'] and fingerprint else None
            if row and (digest is None or digest.hexdigest() != fingerprint['hash']):
                forget_file(db, key)     # rewritten or truncated: rebuild from scratch
                row, digest = None, None
            if row:
                verified[key] = (row['offset'], fingerprint['hash'])
            ingest(db, tool, source, path, stat, row, titles, commit=False)
            final = path.stat()
            if (final.st_size, final.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ValueError('会话正在变化，保留已有索引，稍后重试。')
            indexed = db.execute('SELECT offset FROM files WHERE path=?', (key,)).fetchone()
            if indexed:
                current = prefix_digest(path, indexed['offset'], row['offset'], digest) if row else prefix_digest(path, indexed['offset'])
                db.execute('INSERT OR REPLACE INTO file_fingerprints VALUES (?,?)', (key, current.hexdigest()))
            final = path.stat()
            if (final.st_size, final.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ValueError('会话在校验期间变化，保留已有索引，稍后重试。')
            db.execute('RELEASE collect_file')
            changed += 1
        except (OSError, ValueError) as error:
            db.execute('ROLLBACK TO collect_file')
            db.execute('RELEASE collect_file')
            verified.pop(key, None)
            failed.append(index_failure(db, key, 'read', error))
            unreadable.add(key)
            continue
        if verbose:
            print(f'indexed {tool}:{source} {path.name}')
    for gone in [r['path'] for r in path_rows(db, 'SELECT path FROM files', only_paths) if r['path'] not in seen]:
        forget_file(db, gone)        # e.g. moved to archived_sessions; the new path is re-indexed
    db.commit()
    # A long-lived pre-upgrade MCP process may have seen a refreshed file with
    # the old parser. Reassert persistent import ownership before any new sync.
    with db:
        db.execute("""UPDATE sessions SET kind='import',
            imported_from=(SELECT src_key FROM codex_imports WHERE dst_key=sessions.key)
            WHERE key IN (SELECT dst_key FROM codex_imports)""")
    for raw in path_rows(db, '''SELECT s.*, f.source AS file_source, f.path AS file_path, f.mtime AS modified
                             FROM files f JOIN sessions s ON s.key=f.session_key''', only_paths, 'f.path'):
        row = dict(raw)
        row['indexed_path'] = row['path']
        row['path'], row['source'] = row.pop('file_path'), row.pop('file_source')
        if only_paths is not None and row['path'] not in only_paths:
            continue
        if row['path'] in unreadable:
            continue
        try:
            physical_view(db, row, _previous=verified.get(row['path']))
            with db:
                db.execute('DELETE FROM index_failures WHERE path=?', (row['path'],))
        except (ValueError, OSError) as error:
            failed.append(index_failure(db, row['path'], 'search', error))
    refresh_search_index(db, only_paths)
    performance.record('collection', time.monotonic() - started, files=len(seen), failed=len(failed))
    performance.flush(DATA)
    return CollectionResult(changed, failed)


def refresh_search_index(db, paths=None):
    """Backfill old cached views in bounded transactions during background collection.

    Until backfilled, queries use exact substring matching for uncovered rows.
    Opening an existing database never waits for a full FTS rebuild.
    """
    condition = ''
    if paths is not None:
        db.execute('CREATE TEMP TABLE IF NOT EXISTS search_backfill_scope(path TEXT PRIMARY KEY)')
        db.execute('DELETE FROM search_backfill_scope')
        db.executemany('INSERT OR IGNORE INTO search_backfill_scope VALUES (?)', [(str(path),) for path in paths])
        condition = ' AND path IN (SELECT path FROM search_backfill_scope)'
    while True:
        rows = db.execute('SELECT rowid,path,search_text FROM file_views WHERE rowid NOT IN '
                          '(SELECT view_rowid FROM file_view_part_coverage)' + condition + ' LIMIT 50').fetchall()
        if not rows:
            return
        with db:
            db.executemany('INSERT INTO file_view_parts(path,search_text) VALUES (?,?)', [(row[1], row[2]) for row in rows])
            db.executemany('INSERT INTO file_view_part_coverage VALUES (?)', [(row[0],) for row in rows])


def path_rows(db, query, paths, column='path'):
    """Bound SQLite parameters and fetched rows when refreshing a known scope."""
    if paths is None:
        yield from db.execute(query).fetchall()
        return
    values = sorted(paths)
    for offset in range(0, len(values), 500):
        batch = values[offset:offset + 500]
        yield from db.execute(query + ' WHERE ' + column + ' IN (' + ','.join('?' for _ in batch) + ')',
                              batch).fetchall()


def forget_file(db, path):
    row = db.execute('SELECT session_key FROM files WHERE path=?', (path,)).fetchone()
    key = row and row['session_key']
    if key and db.execute('SELECT 1 FROM sessions WHERE key=? AND path=?', (key, path)).fetchone():
        db.execute('DELETE FROM messages WHERE session_key=?', (key,))
        db.execute('DELETE FROM sessions WHERE key=?', (key,))
        # Duplicate copies of this session get re-evaluated as the new owner next run.
        db.execute('DELETE FROM files WHERE session_key=? AND seq<0', (key,))
    db.execute('DELETE FROM files WHERE path=?', (path,))
    db.execute('DELETE FROM file_fingerprints WHERE path=?', (path,))
    db.execute('DELETE FROM file_views WHERE path=?', (path,))
    db.execute('DELETE FROM index_failures WHERE path=?', (path,))


def physical_view(db, row, query='', *, _previous=None):
    """Cache file-specific metadata/search text; shared native IDs stay separate."""
    import launch_backend as launcher
    stamp = json.dumps(launcher.transcript_stamp(row))
    cached = db.execute('''SELECT metadata,turns,
                           CASE WHEN ?='' THEN 0 ELSE instr(search_text,?)>0 END AS matched
                           FROM file_views WHERE path=? AND stamp=?''',
                        (query, query.casefold(), row['path'], stamp)).fetchone()
    if cached:
        value = dict(row, **json.loads(cached['metadata']), turns=cached['turns'])
        if row['kind'] == 'import':
            value['kind'] = 'import'
        if row.get('indexed_path', row['path']) == row['path'] and row.get('title'):
            value['title'] = row['title']
        return value, bool(query and cached['matched'])
    value = launcher.physical_row(row)
    from . import read_model
    if _previous and read_model.append(db, row, value, stamp, _previous):
        return physical_view(db, row, query)
    messages = launcher.file_messages(value)
    if json.dumps(launcher.transcript_stamp(row)) != stamp:
        raise ValueError('会话正在变化，请稍后刷新。')
    value['turns'] = sum(message['role'] == 'user' for message in messages)
    metadata = {name: value.get(name) for name in ('cwd', 'title', 'kind', 'started')}
    metadata['message_count'] = len(messages)
    text = '\n'.join(message['text'] for message in messages).casefold()
    with db:
        fields = (stamp, json.dumps(metadata, ensure_ascii=False), value['turns'], text, row['path'])
        if db.execute('SELECT 1 FROM file_views WHERE path=?', (row['path'],)).fetchone():
            db.execute('UPDATE file_views SET stamp=?,metadata=?,turns=?,search_text=? WHERE path=?', fields)
        else:
            db.execute('INSERT INTO file_views VALUES (?,?,?,?,?)', (row['path'], *fields[:4]))
        read_model.checkpoint(db, row)
    return value, bool(query and query.casefold() in text)


def codex_session_id(path):
    """Codex connection homes were seeded by copying rollouts, so one id can live in several homes."""
    with path.open('rb') as stream:
        try:
            meta = json.loads(stream.readline()).get('payload') or {}
        except (ValueError, UnicodeDecodeError):
            return None
    return meta.get('id') or meta.get('session_id')


def refresh_title(db, key, titles):
    sid = key.split(':', 1)[1]
    if titles.get(sid):
        db.execute("UPDATE sessions SET title=? WHERE key=? AND title!=?", (titles[sid], key, titles[sid]))


def ingest(db, tool, source, path, stat, row, titles, *, commit=True):
    offset, seq, key = (row['offset'], row['seq'], row['session_key']) if row else (0, 0, None)
    parser, meta, pending, last_ts = PARSERS[tool], {}, [], None
    if tool == 'claude':
        key = 'claude:' + path.stem       # Claude names each file after its session id
    elif not row:
        sid = codex_session_id(path)
        if not sid:
            raise ValueError('会话头缺失或损坏，保留已有索引，等待文件完整后重试。')
        key = 'codex:' + sid
        owner = db.execute('SELECT s.path, f.size FROM sessions s LEFT JOIN files f ON f.path = s.path '
                           'WHERE s.key=?', (key,)).fetchone()
        if owner and owner['path'] != str(path):
            if Path(owner['path']).exists() and (owner['size'] or 0) >= stat.st_size:
                db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?)',
                           (str(path), tool, source, key, stat.st_size, stat.st_mtime, stat.st_size, -1))
                return
            with db if commit else contextlib.nullcontext():  # this longer copy takes over the session
                db.execute('DELETE FROM messages WHERE session_key=?', (key,))
                db.execute('DELETE FROM sessions WHERE key=?', (key,))
                db.execute('UPDATE files SET seq=-1 WHERE path=?', (owner['path'],))
    for position, record in read_new_lines(path, offset):
        if record is None:
            offset = position
            continue
        for event in parser(record):
            if event[0] == 'meta':
                for k, v in event[1].items():
                    if v and (k not in meta or k in ('model', 'version') or
                              (k == 'title' and event[1].get('title_rank', 0) >= meta.get('title_rank', 0))):
                        meta[k] = v
            else:
                _, role, text, ts = event
                pending.append((role, text, ts, position))
                last_ts = ts or last_ts
    with db if commit else contextlib.nullcontext():
        existing = db.execute('SELECT path FROM sessions WHERE key=?', (key,)).fetchone()
        if not existing:
            imported = db.execute('SELECT src_key FROM imports WHERE dst_key=?', (key,)).fetchone()
            if not imported and tool == 'codex':
                imported = db.execute('SELECT src_key FROM codex_imports WHERE dst_key=?', (key,)).fetchone()
            db.execute('INSERT INTO sessions(key, id, tool, source, path, kind, imported_from) VALUES (?,?,?,?,?,?,?)',
                       (key, key.split(':', 1)[1], tool, source, str(path),
                        'import' if imported else 'main', imported['src_key'] if imported else None))
        updates = {k: meta[k] for k in ('model', 'version') if meta.get(k)}
        if meta.get('cwd'):
            # Claude records the shell's current directory per line; the project is where it started.
            db.execute('UPDATE sessions SET cwd=? WHERE key=? AND (cwd IS NULL OR ?)',
                       (meta['cwd'], key, tool == 'codex'))
        if tool == 'codex' and meta.get('kind'):
            updates['kind'] = 'import' if db.execute('SELECT 1 FROM codex_imports WHERE dst_key=?', (key,)).fetchone() else meta['kind']
        title = meta.get('title') or titles.get(key.split(':', 1)[1])
        if title:
            updates['title'] = title
        for k, v in updates.items():
            db.execute(f'UPDATE sessions SET {k}=? WHERE key=?', (v, key))
        if meta.get('started'):
            db.execute('UPDATE sessions SET started=? WHERE key=? AND started IS NULL', (meta['started'], key))
        db.executemany('INSERT INTO messages(session_key, seq, role, text, ts, offset) VALUES (?,?,?,?,?,?)',
                       [(key, seq + i, r, t, ts, pos) for i, (r, t, ts, pos) in enumerate(pending)])
        seq += len(pending)
        if last_ts:
            db.execute('UPDATE sessions SET updated=? WHERE key=?', (last_ts, key))
        db.execute("""UPDATE sessions SET turns=(SELECT count(*) FROM messages WHERE session_key=? AND role='user'),
                      title=COALESCE(NULLIF(title, ''), (SELECT substr(text, 1, 80) FROM messages
                          WHERE session_key=? AND role='user' ORDER BY seq LIMIT 1)) WHERE key=?""", (key, key, key))
        db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?)',
                   (str(path), tool, source, key, stat.st_size, stat.st_mtime, offset, seq))


# ---------------------------------------------------------------- queries

def resolve(db, ref):
    tool = next((name for name in ('codex', 'claude') if ref.startswith(name + ':')), None)
    if tool:
        ref = ref[len(tool) + 1:]
    pattern = ref.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    sql = "SELECT * FROM sessions WHERE id LIKE ? ESCAPE '\\'"
    params = [pattern]
    if tool:
        sql += ' AND tool=?'
        params.append(tool)
    rows = db.execute(sql + ' ORDER BY updated DESC', params).fetchall()
    if len(rows) != 1:
        hint = ', '.join(r['key'] for r in rows[:5])
        raise ValueError(f'{len(rows)} sessions match {ref!r}' + (f': {hint}' if rows else ''))
    return rows[0]


def list_sessions(db, tool=None, grep=None, limit=30, include_all=False):
    sql, args = 'SELECT * FROM sessions WHERE 1=1', []
    if not include_all:
        sql += " AND kind='main'"
    if tool:
        sql, args = sql + ' AND tool=?', args + [tool]
    if grep:
        sql += ' AND (title LIKE ? OR cwd LIKE ? OR id LIKE ?)'
        args += [f'%{grep}%'] * 3
    return db.execute(sql + ' ORDER BY updated DESC LIMIT ?', args + [limit]).fetchall()


def search(db, query, tool=None, cwd=None, roles=None, limit=20, include_all=False):
    terms = [t.strip('"') for t in query.split() if t.strip('"')]
    if not terms:
        return []
    long_terms = [t for t in terms if len(t) >= 3]
    sql = ('SELECT m.*, s.tool, s.source, s.id AS sid, s.title, s.cwd FROM messages m '
           'JOIN sessions s ON s.key = m.session_key')
    where, args = [], []
    if long_terms:
        sql += ' JOIN messages_fts f ON f.rowid = m.rowid'
        where.append('messages_fts MATCH ?')
        args.append(' AND '.join('"' + t.replace('"', '""') + '"' for t in long_terms))
    for t in terms:
        if len(t) < 3:
            where.append('m.text LIKE ?')
            args.append(f'%{t}%')
    if not include_all:
        where.append("s.kind='main'")
    if tool:
        where.append('s.tool=?')
        args.append(tool)
    if cwd:
        where.append('s.cwd LIKE ?')
        args.append(f'%{cwd}%')
    where.append('m.role IN (%s)' % ','.join('?' * len(roles or ['user', 'assistant'])))
    args += roles or ['user', 'assistant']
    sql += ' WHERE ' + ' AND '.join(where) + ' ORDER BY m.ts DESC LIMIT ?'
    return [dict(r, snippet=snippet(r['text'], terms)) for r in db.execute(sql, args + [limit])]


def snippet(text, terms):
    lower = text.lower()
    hits = [lower.find(t.lower()) for t in terms if lower.find(t.lower()) >= 0]
    at = min(hits) if hits else 0
    start = max(0, at - SNIPPET)
    out = text[start:at + SNIPPET * 2].replace('\n', ' ')
    return ('…' if start else '') + out + ('…' if at + SNIPPET * 2 < len(text) else '')


def messages(db, key, roles=None, head=None, tail=None):
    rows = db.execute('SELECT * FROM messages WHERE session_key=? ORDER BY seq', (key,)).fetchall()
    if roles:
        rows = [r for r in rows if r['role'] in roles]
    if tail:
        rows = rows[-tail:]
    if head:
        rows = rows[:head]
    return rows


def render(session, rows, tool_chars=600):
    out = [f"# {session['title'] or session['id']}\n",
           f"- source: {session['tool']}:{session['source']}  id: `{session['id']}`",
           f"- cwd: `{session['cwd']}`  started: {session['started']}  updated: {session['updated']}",
           f"- transcript: `{session['path']}`\n"]
    for r in rows:
        if r['role'] in ('tool_call', 'tool_output'):
            out.append(f"**{r['role']}**\n```\n{clip(r['text'], tool_chars)}\n```\n")
        else:
            out.append(f"## {r['role'].upper()}  <sub>{r['ts'] or ''}</sub>\n\n{r['text']}\n")
    return '\n'.join(out)


# ---------------------------------------------------------------- Codex -> Claude import

def claude_project_dir(cwd):
    name = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
    if len(name) > 200:
        raise ValueError('Working directory path is too long for a Claude project name; pass --cwd.')
    homes = claude_homes()
    if not homes:
        raise ValueError('No Claude history directory selected; open the setup wizard.')
    return homes[0] / 'projects' / name


def codex_items(path, source_home=None):
    """Codex transcript as (role, text, ts) turns, starting from the last compaction if any."""
    items = []
    chain = codex_import.ancestry(source_home, path) if source_home is not None else [(path, None, None)]
    records = (record for p, byte_limit, ordinal_limit in chain
               for record in codex_import.records(p, byte_limit, ordinal_limit))
    for record in records:
        if record.get('type') == 'compacted':
            p = record.get('payload') or {}
            items = [('note', 'Codex 在此处压缩了更早的历史，以下是压缩后保留的内容。', record.get('timestamp'))]
            for entry in p.get('replacement_history') or []:
                for event in parse_codex({'type': 'response_item', 'payload': entry, 'timestamp': record.get('timestamp')}):
                    if event[0] == 'msg':
                        items.append(event[1:])
            if p.get('message'):
                items.append(('note', 'Codex 压缩摘要：\n' + p['message'], record.get('timestamp')))
            continue
        for event in parse_codex(record):
            if event[0] == 'msg' and event[1] != 'compacted':
                items.append(event[1:])
    return items


def build_turns(items):
    turns = []
    for role, text, ts in items:
        if role == 'user' or not turns:
            turns.append(dict(user=[], rest=[], ts=ts))
        (turns[-1]['user'] if role == 'user' else turns[-1]['rest']).append((role, text))
    return turns


def assistant_text(rest, detailed, tool_chars):
    parts, skipped = [], 0
    for role, text in rest:
        if role in ('assistant', 'agent'):
            parts.append(text)
        elif role == 'note':
            parts.append(f'> {text}')
        elif detailed:
            label = '工具调用' if role == 'tool_call' else '工具输出'
            parts.append(f'> [Codex {label}]\n```\n{clip(text, tool_chars)}\n```')
        else:
            skipped += 1
    if skipped:
        parts.append(f'（此轮省略了 {skipped // 2 or skipped} 次 Codex 工具调用）')
    return '\n\n'.join(parts)


def import_claude(db, ref, cwd=None, max_chars=200_000, detail_turns=5, tool_chars=600, dry_run=False, sid=None):
    session = resolve(db, ref) if isinstance(ref, str) else ref
    if session['tool'] != 'codex':
        raise ValueError('import-claude only takes Codex sessions.')
    cwd = cwd or session['cwd']
    turns = build_turns(codex_items(Path(session['path']), dict(codex_homes()).get(session['source'])))
    if not turns:
        raise ValueError('No conversation text in this session.')
    rendered, used = [], 0
    for index in range(len(turns) - 1, -1, -1):
        turn = turns[index]
        user = '\n\n'.join(t for _, t in turn['user']) or '（Codex 会话开头没有用户消息）'
        reply = assistant_text(turn['rest'], len(turns) - index <= detail_turns, tool_chars)
        cost = len(user) + len(reply)
        if rendered and used + cost > max_chars:
            break
        rendered.insert(0, (user, reply, turn['ts']))
        used += cost
    omitted = len(turns) - len(rendered)
    preamble = (f'[{IMPORT_MARK}] 以下是从 Codex 会话导入的只读副本，不是原生 Claude 会话。\n'
                f"- 来源：codex:{session['source']}  会话 {session['id']}\n"
                f"- 原工作目录：{session['cwd']}\n- 原始记录：{session['path']}\n"
                + (f'- 更早的 {omitted} 轮已省略，可用 `agent_history.py show {session["id"][:13]}` 查看。\n' if omitted else '')
                + '- 工具调用只保留了最近几轮的截断摘要；隐藏推理和图片不包含在内。\n'
                  '- 集群任务、远程机器和文件状态可能已经变化，继续之前请先核实当前状态。\n\n---\n\n')
    sid = sid or str(uuid.uuid4())
    version = (db.execute("SELECT version FROM sessions WHERE tool='claude' AND version IS NOT NULL "
                          'ORDER BY updated DESC LIMIT 1').fetchone() or {'version': '2.1.0'})['version']
    base = dict(isSidechain=False, userType='external', cwd=cwd, sessionId=sid, version=version, gitBranch='')
    lines, parent = [], None
    for i, (user, reply, ts) in enumerate(rendered):
        ts = ts or datetime.datetime.now(datetime.timezone.utc).isoformat()
        uid = str(uuid.uuid4())
        lines.append(dict(base, parentUuid=parent, type='user', uuid=uid, timestamp=ts,
                          message=dict(role='user', content=(preamble if i == 0 else '') + user)))
        parent = uid
        reply = reply or '（Codex 没有对这条消息作出文字回复。）'
        uid = str(uuid.uuid4())
        lines.append(dict(base, parentUuid=parent, type='assistant', uuid=uid, timestamp=ts,
                          message=dict(id='msg_import_' + uuid.uuid4().hex[:20], type='message', role='assistant',
                                       model='<synthetic>', content=[dict(type='text', text=reply)],
                                       stop_reason='end_turn', stop_sequence=None,
                                       usage=dict(input_tokens=0, output_tokens=0))))
        parent = uid
    title = '[Codex] ' + (session['title'] or session['id'])[:70]
    lines.append(dict(type='custom-title', customTitle=title, sessionId=sid))
    lines.append(dict(type='ai-title', aiTitle=title, sessionId=sid))
    target = claude_project_dir(cwd) / f'{sid}.jsonl'
    summary = dict(session=sid, path=str(target), cwd=cwd, turns=len(rendered), omitted=omitted,
                   chars=used, title=title, cwd_exists=Path(cwd).is_dir())
    if dry_run:
        return summary
    target.parent.mkdir(parents=True, exist_ok=True)
    with db:
        db.execute('INSERT OR REPLACE INTO imports(dst_key, src_key, dst_path, created, src_updated) '
                   'VALUES (?,?,?,?,?)', ('claude:' + sid, session['key'], str(target),
                                          datetime.datetime.now().isoformat(timespec='seconds'), session['updated']))
    body = ''.join(json.dumps(l, ensure_ascii=False) + '\n' for l in lines).encode('utf-8')
    temp = target.with_suffix('.tmp')
    temp.write_bytes(body)
    temp.replace(target)
    db.execute('UPDATE imports SET dst_size=? WHERE dst_key=?', (len(body), 'claude:' + sid))
    forget_file(db, str(target))    # rewritten in place: let the collector re-read it from the start
    db.commit()
    return summary


def continued_in_claude(path):
    """True once the user has sent a message in an imported copy (it is then their branch)."""
    with contextlib.suppress(OSError):
        for _, record in read_new_lines(Path(path), 0):
            if record and record.get('type') in ('user', 'assistant'):
                if record.get('promptId') or (record.get('message') or {}).get('model') not in (None, '<synthetic>'):
                    return True
    return False


def sync_claude(db, days=30, min_turns=2, dry_run=False, **options):
    """Import recent Codex sessions into Claude. Re-running refreshes untouched copies in place;
    copies the user already continued are left alone and a fresh copy is made; deleted copies stay deleted."""
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')
    rows = db.execute("SELECT * FROM sessions WHERE tool='codex' AND kind='main' AND updated>=? AND turns>=? "
                      'ORDER BY updated', (since, min_turns)).fetchall()
    report = dict(new=[], refreshed=[], forked=[], unchanged=0, deleted=0, failed=[])
    for session in rows:
        latest = db.execute('SELECT * FROM imports WHERE src_key=? ORDER BY created DESC LIMIT 1',
                            (session['key'],)).fetchone()
        sid, bucket = None, 'new'
        if latest:
            if not Path(latest['dst_path']).exists():
                report['deleted'] += 1
                continue
            if (latest['src_updated'] or '') >= (session['updated'] or ''):
                report['unchanged'] += 1
                continue
            if continued_in_claude(latest['dst_path']):
                bucket = 'forked'
            elif (Path(latest['dst_path']).stat().st_size != latest['dst_size'] and
                  time.time() - Path(latest['dst_path']).stat().st_mtime < 1800):
                report['unchanged'] += 1     # opened in Claude in the last 30 min; don't rewrite under it
                continue
            else:
                sid, bucket = latest['dst_key'].split(':', 1)[1], 'refreshed'
        if dry_run:
            report[bucket].append(session['id'])
            continue
        try:
            result = import_claude(db, session, sid=sid, **options)
            report[bucket].append(f"{session['id'][:13]} → {result['path']}")
        except (ValueError, OSError) as error:
            report['failed'].append(f"{session['id'][:13]}: {error}")
    return report


# ---------------------------------------------------------------- MCP server

MCP_TOOLS = [
    dict(name='search_history',
         description='Full-text search over past Codex and Claude Code conversations on this machine. '
                     'Returns matching messages with session ids; use read_session to open one.',
         inputSchema=dict(type='object', required=['query'], properties=dict(
             query=dict(type='string', description='Space-separated terms, all must match'),
             tool=dict(type='string', enum=['codex', 'claude']),
             cwd=dict(type='string', description='Substring of the working directory'),
             limit=dict(type='integer', default=20)))),
    dict(name='list_sessions',
         description='List recent Codex and Claude Code sessions, newest first.',
         inputSchema=dict(type='object', properties=dict(
             tool=dict(type='string', enum=['codex', 'claude']),
             query=dict(type='string', description='Filter on title, cwd or id'),
             limit=dict(type='integer', default=30)))),
    dict(name='read_session',
         description='Read the messages of one session by id or unique id prefix.',
         inputSchema=dict(type='object', required=['id'], properties=dict(
             id=dict(type='string'),
             tail=dict(type='integer', description='Only the last N messages'),
             include_tools=dict(type='boolean', default=False)))),
]


def mcp_call(name, args):
    with contextlib.ExitStack() as stack:
        with collect_lock(wait=False) as locked:
            db = stack.enter_context(contextlib.closing(connect()))
            if locked:
                collect(db)       # skipped while a scheduled run holds the lock
        if name == 'search_history':
            rows = search(db, args['query'], args.get('tool'), args.get('cwd'), limit=args.get('limit', 20))
            return '\n\n'.join(f"[{r['tool']}:{r['source']}] {r['sid']} ({r['ts']}) {r['title']}\n"
                               f"  cwd={r['cwd']}\n  {r['role']}: {r['snippet']}" for r in rows) or 'No matches.'
        if name == 'list_sessions':
            rows = list_sessions(db, args.get('tool'), args.get('query'), args.get('limit', 30))
            return '\n'.join(session_line(r) for r in rows) or 'No sessions.'
        if name == 'read_session':
            session = resolve(db, args['id'])
            roles = None if args.get('include_tools') else ['user', 'assistant', 'compacted', 'agent']
            return clip(render(session, messages(db, session['key'], roles, tail=args.get('tail'))), 60_000)
        raise ValueError('Unknown tool ' + name)


def serve_mcp():
    stdin = sys.stdin.buffer
    for line in stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        mid, method = msg.get('id'), msg.get('method')
        if mid is None:
            continue          # notifications need no reply
        if method == 'initialize':
            result = dict(protocolVersion=msg.get('params', {}).get('protocolVersion', '2025-06-18'),
                          capabilities=dict(tools={}), serverInfo=dict(name='agent-history', version='1.0'))
        elif method == 'tools/list':
            result = dict(tools=MCP_TOOLS)
        elif method == 'tools/call':
            try:
                params = msg.get('params') or {}
                if not isinstance(params, dict) or not isinstance(params.get('arguments', {}), dict):
                    raise ValueError('工具参数必须是对象。')
                result = dict(content=[dict(type='text', text=mcp_call(params.get('name'), params.get('arguments') or {}))])
            except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
                result = dict(content=[dict(type='text', text=f'Error: {error}')], isError=True)
        elif method == 'ping':
            result = {}
        else:
            reply = dict(jsonrpc='2.0', id=mid, error=dict(code=-32601, message='Method not found'))
            sys.stdout.write(json.dumps(reply) + '\n')
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps(dict(jsonrpc='2.0', id=mid, result=result), ensure_ascii=False) + '\n')
        sys.stdout.flush()


# ---------------------------------------------------------------- CLI

def session_line(r):
    flag = {'import': ' [imported]', 'guardian': ' [guardian]', 'subagent': ' [subagent]'}.get(r['kind'], '')
    return (f"{(r['updated'] or '')[:16]}  {r['tool']}:{r['source']:<9} {r['id']}  turns={r['turns']:<3} "
            f"{(r['title'] or '').replace(chr(10), ' ')[:60]}{flag}\n    cwd={r['cwd']}")


def log(text):
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / 'collect.log'
    if path.exists() and path.stat().st_size > 1_000_000:
        path.replace(path.with_suffix('.log.1'))
    with path.open('a', encoding='utf-8') as fh:
        fh.write(f'{datetime.datetime.now().isoformat(timespec="seconds")} {text}\n')


def main():
    global ROOT, DATA
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--shared-root', type=Path, help='directory containing connections.json and common.toml')
    ap.add_argument('--data-dir', type=Path, help='history database and runtime data directory')
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('collect')
    c.add_argument('-v', '--verbose', action='store_true')
    c = sub.add_parser('list-codex', help='connection-aware conversation list from the unified catalog')
    c.add_argument('--source')
    c.add_argument('--query', default='')
    c.add_argument('--limit', type=int, default=30)
    c.add_argument('--json', action='store_true')
    c.add_argument('--include-imports', action='store_true')
    c = sub.add_parser('resolve-codex')
    c.add_argument('--source', required=True)
    c.add_argument('--session', required=True)
    c.add_argument('--originals-only', action='store_true')
    c = sub.add_parser('add-connection')
    c.add_argument('name')
    c.add_argument('--home', required=True)
    c.add_argument('--key-env')
    c.add_argument('--code-mode-host', action='store_true')
    s = sub.add_parser('sessions')
    s.add_argument('--tool', choices=['codex', 'claude'])
    s.add_argument('--grep')
    s.add_argument('-n', type=int, default=30)
    s.add_argument('--all', action='store_true', help='include guardian/subagent sessions and imported copies')
    q = sub.add_parser('search')
    q.add_argument('query')
    q.add_argument('--tool', choices=['codex', 'claude'])
    q.add_argument('--cwd')
    q.add_argument('--tools', action='store_true', help='also search tool calls and outputs')
    q.add_argument('-n', type=int, default=20)
    q.add_argument('--all', action='store_true')
    for name in ('show', 'export'):
        p = sub.add_parser(name)
        p.add_argument('id')
        p.add_argument('--tail', type=int)
        p.add_argument('--head', type=int)
        p.add_argument('--no-tools', action='store_true')
        p.add_argument('--tool-chars', type=int, default=600)
        if name == 'export':
            p.add_argument('-o', required=True)
    i = sub.add_parser('import-claude')
    i.add_argument('id')
    i.add_argument('--cwd', help='working directory for the new Claude session (default: the Codex cwd)')
    i.add_argument('--max-chars', type=int, default=200_000)
    i.add_argument('--detail-turns', type=int, default=5, help='recent turns that keep tool summaries')
    i.add_argument('--dry-run', action='store_true')
    y = sub.add_parser('sync-claude', help='import/refresh recent Codex sessions into Claude /resume')
    y.add_argument('--days', type=int, default=30)
    y.add_argument('--min-turns', type=int, default=2)
    y.add_argument('--dry-run', action='store_true')
    y.add_argument('--quiet', action='store_true', help='print nothing unless something changed (for hooks)')
    i = sub.add_parser('import-codex', help='import a Codex or Claude session into native Codex /resume')
    i.add_argument('id')
    i.add_argument('--to', required=True, help='target registered connection')
    i.add_argument('--source', help='explicit source connection (including duplicated historical ids)')
    i.add_argument('--cwd', help='target working directory for Claude conversion')
    i.add_argument('--dry-run', action='store_true')
    y = sub.add_parser('sync-codex', help='import/refresh recent original Codex sessions into native /resume')
    y.add_argument('--to', required=True)
    y.add_argument('--days', type=int, default=30)
    y.add_argument('--min-turns', type=int, default=2)
    y.add_argument('--settle-seconds', type=int, default=120)
    y.add_argument('--dry-run', action='store_true')
    y.add_argument('--quiet', action='store_true')
    sub.add_parser('mcp')
    args = ap.parse_args()
    if args.shared_root:
        ROOT = args.shared_root.expanduser().resolve()
    if args.data_dir:
        DATA = args.data_dir.expanduser().resolve()

    if args.cmd == 'mcp':
        return serve_mcp()
    if args.cmd == 'add-connection':
        connections.register(ROOT, args.name, args.home, args.key_env, args.code_mode_host)
        return print('Registered ' + args.name)
    if args.cmd in ('list-codex', 'resolve-codex'):
        with collect_lock(wait=True) as locked:
            if not locked:
                raise ValueError('Another history operation holds the collector lock.')
            db = connect()
            try:
                collect(db)
                if args.cmd == 'resolve-codex':
                    print(json.dumps(resolve_codex(db, args.source, args.session,
                                                   include_imports=not args.originals_only), ensure_ascii=False))
                else:
                    if args.limit < 1:
                        raise ValueError('--limit must be positive.')
                    rows = codex_sessions(db, args.source, args.query, args.limit, args.include_imports)
                    if args.json:
                        print(json.dumps(rows, ensure_ascii=False))
                    else:
                        for r in rows:
                            date = datetime.datetime.fromtimestamp(r['modified']).strftime('%m-%d %H:%M')
                            print(f"{date}  {r['source']:10}  {r['id']}  {r['title'] or r['cwd']}")
            finally:
                db.close()
        return
    if args.cmd in ('import-codex', 'sync-codex'):
        with collect_lock(wait=True) as locked:
            if not locked:
                raise ValueError('Another history operation holds the collector lock.')
            db = connect()
            try:
                collect(db)
                homes = dict(codex_homes())
                registry = json.loads((ROOT / 'connections.json').read_text(encoding='utf-8-sig'))
                entry = registry['connections'].get(args.to)
                if not entry or entry.get('history_only'):
                    raise ValueError('Target must be a launchable registered Codex connection.')
                if args.cmd == 'import-codex':
                    session = (resolve(db, 'claude:' + args.id.removeprefix('claude:')) if args.source == 'claude' else
                               resolve_codex(db, args.source, args.id, include_imports=False)
                               if args.source else resolve(db, args.id))
                    session = dict(session)
                    if args.cwd:
                        if session['tool'] != 'claude':
                            raise ValueError('--cwd is only supported for Claude to Codex conversion.')
                        if not Path(args.cwd).is_dir():
                            raise ValueError('Target working directory does not exist.')
                        session['cwd'] = str(Path(args.cwd).resolve())
                    source_home = homes[session['source']] if session['tool'] == 'codex' else None
                    result = codex_import.import_session(db, session, source_home,
                                                         args.to, homes[args.to], args.dry_run, prefer_native=False)
                    if result['status'] in ('busy', 'deleted'):
                        raise ValueError('Import unavailable: ' + result['status'])
                    if not args.dry_run:
                        collect(db)
                    print(json.dumps(result, ensure_ascii=False))
                else:
                    report = codex_import.sync(db, args.to, homes, args.days, args.min_turns,
                                               args.dry_run, args.settle_seconds)
                    if not args.dry_run:
                        collect(db)
                    counts = {k: len(v) if isinstance(v, list) else v for k, v in report.items()}
                    log(f'sync-codex {args.to} {counts}')
                    if args.quiet:
                        if report['new'] or report['refreshed'] or report['forked'] or report['failed']:
                            print('agent-history: ' + args.to + ' ' + json.dumps(counts))
                    else:
                        print(json.dumps(report, ensure_ascii=False, indent=1))
                    if report['failed']:
                        return sys.exit(1)
            finally:
                db.close()
        return
    if args.cmd in ('collect', 'sync-claude'):
        with collect_lock(wait=args.cmd == 'sync-claude') as locked:
            if not locked:
                return print('another collector is running; skipped')
            started = datetime.datetime.now()
            try:
                db = connect()
                changed = collect(db, getattr(args, 'verbose', False))
                if args.cmd == 'sync-claude':
                    report = sync_claude(db, args.days, args.min_turns, args.dry_run)
                    if not args.dry_run:
                        collect(db)      # index the copies just written so they are marked as imports
                elif args.cmd == 'collect':
                    # Reuse the existing five-minute collector task. Only explicitly
                    # enabled connections get native imports; launch-time sync also works.
                    registry = json.loads((ROOT / 'connections.json').read_text(encoding='utf-8-sig'))
                    homes = dict(codex_homes())
                    import setup_backend
                    scope = setup_backend.load_sources()
                    for name, entry in registry['connections'].items():
                        if entry.get('sync_history') and not entry.get('history_only') and \
                                setup_backend.source_enabled(dict(tool='codex', source=name), scope):
                            report = codex_import.sync(db, name, homes,
                                                       days=entry.get('sync_days', 30),
                                                       min_turns=entry.get('sync_min_turns', 2))
                            counts = {k: len(v) if isinstance(v, list) else v for k, v in report.items()}
                            log(f'sync-codex {name} {counts}')
                    collect(db)
                    import conversation_store
                    conversation_store.sync_all(db)
                    import launch_jobs
                    launch_jobs.reconcile()
                    import handoff_lifecycle
                    handoff_lifecycle.retry_pending(db)
                    import trash
                    purged = trash.purge()
                    if purged:
                        log(f'trash purged {purged}')
            except Exception as error:
                log(f'ERROR {args.cmd} {type(error).__name__}: {error}')
                raise
            seconds = (datetime.datetime.now() - started).total_seconds()
            if args.cmd == 'collect':
                log(f'collected {changed} changed files in {seconds:.1f}s')
                return print(f'{changed} files updated in {seconds:.1f}s')
            counts = {k: (len(v) if isinstance(v, list) else v) for k, v in report.items()}
            log(f'sync-claude {counts} in {seconds:.1f}s')
            if args.quiet and not (report['new'] or report['refreshed'] or report['forked'] or report['failed']):
                return
            if args.quiet:
                return print(f"agent-history: imported {len(report['new']) + len(report['forked'])} and "
                             f"refreshed {len(report['refreshed'])} Codex sessions for /resume")
            return print(json.dumps(report, ensure_ascii=False, indent=1))
    db = connect()
    if args.cmd == 'sessions':
        for r in list_sessions(db, args.tool, args.grep, args.n, args.all):
            print(session_line(r))
    elif args.cmd == 'search':
        roles = ['user', 'assistant', 'tool_call', 'tool_output'] if args.tools else None
        for r in search(db, args.query, args.tool, args.cwd, roles, args.n, args.all):
            print(f"{(r['ts'] or '')[:16]}  {r['tool']}:{r['source']:<9} {r['sid']}  {(r['title'] or '')[:50]}")
            print(f"    {r['role']}: {r['snippet']}")
    elif args.cmd in ('show', 'export'):
        session = resolve(db, args.id)
        roles = ['user', 'assistant', 'compacted', 'agent'] if args.no_tools else None
        text = render(session, messages(db, session['key'], roles, args.head, args.tail), args.tool_chars)
        if args.cmd == 'show':
            print(text)
        else:
            Path(args.o).write_text(text, encoding='utf-8')
            print(f'wrote {args.o} ({len(text)} chars)')
    elif args.cmd == 'import-claude':
        print(json.dumps(import_claude(db, args.id, args.cwd, args.max_chars, args.detail_turns,
                                       dry_run=args.dry_run), ensure_ascii=False, indent=1))


if __name__ == '__main__':
    if sys.stdout:
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except ValueError as error:
        print(error, file=sys.stderr)
        sys.exit(1)
