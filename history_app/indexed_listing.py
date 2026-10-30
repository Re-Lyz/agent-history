"""Page the collected read model in SQLite, preserving physical-version identity."""
import json
import math
import os
from pathlib import Path
import sqlite3
from collections import OrderedDict, defaultdict
from functools import wraps
import threading
import connections

import conversation_store as conversations
import setup_backend
import ui_support as support
from history_app.reader_data import checkpoint

_groups = OrderedDict()
_group_lock = threading.Lock()
_versions = OrderedDict()
_DEFAULT_SCOPE = object()


def cached_groups(db, rows, cancel):
    """Cache display decisions, keyed by committed revisions and live presence.

    Native launch/cleanup validators never consume this cache.
    """
    grouped = defaultdict(list)
    for row in rows:
        grouped[db.execute('SELECT conversation FROM listing_versions WHERE identity=?',
                           (row['member_identity'],)).fetchone()[0]].append(row)
    database = db.execute('PRAGMA database_list').fetchone()[2]
    scope = json.dumps(setup_backend.load_sources(), sort_keys=True)
    for cid, candidates in grouped.items():
        checkpoint(cancel)
        revision = db.execute('SELECT revision FROM logical_clock WHERE conversation=?', (cid,)).fetchone()
        presence = []
        for item in db.execute("SELECT id,json_extract(row_json,'$.path') AS path FROM conversation_versions "
                               "WHERE conversation=? AND present=1 AND head IS NOT NULL AND state<>'merge-pending'", (cid,)):
            checkpoint(cancel)
            presence.append((item['id'], Path(item['path']).exists()))
        key = (database, cid, revision[0] if revision else 0, scope,
               tuple(sorted(row['member_identity'] for row in candidates)), tuple(sorted(presence)))
        with _group_lock:
            recipe = _groups.get(key)
        if recipe is None:
            result = conversations.group_rows(db, candidates, cancel=cancel)
            recipe = [(row['member_identity'], {key: row[key] for key in
                       ('conversation', 'version', 'branches', 'version_count', 'logical') if key in row}) for row in result]
            with _group_lock:
                _groups[key] = recipe
                _groups.move_to_end(key)
                while len(_groups) > 1024:
                    _groups.popitem(last=False)
        available = {row['member_identity']: row for row in candidates}
        for identity, extra in recipe:
            yield dict(available[identity], **extra)


def cancellable(function):
    @wraps(function)
    @connections.snapshot()
    def wrapped(db, *args, **kwargs):
        cancel = kwargs.get('cancel')
        db.set_progress_handler(lambda: int(bool(cancel and cancel())), 1000)
        try:
            return function(db, *args, **kwargs)
        except sqlite3.OperationalError:
            checkpoint(cancel)
            raise
        finally:
            db.set_progress_handler(None, 0)
    return wrapped


class HistoryPage(list):
    def __init__(self, rows=(), total=0):
        super().__init__(rows)
        self.total = total


def functions(db, scope=_DEFAULT_SCOPE, homes=None):
    scope = setup_backend.load_sources() if scope is _DEFAULT_SCOPE else scope
    roots = {}
    if homes is not None:
        for source, home in homes.items():
            values = []
            for name in ('sessions', 'archived_sessions'):
                path = Path(home) / name
                values.extend((support.project_key(str(path)), support.project_key(str(path.resolve()))))
            roots[source] = tuple(set(values))
    def enabled(tool, source, path):
        selected = scope is None or (source in scope['codex'] if tool == 'codex' else
                                     str(Path(path).parent.parent.parent) in scope['claude'])
        if not selected or tool != 'codex' or homes is None:
            return selected
        permitted = roots.get(source, ())
        def contained(value):
            key = support.project_key(str(value))
            return any(key.startswith(root + os.sep) for root in permitted)
        if contained(path):
            return True
        try:
            return contained(Path(path).resolve())
        except (OSError, ValueError):
            return False
    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None
    db.create_function('history_enabled', 3, enabled)
    db.create_function('history_fold', 1, lambda value: str(value or '').casefold(), deterministic=True)
    db.create_function('history_project', 1, support.project_key, deterministic=True)
    db.create_function('history_number', 1, number, deterministic=True)


def project_choices(db):
    functions(db)
    projects = {}
    for row in db.execute('''SELECT DISTINCT coalesce(json_extract(v.metadata,'$.cwd'),s.cwd,'') AS cwd
                            FROM files f JOIN sessions s ON s.key=f.session_key
                            LEFT JOIN file_views v ON v.path=f.path
                            WHERE history_enabled(s.tool,f.source,f.path)'''):
        projects.setdefault(support.project_key(row['cwd']), row['cwd'])
    return {'全部项目': None, **{cwd or '未记录目录': cwd for cwd in sorted(projects.values(), key=str.casefold)}}


