"""Immutable conversation revisions and explicit cross-Agent handoffs.

Original transcripts are never written. Native baselines let a return handoff
reuse original platform context and convert only the intervening new events.
"""
import contextlib
import datetime
import hashlib
import gzip
from functools import lru_cache
import json
import os
from pathlib import Path
import uuid

import agent_history as ah
import connections
import codex_import as ci
from setup_backend import atomic_json, load_sources, source_enabled

SCHEMA = '''
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, title TEXT, active_version TEXT);
CREATE TABLE IF NOT EXISTS conversation_versions(
 id TEXT PRIMARY KEY, conversation TEXT NOT NULL, identity TEXT UNIQUE NOT NULL,
 row_json TEXT NOT NULL, parent_revision TEXT, baseline TEXT, head TEXT,
 present INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'ready', stamp TEXT, event_ids TEXT);
CREATE TABLE IF NOT EXISTS conversation_revisions(
 id TEXT PRIMARY KEY, version TEXT NOT NULL, parent TEXT, snapshot TEXT NOT NULL,
 native_hash TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handoff_publications(
 identity TEXT PRIMARY KEY, source_revision TEXT NOT NULL, target TEXT NOT NULL,
 version TEXT NOT NULL, native_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready');
CREATE INDEX IF NOT EXISTS conversation_members ON conversation_versions(conversation);
CREATE TABLE IF NOT EXISTS conversation_merges(
 source_version TEXT NOT NULL, target_version TEXT NOT NULL,
 source_events TEXT NOT NULL, target_events TEXT NOT NULL,
 PRIMARY KEY(source_version,target_version));
CREATE TABLE IF NOT EXISTS handoff_intents(
 identity TEXT PRIMARY KEY, source_version TEXT NOT NULL, source_revision TEXT NOT NULL,
 target TEXT NOT NULL, cwd TEXT NOT NULL, row_json TEXT, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS temporary_handoffs(
 id TEXT PRIMARY KEY, source_version TEXT NOT NULL, source_revision TEXT NOT NULL,
 origin_version TEXT NOT NULL, origin_revision TEXT NOT NULL,
 temporary_version TEXT UNIQUE, target TEXT NOT NULL, cwd TEXT NOT NULL,
 state TEXT NOT NULL, final_revision TEXT, durable_version TEXT,
 cleanup_path TEXT, error TEXT NOT NULL DEFAULT '', temporary_row TEXT, durable_row TEXT,
 policy TEXT NOT NULL DEFAULT 'legacy');
CREATE TABLE IF NOT EXISTS conversation_runs(
 conversation TEXT PRIMARY KEY, job TEXT UNIQUE NOT NULL, version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_retirements(
 job TEXT NOT NULL, version TEXT NOT NULL, snapshot TEXT NOT NULL, saved TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'planned', PRIMARY KEY(job,version));
'''


def ensure(db):
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_runs(
        conversation TEXT PRIMARY KEY, job TEXT UNIQUE NOT NULL, version TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_retirements(
        job TEXT NOT NULL, version TEXT NOT NULL, snapshot TEXT NOT NULL, saved TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'planned', PRIMARY KEY(job,version))''')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_merges(
        source_version TEXT NOT NULL, target_version TEXT NOT NULL,
        source_events TEXT NOT NULL, target_events TEXT NOT NULL,
        PRIMARY KEY(source_version,target_version))''')
    columns = {column[1] for column in db.execute('PRAGMA table_info(conversation_versions)')}
    if not columns:
        db.executescript(SCHEMA)
        return
    for column in ('stamp', 'event_ids'):
        if column not in columns:
            db.execute('ALTER TABLE conversation_versions ADD COLUMN ' + column + ' TEXT')
    handoff_columns = {column[1] for column in db.execute('PRAGMA table_info(temporary_handoffs)')}
    if handoff_columns and 'policy' not in handoff_columns:
        db.execute("ALTER TABLE temporary_handoffs ADD COLUMN policy TEXT NOT NULL DEFAULT 'legacy'")
    db.commit()


