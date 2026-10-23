"""Rebuildable search fragments and append checkpoints; never authorize native cleanup."""
import json
from pathlib import Path

REQUIRED_TABLES = frozenset(('file_views', 'file_view_parts', 'file_views_fts', 'file_view_parts_fts',
    'file_view_search_coverage', 'file_view_part_coverage', 'file_view_incremental', 'index_failures', 'logical_clock'))
REQUIRED_TRIGGERS = frozenset(('file_view_parts_ai', 'file_view_parts_ad', 'file_views_bi', 'file_views_ai',
    'file_views_ad', 'file_views_au', 'file_views_legacy_au', 'logical_versions_ai', 'logical_versions_au',
    'logical_versions_ad', 'logical_active_au', 'logical_merges_ai', 'logical_merges_au', 'logical_merges_ad'))


def ready(db):
    """An early v5 prototype had the same number but a different FTS contract.

    Check the actual read-model objects before permitting a cached-list query.
    This never reads transcript data or changes logical conversation records.
    """
    objects = {(row['type'], row['name']): row['sql'] for row in db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','trigger')")}
    if any(('table', name) not in objects for name in REQUIRED_TABLES) or \
            any(('trigger', name) not in objects for name in REQUIRED_TRIGGERS):
        return False
    for name, content in (('file_views_fts', 'file_views'), ('file_view_parts_fts', 'file_view_parts')):
        sql = ''.join((objects['table', name] or '').lower().split()).replace('"', "'")
        if "content='" + content + "'" not in sql:
            return False
    return True

SCHEMA = '''
DROP TRIGGER IF EXISTS file_views_bi;
DROP TRIGGER IF EXISTS file_views_ai;
DROP TRIGGER IF EXISTS file_views_ad;
DROP TRIGGER IF EXISTS file_views_au;
DROP TRIGGER IF EXISTS file_views_legacy_au;
DROP TRIGGER IF EXISTS file_view_parts_ai;
DROP TRIGGER IF EXISTS file_view_parts_ad;
DROP TABLE IF EXISTS file_views_fts;
DROP TABLE IF EXISTS file_view_parts_fts;
CREATE TABLE IF NOT EXISTS file_view_parts(rowid INTEGER PRIMARY KEY,path TEXT NOT NULL,search_text TEXT NOT NULL);
DELETE FROM file_view_parts;
CREATE INDEX IF NOT EXISTS file_view_parts_path ON file_view_parts(path);
CREATE VIRTUAL TABLE file_views_fts USING fts5(search_text,
    content='file_views',content_rowid='rowid',tokenize='trigram',detail='none');
CREATE VIRTUAL TABLE file_view_parts_fts USING fts5(search_text,
    content='file_view_parts',content_rowid='rowid',tokenize='trigram',detail='none');
CREATE TABLE IF NOT EXISTS file_view_search_coverage(view_rowid INTEGER PRIMARY KEY);
DELETE FROM file_view_search_coverage;
CREATE TABLE IF NOT EXISTS file_view_part_coverage(view_rowid INTEGER PRIMARY KEY);
DELETE FROM file_view_part_coverage;
CREATE TABLE IF NOT EXISTS file_view_incremental(path TEXT PRIMARY KEY,offset INTEGER,hash TEXT,
    publishing INTEGER NOT NULL DEFAULT 0);
DELETE FROM file_view_incremental;
CREATE TABLE IF NOT EXISTS index_failures(path TEXT PRIMARY KEY,stage TEXT NOT NULL,error TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS logical_clock(conversation TEXT PRIMARY KEY,revision INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS logical_versions_ai AFTER INSERT ON conversation_versions BEGIN
    INSERT INTO logical_clock VALUES(new.conversation,1) ON CONFLICT(conversation) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS logical_versions_au AFTER UPDATE ON conversation_versions BEGIN
    INSERT INTO logical_clock VALUES(old.conversation,1) ON CONFLICT(conversation) DO UPDATE SET revision=revision+1;
    INSERT INTO logical_clock VALUES(new.conversation,1) ON CONFLICT(conversation) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS logical_versions_ad AFTER DELETE ON conversation_versions BEGIN
    INSERT INTO logical_clock VALUES(old.conversation,1) ON CONFLICT(conversation) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS logical_active_au AFTER UPDATE ON conversations BEGIN
    INSERT INTO logical_clock VALUES(new.id,1) ON CONFLICT(conversation) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS logical_merges_ai AFTER INSERT ON conversation_merges BEGIN
    INSERT INTO logical_clock SELECT conversation,1 FROM conversation_versions WHERE id=new.source_version
    ON CONFLICT(conversation) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS logical_merges_au AFTER UPDATE ON conversation_merges BEGIN
    INSERT INTO logical_clock SELECT conversation,1 FROM conversation_versions WHERE id=new.source_version
    ON CONFLICT(conversation) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS logical_merges_ad AFTER DELETE ON conversation_merges BEGIN
    INSERT INTO logical_clock SELECT conversation,1 FROM conversation_versions WHERE id=old.source_version
    ON CONFLICT(conversation) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS file_view_parts_ai AFTER INSERT ON file_view_parts BEGIN
    INSERT INTO file_view_parts_fts(rowid,search_text) VALUES(new.rowid,new.search_text); END;
CREATE TRIGGER IF NOT EXISTS file_view_parts_ad AFTER DELETE ON file_view_parts BEGIN
    INSERT INTO file_view_parts_fts(file_view_parts_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text); END;
CREATE TRIGGER file_views_bi BEFORE INSERT ON file_views BEGIN
    INSERT INTO file_views_fts(file_views_fts,rowid,search_text)
        SELECT 'delete',v.rowid,v.search_text FROM file_views v
        JOIN file_view_search_coverage c ON c.view_rowid=v.rowid WHERE v.path=new.path;
    DELETE FROM file_view_parts WHERE path=new.path;
    DELETE FROM file_view_incremental WHERE path=new.path;
    DELETE FROM file_view_search_coverage WHERE view_rowid IN (SELECT rowid FROM file_views WHERE path=new.path);
    DELETE FROM file_view_part_coverage WHERE view_rowid IN (SELECT rowid FROM file_views WHERE path=new.path); END;
CREATE TRIGGER file_views_ai AFTER INSERT ON file_views BEGIN
    INSERT INTO file_view_parts(path,search_text) VALUES(new.path,new.search_text);
    INSERT INTO file_view_part_coverage VALUES(new.rowid) ON CONFLICT(view_rowid) DO NOTHING; END;
CREATE TRIGGER file_views_ad AFTER DELETE ON file_views BEGIN
    INSERT INTO file_views_fts(file_views_fts,rowid,search_text)
        SELECT 'delete',old.rowid,old.search_text WHERE EXISTS
        (SELECT 1 FROM file_view_search_coverage WHERE view_rowid=old.rowid);
    DELETE FROM file_view_parts WHERE path=old.path;
    DELETE FROM file_view_incremental WHERE path=old.path;
    DELETE FROM file_view_search_coverage WHERE view_rowid=old.rowid;
    DELETE FROM file_view_part_coverage WHERE view_rowid=old.rowid; END;
CREATE TRIGGER file_views_legacy_au AFTER UPDATE ON file_views BEGIN
    INSERT INTO file_views_fts(file_views_fts,rowid,search_text)
        SELECT 'delete',old.rowid,old.search_text WHERE EXISTS
        (SELECT 1 FROM file_view_search_coverage WHERE view_rowid=old.rowid);
    DELETE FROM file_view_search_coverage WHERE view_rowid=old.rowid; END;
CREATE TRIGGER file_views_au AFTER UPDATE ON file_views
    WHEN NOT EXISTS (SELECT 1 FROM file_view_incremental WHERE path=new.path AND publishing=1) BEGIN
    DELETE FROM file_view_parts WHERE path=old.path;
    DELETE FROM file_view_incremental WHERE path=old.path;
    INSERT INTO file_view_parts(path,search_text) VALUES(new.path,new.search_text);
    INSERT INTO file_view_part_coverage VALUES(new.rowid) ON CONFLICT(view_rowid) DO NOTHING; END;
'''


def append(db, row, value, stamp, previous):
    """Only use a checkpoint whose complete native prefix was just verified."""
    import agent_history as ah
    from history_app.reader_data import message_events
    state = db.execute('SELECT * FROM file_view_incremental WHERE path=?', (row['path'],)).fetchone()
    cached = db.execute('SELECT metadata FROM file_views WHERE path=?', (row['path'],)).fetchone()
    if not state or not cached or not previous or (state['offset'], state['hash']) != previous:
        return False
    metadata = json.loads(cached['metadata'])
    if 'message_count' not in metadata:
        return False
    messages = []
    for offset, record in ah.read_new_lines(Path(row['path']), state['offset']):
        if record is not None:
            messages.extend(message_events(row['tool'], row['path'], record, None, offset=offset))
    import launch_backend
    if json.dumps(launch_backend.transcript_stamp(row)) != stamp:
        raise ValueError('会话在读取过程中变化，请稍后重试索引。')
    text = '\n'.join(message['text'] for message in messages).casefold()
    prefix = '\n' if metadata['message_count'] and messages else ''
    metadata.update({name: value.get(name) for name in ('cwd', 'title', 'kind', 'started')})
    metadata['message_count'] += len(messages)
    with db:
        db.execute('UPDATE file_view_incremental SET publishing=1 WHERE path=?', (row['path'],))
        if messages:
            # Two previous characters cover every trigram across fragment joins.
            db.execute('INSERT INTO file_view_parts(path,search_text) '
                       'SELECT path,substr(search_text,-2) || ? FROM file_views WHERE path=?',
                       (prefix + text, row['path']))
        db.execute('UPDATE file_views SET stamp=?,metadata=?,turns=turns+?,search_text=search_text || ? WHERE path=?',
                   (stamp, json.dumps(metadata, ensure_ascii=False), sum(m['role'] == 'user' for m in messages),
                    prefix + text, row['path']))
        checkpoint(db, row)
    return True


def checkpoint(db, row):
    import agent_history as ah
    if row['tool'] == 'codex':
        chain = ah.codex_import.ancestry(dict(ah.codex_homes())[row['source']], row['path'])
        if len(chain) != 1 or chain[0][1:] != (None, None):
            return
    current = db.execute('SELECT f.offset,h.hash FROM files f JOIN file_fingerprints h ON h.path=f.path WHERE f.path=?',
                         (row['path'],)).fetchone()
    if current:
        db.execute('INSERT INTO file_view_incremental VALUES (?,?,?,0) ON CONFLICT(path) DO UPDATE SET '
                   'offset=excluded.offset,hash=excluded.hash,publishing=0', (row['path'], current['offset'], current['hash']))