def versions(db, cancel):
    db.execute('CREATE TEMP TABLE IF NOT EXISTS listing_versions(path TEXT,source TEXT,identity TEXT,'
               'conversation TEXT,title TEXT,PRIMARY KEY(path,source))')
    db.execute('DELETE FROM listing_versions')
    database = db.execute('PRAGMA database_list').fetchone()[2]
    for item in db.execute('SELECT DISTINCT v.conversation,coalesce(c.revision,0) AS revision,t.title '
                           'FROM conversation_versions v LEFT JOIN logical_clock c ON c.conversation=v.conversation '
                           'LEFT JOIN conversation_titles t ON t.conversation=v.conversation').fetchall():
        checkpoint(cancel)
        key = (database, item['conversation'], item['revision'])
        with _group_lock:
            records = _versions.get(key)
        if records is None:
            records = [(json.loads(raw['row_json']), raw['identity']) for raw in db.execute(
                'SELECT row_json,identity FROM conversation_versions WHERE conversation=?', (item['conversation'],))]
            with _group_lock:
                _versions[key] = records
                while len(_versions) > 1024:
                    _versions.popitem(last=False)
        projected = []
        for row, identity in records:
            checkpoint(cancel)
            # Validate current connection identity even on a revision-cache hit.
            if conversations.identity(row) == identity:
                projected.append((row['path'], row['source'], identity, item['conversation'], item['title']))
        db.executemany('INSERT OR REPLACE INTO listing_versions VALUES (?,?,?,?,?)', projected)


FIELDS = ('key', 'id', 'tool', 'source', 'path', 'cwd', 'title', 'started', 'updated', 'turns',
          'model', 'kind', 'imported_from', 'version', 'modified', 'indexed_path', 'member_identity',
          'message_count', 'view_stamp', 'conversation')