def begin_intent(db, source, target, cwd, reused=False, request_id=None):
    key = digest([source['head'], target, cwd, request_id]) if request_id else digest([source['head'], target, cwd])
    with db:
        db.execute('INSERT OR IGNORE INTO handoff_intents VALUES (?,?,?,?,?,?,?)',
                   (key, source['id'], source['head'], target, cwd, None, 'preparing'))
        if not reused:
            db.execute("UPDATE handoff_intents SET state='preparing',row_json=NULL WHERE identity=? AND state='registered'", (key,))
    return key


def record_publication(db, key, row):
    with db:
        db.execute('UPDATE handoff_intents SET row_json=?,state=? WHERE identity=?',
                   (json.dumps(dict(row), ensure_ascii=False), 'published', key))


def recover_intents(db):
    for intent in db.execute("SELECT * FROM handoff_intents WHERE state='published'").fetchall():
        row = json.loads(intent['row_json'])
        if not source_enabled(row, load_sources()):
            continue
        if not Path(row['path']).exists():
            continue
        source = dict(db.execute('SELECT * FROM conversation_versions WHERE id=?', (intent['source_version'],)).fetchone())
        if not source_enabled(json.loads(source['row_json']), load_sources()):
            continue
        source['head'] = intent['source_revision']
        register_target(db, source, row, intent['target'], intent['cwd'])
        with db:
            db.execute("UPDATE handoff_intents SET state='registered' WHERE identity=?", (intent['identity'],))


def backfill(db):
    """Associate legacy imports conservatively; ambiguous same-ID sources stay separate."""
    from launch_backend import physical_row
    mappings = list(db.execute('SELECT dst_key,src_key,dst_path FROM imports')) + \
        list(db.execute('SELECT dst_key,src_key,dst_path FROM codex_imports'))
    for mapping in mappings:
        destination = db.execute('SELECT * FROM sessions WHERE key=?', (mapping['dst_key'],)).fetchone()
        if not destination or not Path(mapping['dst_path']).is_file():
            continue
        row = dict(destination, path=mapping['dst_path'])
        if not source_enabled(row, load_sources()):
            continue
        if lookup(db, row):
            continue
        source_files = db.execute('SELECT * FROM files WHERE session_key=?', (mapping['src_key'],)).fetchall()
        source = db.execute('SELECT * FROM sessions WHERE key=?', (mapping['src_key'],)).fetchone()
        paths = {file['path']: file for file in source_files if Path(file['path']).exists()}
        if source is None or len(paths) != 1:
            continue
        file = next(iter(paths.values()))
        original = dict(source, source=file['source'], path=file['path'])
        if not source_enabled(original, load_sources()):
            continue
        try:
            parent = enroll(db, physical_row(original))
            # Legacy mapping lacks the source revision at import time. Group
            # versions but retain their own histories rather than invent a
            # baseline that could discard a continuation or duplicate old turns.
            enroll(db, physical_row(row), conversation=parent['conversation'])
        except (ValueError, OSError):
            continue


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def identity(row):
    home = dict(ah.codex_homes()).get(row['source']) if row['tool'] == 'codex' else Path(row['path']).parent.parent
    return digest([row['tool'], row['source'], str(home), row['id'], os.path.normcase(str(Path(row['path']).resolve()))])


def lookup(db, row):
    return db.execute('SELECT * FROM conversation_versions WHERE identity=?', (identity(row),)).fetchone()


def revision(db, rid):
    item = db.execute('SELECT * FROM conversation_revisions WHERE id=?', (rid,)).fetchone()
    if not item:
        raise ValueError('接续快照缺失，请重新检查历史来源。')
    return read_snapshot(item['snapshot'])


@lru_cache(maxsize=4)
def read_snapshot(filename):
    path = Path(filename)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        value = json.load(stream)
    base = value.pop('base', None)
    if base:
        parent = path.parent / (base + '.json.gz')
        if not parent.exists():
            parent = path.parent / (base + '.json')
        previous = read_snapshot(str(parent))
        for key in ('records', 'raw', 'events'):
            value[key] = previous[key] + value[key]
    return value


def write_snapshot(path, value, previous=None):
    encoded = dict(value, depth=0)
    arrays = ('records', 'raw', 'events')
    if previous and previous.get('depth', 0) < 31 and all(prefix(previous[key], value[key]) for key in arrays):
        encoded.update(base=previous['id'], depth=previous.get('depth', 0) + 1)
        for key in arrays:
            encoded[key] = value[key][len(previous[key]):]
    atomic_json(path, encoded)


