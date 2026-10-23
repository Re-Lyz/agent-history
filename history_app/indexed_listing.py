"""Page the collected read model in SQLite, preserving physical-version identity."""
import json
import math
from pathlib import Path
import sqlite3

import conversation_store as conversations
import setup_backend
import ui_support as support
from history_app.reader_data import checkpoint


class HistoryPage(list):
    def __init__(self, rows=(), total=0):
        super().__init__(rows)
        self.total = total


def functions(db):
    scope = setup_backend.load_sources()
    def enabled(tool, source, path):
        return scope is None or (source in scope['codex'] if tool == 'codex' else
                                 str(Path(path).parent.parent.parent) in scope['claude'])
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
    for item in db.execute('SELECT v.row_json,v.identity,v.conversation,t.title FROM conversation_versions v LEFT JOIN conversation_titles t '
                           'ON t.conversation=v.conversation').fetchall():
        checkpoint(cancel)
        row = json.loads(item['row_json'])
        # A path or native ID alone never establishes conversation membership.
        if conversations.identity(row) == item['identity']:
            db.execute('INSERT OR REPLACE INTO listing_versions VALUES (?,?,?,?,?)',
                       (row['path'], row['source'], item['identity'], item['conversation'], item['title']))


FIELDS = ('key', 'id', 'tool', 'source', 'path', 'cwd', 'title', 'started', 'updated', 'turns',
          'model', 'kind', 'imported_from', 'version', 'modified', 'indexed_path', 'member_identity',
          'message_count', 'view_stamp')


def page(db, source='', query='', include_imports=False, include_internal=False, only=None, limit=500,
         project=None, sort_by='time', descending=True, offset=0, logical=False, cancel=None, keep_paths=None):
    if sort_by not in ('time', 'source', 'turns', 'title') or not isinstance(descending, bool):
        raise ValueError('Invalid history sort column or direction.')
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError('Invalid history page.')
    checkpoint(cancel)
    functions(db)
    versions(db, cancel)
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
            text_match = '(view_rowid IN (SELECT rowid FROM file_views_fts WHERE file_views_fts MATCH ?) '
            text_match += 'OR view_rowid NOT IN (SELECT view_rowid FROM file_view_search_coverage)) AND instr(search_text,?)>0'
            grams = dict.fromkeys(folded[start:start + 3] for start in (0, (len(folded) - 3) // 2, len(folded) - 3))
            text_parameters.insert(0, ' AND '.join('"' + gram.replace('"', '""') + '"' for gram in grams))
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
        s.imported_from,s.version,m.identity AS member_identity,v.rowid AS view_rowid,v.search_text,
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
        for row in conversations.group_rows(db, enrolled):
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