@cancellable
def page(db, source='', query='', include_imports=False, include_internal=False, only=None, limit=500,
         project=None, sort_by='time', descending=True, offset=0, logical=False, cancel=None, keep_paths=None,
         scope=_DEFAULT_SCOPE, version_metadata=True, homes=None):
    if sort_by not in ('time', 'source', 'turns', 'title') or not isinstance(descending, bool):
        raise ValueError('Invalid history sort column or direction.')
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError('Invalid history page.')
    checkpoint(cancel)
    functions(db, scope, homes)
    if version_metadata:
        versions(db, cancel)
    else:
        db.execute('CREATE TEMP TABLE IF NOT EXISTS listing_versions(path TEXT,source TEXT,identity TEXT,'
                   'conversation TEXT,title TEXT,PRIMARY KEY(path,source))')
        db.execute('DELETE FROM listing_versions')
    db.execute('CREATE TEMP TABLE IF NOT EXISTS listing_only(path TEXT PRIMARY KEY)')
    db.execute('DELETE FROM listing_only')
    if only is not None:
        db.executemany('INSERT OR IGNORE INTO listing_only VALUES (?)', [(str(Path(path)),) for path in only])
    kinds = ['main'] + (['import'] if include_imports else []) + (['guardian', 'subagent'] if include_internal else [])
    where, parameters = ['history_enabled(tool,source,path)'], []
    if source:
        where.append('source=?')
        parameters.append(source)
    if only is not None:
        where.append('path IN (SELECT path FROM listing_only)')
    else:
        where.append('(kind IN (' + ','.join('?' for _ in kinds) + ") OR (kind='import' AND member_identity IS NOT NULL))")
        parameters.extend(kinds)
    if project is not None:
        where.append('history_project(cwd)=history_project(?)')
        parameters.append(project)
    if query:
        folded = query.casefold()
        text_match = 'instr(search_text,?)>0'
        text_parameters = [folded]
        if len(folded) >= 3 and '\x00' not in folded:
            # FTS narrows candidates; instr retains exact substring semantics.
            grams = dict.fromkeys(folded[start:start + 3] for start in (0, (len(folded) - 3) // 2, len(folded) - 3))
            candidates = ' AND '.join('path IN (SELECT p.path FROM file_view_parts p JOIN file_view_parts_fts f '
                                      'ON f.rowid=p.rowid WHERE file_view_parts_fts MATCH ?)' for _ in grams)
            text_match = '((' + candidates + ') OR view_rowid NOT IN '
            text_match += '(SELECT view_rowid FROM file_view_part_coverage)) AND instr(search_text,?)>0'
            text_parameters = ['"' + gram.replace('"', '""') + '"' for gram in grams] + [folded]
        where.append('(instr(history_fold(title || \' \' || cwd || \' \' || id),?)>0 OR (' + text_match + '))')
        parameters.extend([folded, *text_parameters])
    cte = '''WITH base AS (
        SELECT s.key,s.id,s.tool,f.source,f.path,s.path AS indexed_path,f.mtime AS modified,
        coalesce(json_extract(v.metadata,'$.cwd'),s.cwd,'') AS cwd,
        coalesce(m.title,CASE WHEN s.path=f.path AND coalesce(s.title,'')<>'' THEN s.title
            ELSE coalesce(json_extract(v.metadata,'$.title'),s.title,'') END) AS title,
        coalesce(json_extract(v.metadata,'$.started'),s.started) AS started,s.updated,
        coalesce(v.turns,s.turns,0) AS turns,s.model,
        CASE WHEN s.kind='import' THEN s.kind ELSE coalesce(json_extract(v.metadata,'$.kind'),s.kind) END AS kind,
        s.imported_from,s.version,m.identity AS member_identity,m.conversation,v.rowid AS view_rowid,v.search_text,
        json_extract(v.metadata,'$.message_count') AS message_count,v.stamp AS view_stamp
        FROM files f JOIN sessions s ON s.key=f.session_key LEFT JOIN file_views v ON v.path=f.path
        LEFT JOIN listing_versions m ON m.path=f.path AND m.source=f.source),
        filtered AS (SELECT * FROM base WHERE ''' + ' AND '.join(where) + ') '
    db.execute('CREATE TEMP TABLE IF NOT EXISTS listing_grouped(path TEXT,source TEXT,row_json TEXT,'
               'PRIMARY KEY(path,source))')
    db.execute('DELETE FROM listing_grouped')
    grouped = logical and not query and not include_imports and only is None
    if grouped:
        enrolled = [dict(row) for row in db.execute(cte + 'SELECT ' + ','.join(FIELDS) +
                                                   ' FROM filtered WHERE member_identity IS NOT NULL', parameters)]
        checkpoint(cancel)
        for row in cached_groups(db, enrolled, cancel):
            db.execute('INSERT INTO listing_grouped VALUES (?,?,?)',
                       (row['path'], row['source'], json.dumps(row, ensure_ascii=False)))
    selected = '(member_identity IS NULL OR g.path IS NOT NULL)' if grouped else '1'
    joined = ' FROM filtered b LEFT JOIN listing_grouped g ON g.path=b.path AND g.source=b.source WHERE ' + selected
    order_value = ('history_number(b.modified)' if sort_by == 'time' else
                   'history_number(b.turns)' if sort_by == 'turns' else 'nullif(history_fold(b.' + sort_by + "),'')")
    order = order_value + ' IS NULL,' + order_value + (' DESC' if descending else ' ASC')
    order += ',history_fold(b.source),b.id,b.path'
    db.execute('CREATE TEMP TABLE IF NOT EXISTS listing_keep(path TEXT PRIMARY KEY)')
    db.execute('DELETE FROM listing_keep')
    if keep_paths:
        db.executemany('INSERT OR IGNORE INTO listing_keep VALUES (?)', [(path,) for path in keep_paths])
    db.set_progress_handler(lambda: int(bool(cancel and cancel())), 1000)
    try:
        total = db.execute(cte + 'SELECT count(*)' + joined, parameters).fetchone()[0]
        if keep_paths and limit is not None and offset == 0:
            last = db.execute(cte + 'SELECT max(position) FROM (SELECT b.path, row_number() OVER (ORDER BY ' +
                              order + ') AS position' + joined + ') WHERE path IN (SELECT path FROM listing_keep)',
                              parameters).fetchone()[0]
            limit = max(limit, last or 0)
        checkpoint(cancel)
        rows = db.execute(cte + 'SELECT ' + ','.join('b.' + field for field in FIELDS) +
                          ',g.row_json AS logical_json' + joined + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?',
                          [*parameters, -1 if limit is None else limit, offset]).fetchall()
        result = []
        for raw in rows:
            checkpoint(cancel)
            row = dict(raw)
            overlay = row.pop('logical_json')
            if overlay:
                row.update(json.loads(overlay))
            row.pop('member_identity', None)
            result.append(row)
        return HistoryPage(result, total)
    except sqlite3.OperationalError:
        checkpoint(cancel)
        raise
    finally:
        db.set_progress_handler(None, 0)