def read_native(row):
    from launch_backend import transcript_stamp
    stamp = transcript_stamp(row)
    if row['tool'] == 'codex':
        home = dict(ah.codex_homes())[row['source']]
        chain = ci.ancestry(home, row['path'])
    else:
        chain = [(Path(row['path']), None, None)]
    records = [item for file, byte_limit, ordinal_limit in chain
               for item in ci.records(file, byte_limit, ordinal_limit)]
    events = []
    latest = {record['uuid']: index for index, record in enumerate(records)
              if row['tool'] == 'claude' and record.get('uuid') and record.get('type') in ('user', 'assistant')}
    for ordinal, record in enumerate(records):
        if row['tool'] == 'claude' and (record.get('isSidechain') or record.get('isMeta')):
            continue
        if row['tool'] == 'claude' and record.get('uuid') and latest.get(record['uuid'], ordinal) != ordinal:
            continue
        for block, event in enumerate(ah.PARSERS[row['tool']](record)):
            if event[0] != 'msg':
                continue
            events.append(dict(role=event[1], text=event[2], ts=event[3],
                origin=[record.get('uuid') or ordinal, block]))
    if transcript_stamp(row) != stamp:
        raise ValueError('会话正在变化，请稍后重新采集。')
    return records, events


def prefix(left, right):
    return len(left) <= len(right) and left == right[:len(left)]


def observe(db, version, force=True):
    item = dict(version)
    row = json.loads(item['row_json'])
    if not Path(row['path']).is_file():
        if item['state'] == 'retired':
            return None
        db.execute('UPDATE conversation_versions SET present=0,state=? WHERE id=?', ('missing', item['id']))
        db.commit()
        return None
    from launch_backend import transcript_stamp
    stamp = json.dumps(transcript_stamp(row))
    if not force and item['head'] and item.get('stamp') == stamp:
        return item['head']
    records, raw = read_native(row)
    native_hash = digest(records)
    previous = revision(db, item['head']) if item['head'] else None
    if previous and previous['native_hash'] == native_hash:
        db.execute("UPDATE conversation_versions SET present=1,state=?,stamp=?,event_ids=? WHERE id=?",
                   (previous['state'], stamp, json.dumps([event['id'] for event in previous['events']]), item['id']))
        db.commit()
        return item['head']
    baseline = revision(db, item['baseline']) if item['baseline'] else None
    parent = revision(db, item['parent_revision']) if item['parent_revision'] else None
    state = 'ready'
    if baseline and parent:
        if prefix(baseline['raw'], raw):
            extra = raw[len(baseline['raw']):]
            canonical = list(parent['events']) + [dict(event, id=digest([item['id'], event])) for event in extra]
        else:
            # Rewrites/compaction are a new revision, never append the full
            # converted history onto its own parent or overwrite another branch.
            state = 'rewritten'
            canonical = [dict(event, id=digest([item['id'], event])) for event in raw]
    elif parent and not baseline:
        canonical = parent['events']
    else:
        canonical = [dict(event, id=digest([item['id'], event])) for event in raw]
        if previous and not prefix(previous['raw'], raw):
            state = 'rewritten'
    if previous and not prefix(previous['raw'], raw):
        state = 'rewritten'
    rid = digest([item['id'], native_hash, item['head'], item['parent_revision']])
    snapshot = ah.DATA / 'handoffs' / 'revisions' / (rid + '.json.gz')
    value = dict(id=rid, version=item['id'], row=row, records=records, raw=raw,
                 events=canonical, native_hash=native_hash, state=state,
                 parent=item['head'] or item['parent_revision'])
    write_snapshot(snapshot, value, previous)
    with db:
        db.execute('INSERT OR IGNORE INTO conversation_revisions VALUES (?,?,?,?,?,?)',
                   (rid, item['id'], value['parent'], str(snapshot), native_hash,
                    datetime.datetime.now(datetime.timezone.utc).isoformat()))
        db.execute('UPDATE conversation_versions SET head=?,baseline=COALESCE(baseline,?),present=1,state=?,stamp=?,event_ids=? WHERE id=?',
                   (rid, rid, state, stamp, json.dumps([event['id'] for event in canonical]), item['id']))
    return rid


def enroll(db, row, parent_revision=None, conversation=None):
    ensure(db)
    existing = lookup(db, row)
    if existing:
        observe(db, existing)
        return dict(db.execute('SELECT * FROM conversation_versions WHERE id=?', (existing['id'],)).fetchone())
    vid = uuid.uuid4().hex
    conversation = conversation or uuid.uuid4().hex
    with db:
        db.execute('INSERT OR IGNORE INTO conversations VALUES (?,?,?)', (conversation, row.get('title'), vid))
        db.execute('INSERT INTO conversation_versions(id,conversation,identity,row_json,parent_revision) VALUES (?,?,?,?,?)',
                   (vid, conversation, identity(row), json.dumps(dict(row), ensure_ascii=False), parent_revision))
    item = db.execute('SELECT * FROM conversation_versions WHERE id=?', (vid,)).fetchone()
    observe(db, item)
    return dict(db.execute('SELECT * FROM conversation_versions WHERE id=?', (vid,)).fetchone())


@connections.snapshot()
def sync_all(db, conversation=None, force=False):
    ensure(db)
    recover_intents(db)
    backfill(db)
    report = dict(updated=0, missing=0, failed=[], skipped=[])
    scope = load_sources()
    query = 'SELECT * FROM conversation_versions' + (' WHERE conversation=?' if conversation else '')
    for item in db.execute(query, (conversation,) if conversation else ()).fetchall():
        if item['state'] == 'merge-pending':
            continue  # Only the merge verifier may publish this staged version.
        if not source_enabled(json.loads(item['row_json']), scope):
            report['skipped'].append(json.loads(item['row_json'])['path'])
            continue
        try:
            head = observe(db, item, force=force)
            report['updated'] += bool(head and head != item['head'])
            report['missing'] += head is None
        except (ValueError, OSError) as error:
            report['failed'].append(dict(version=item['id'], path=json.loads(item['row_json'])['path'], error=str(error)))
    return report


def members(db, conversation, include_merged=False, *, _items=None):
    items = _items
    if items is None:
        scope = load_sources()
        items = []
        for raw in db.execute("SELECT * FROM conversation_versions WHERE conversation=? AND present=1 AND head IS NOT NULL AND state<>'merge-pending'", (conversation,)):
            row = json.loads(raw['row_json'])
            if source_enabled(row, scope) and Path(row['path']).exists():
                items.append(dict(raw))
    if include_merged:
        return items
    # Old versions stay on disk. Only hide the exact content included in a
    # surviving merge; new traffic or a rewritten/deleted merge restores them.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_merges'").fetchone():
        return items
    merges = db.execute('SELECT m.* FROM conversation_merges m JOIN conversation_versions v '
                        'ON v.id=m.source_version WHERE v.conversation=?', (conversation,)).fetchall()
    if not merges:
        return items
    signatures = {item['id']: json.loads(item['event_ids']) if item.get('event_ids') else
                  [event['id'] for event in revision(db, item['head'])['events']] for item in items}
    covered = set()
    for merge in merges:
        source, target = merge['source_version'], merge['target_version']
        if source in signatures and target in signatures and signatures[source] == json.loads(merge['source_events']) and \
                prefix(json.loads(merge['target_events']), signatures[target]):
            covered.add(source)
    return [item for item in items if item['id'] not in covered]


def tips(db, conversation, *, _items=None):
    items = members(db, conversation) if _items is None else _items
    signatures = {item['id']: json.loads(item['event_ids']) if item['event_ids'] else
                  [event['id'] for event in revision(db, item['head'])['events']] for item in items if item['head']}
    active = db.execute('SELECT active_version FROM conversations WHERE id=?', (conversation,)).fetchone()[0]
    result = []
    for item in items:
        signature = signatures.get(item['id'], [])
        if any(other != item['id'] and len(signatures[other]) > len(signature) and prefix(signature, signatures[other])
               for other in signatures):
            continue
        equal = next((prior for prior in result if signatures[prior['id']] == signature), None)
        if equal:
            if item['id'] == active:
                result.remove(equal)
            else:
                continue
        result.append(item)
    return result


def activate(db, item):
    with db:
        db.execute('UPDATE conversations SET active_version=? WHERE id=?', (item['id'], item['conversation']))


def native_ancestor(db, head, tool):
    seen = set()
    while head and head not in seen:
        seen.add(head)
        value = revision(db, head)
        if value['row']['tool'] == tool:
            return value
        head = value['parent']
    return None


def append_events(records, events, tool, sid, cwd):
    """Convert only completed textual traffic; never emit executable tool calls."""
    if tool == 'claude':
        parent = next((record.get('uuid') for record in reversed(records) if record.get('uuid')), None)
        for event in events:
            if event['role'] in ('compacted', 'note'):
                continue
            role = 'user' if event['role'] == 'user' else 'assistant'
            text = event['text'] if event['role'] in ('user', 'assistant', 'agent') else \
                '[跨平台工具摘要]\n' + ah.clip(event['text'], 4000)
            uid = str(uuid.uuid4())
            message = dict(role=role, content=text if role == 'user' else [dict(type='text', text=text)])
            if role == 'assistant':
                message.update(id='msg_handoff_' + uuid.uuid4().hex, type='message', model='<synthetic>',
                               stop_reason='end_turn', stop_sequence=None, usage=dict(input_tokens=0, output_tokens=0))
            records.append(dict(type=role, sessionId=sid, uuid=uid, parentUuid=parent,
                                timestamp=event['ts'] or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                cwd=cwd, version='2.1.0', isSidechain=False, message=message))
            parent = uid
    else:
        for event in events:
            if event['role'] in ('compacted', 'note'):
                continue
            role = 'user' if event['role'] == 'user' else 'assistant'
            text = event['text'] if event['role'] in ('user', 'assistant', 'agent') else \
                '[跨平台工具摘要]\n' + ah.clip(event['text'], 4000)
            records.append(dict(type='response_item', timestamp=event['ts'], payload=dict(type='message', role=role,
                content=[dict(type='input_text' if role == 'user' else 'output_text', text=text)])))
            records.append(dict(type='event_msg', timestamp=event['ts'], payload=dict(
                type='user_message' if role == 'user' else 'agent_message', message=text)))
    return records


def projection(db, source, tool, cwd):
    value = revision(db, source['head'])
    anchor = native_ancestor(db, source['head'], tool)
    if anchor is None or anchor['id'] == value['id']:
        return None
    anchor_ids = [event['id'] for event in anchor['events']]
    if not prefix(anchor_ids, [event['id'] for event in value['events']]):
        # An explicit rewritten branch can still be transferred using its current
        # native text, but must not be spliced onto an incompatible old anchor.
        return None
    records = json.loads(json.dumps(anchor['records']))
    sid = anchor['row']['id']
    if tool == 'codex':
        records = [record for index, record in enumerate(records) if record.get('type') != 'session_meta' or index == 0]
        records[0]['payload'].update(id=sid, cwd=cwd)
        records[0]['payload'].pop('history_base', None)
        records[0]['payload']['history_mode'] = 'legacy'
    append_events(records, value['events'][len(anchor_ids):], tool, sid, cwd)
    key = digest([source['head'], tool, cwd])
    path = ah.DATA / 'handoffs' / 'projections' / (key + '.jsonl')
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        body = ''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records)
        staged = path.with_suffix('.tmp')
        staged.write_text(body, encoding='utf-8')
        os.replace(staged, path)
    return dict(anchor['row'], path=str(path), key=json.loads(source['row_json'])['key'], cwd=cwd)


def register_target(db, source, row, target, cwd):
    existing = lookup(db, row)
    if existing and not existing['parent_revision'] and existing['conversation'] == source['conversation'] and \
            row.get('kind') == 'import':
        # A conservatively backfilled untouched import can acquire an exact
        # baseline after the adapter has explicitly verified/reused its snapshot.
        with db:
            db.execute('UPDATE conversation_versions SET parent_revision=?,baseline=NULL,head=NULL WHERE id=?',
                       (source['head'], existing['id']))
    child = enroll(db, row, parent_revision=source['head'], conversation=source['conversation'])
    pub = digest([source['head'], target, cwd])
    value = revision(db, child['head'])
    with db:
        db.execute('INSERT OR REPLACE INTO handoff_publications VALUES (?,?,?,?,?,?)',
                   (pub, source['head'], target, child['id'], value['native_hash'], 'ready'))
    activate(db, child)
    return child


def reuse(db, source, target, cwd):
    pub = db.execute('SELECT * FROM handoff_publications WHERE identity=?',
                     (digest([source['head'], target, cwd]),)).fetchone()
    if not pub:
        return None
    item = db.execute('SELECT * FROM conversation_versions WHERE id=?', (pub['version'],)).fetchone()
    if not item or not Path(json.loads(item['row_json'])['path']).exists():
        raise ValueError('目标版本已删除，请先在回收站恢复。')
    observe(db, item)
    item = dict(db.execute('SELECT * FROM conversation_versions WHERE id=?', (pub['version'],)).fetchone())
    if revision(db, item['head'])['native_hash'] != pub['native_hash']:
        return None
    row = json.loads(item['row_json'])
    if row['tool'] == 'codex':
        with ci.writer_lock(dict(ah.codex_homes())[row['source']], row['id']) as locked:
            if not locked:
                raise ValueError('目标版本正在使用，请关闭对应会话后重试。')
    activate(db, item)
    return row


def publish_claude(db, source, projected, cwd, intent=None, on_publish=None):
    records, _ = read_native(projected)
    prior = db.execute('SELECT row_json FROM handoff_intents WHERE identity=?', (intent,)).fetchone() if intent else None
    sid = json.loads(prior['row_json'])['id'] if prior and prior['row_json'] else str(uuid.uuid4())
    for record in records:
        if record.get('sessionId'):
            record['sessionId'] = sid
        if record.get('cwd') is not None:
            record['cwd'] = cwd
    path = ah.claude_project_dir(cwd) / (sid + '.jsonl')
    row = json.loads(source['row_json'])
    target_row = dict(row, tool='claude', source='claude', key='claude:' + sid, id=sid,
                      path=str(path), cwd=cwd, kind='import')
    if path.exists():
        raise ValueError('接续目标已存在，等待恢复发布任务后重试；不会覆盖。')
    # Record the intended native identity before publication so an interrupted
    # write can be recovered without generating another unrelated Claude UUID.
    if intent:
        record_publication(db, intent, target_row)
    if on_publish:
        on_publish(target_row)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix('.tmp')
    staged.write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in records), encoding='utf-8')
    os.replace(staged, path)
    with db:
        db.execute('INSERT INTO imports VALUES (?,?,?,?,?,?)', ('claude:' + sid, row['key'], str(path),
            datetime.datetime.now(datetime.timezone.utc).isoformat(), row.get('updated'), path.stat().st_size))
    return target_row


@connections.snapshot()
def group_rows(db, rows):
    """Group only explicitly related versions; preserve unrelated same-ID files."""
    ensure(db)
    result, seen = [], set()
    identified = [(identity(row), row) for row in rows]
    available = dict(identified)
    # Resolve each physical identity once and fetch membership in bounded
    # batches. Keep the full identity: equal native IDs can be separate files.
    physical_ids = list(available)
    enrolled = {}
    for offset in range(0, len(physical_ids), 500):
        batch = physical_ids[offset:offset + 500]
        placeholders = ','.join('?' for _ in batch)
        enrolled.update((item['identity'], item) for item in db.execute(
            'SELECT * FROM conversation_versions WHERE identity IN (' + placeholders + ')', batch))
    for physical_id, row in identified:
        item = enrolled.get(physical_id)
        if not item or not item['head']:
            result.append(row)
            continue
        cid = item['conversation']
        if cid in seen:
            continue
        seen.add(cid)
        all_members = members(db, cid, include_merged=True)
        visible_members = members(db, cid, _items=all_members)
        choices = [member for member in all_members if member['identity'] in available]
        heads = tips(db, cid, _items=visible_members)
        candidates = [head for head in heads if head['identity'] in available]
        active = db.execute('SELECT active_version FROM conversations WHERE id=?', (cid,)).fetchone()[0]
        selected = next((head for head in candidates if head['id'] == active), None) or \
            next(iter(candidates), None) or next(iter(choices), None)
        if selected:
            value = dict(available[selected['identity']])
            value.update(conversation=cid, version=selected['id'], branches=len(heads),
                         version_count=len(visible_members), logical=True)
            result.append(value)
    return result
