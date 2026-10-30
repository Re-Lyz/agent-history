"""User titles belong to logical conversations; native transcripts stay untouched."""
import re
import datetime
import json
import uuid
import agent_history as ah
import conversation_store as store

SCHEMA = 'CREATE TABLE IF NOT EXISTS conversation_titles(conversation TEXT PRIMARY KEY, title TEXT NOT NULL);'


def display(row, sources=()):
    """Hide legacy account prefixes without changing stored/native titles."""
    value = row.get('title') or ''
    accounts = {str(name).casefold() for name in sources}
    accounts.add(str(row.get('source') or '').casefold())
    while match := re.match(r'^\s*\[([^\[\]\r\n]+)\]\s*', value):
        label = match[1].casefold()
        if label not in accounts and not re.fullmatch(r'openai\d+', label):
            break
        value = value[match.end():]
    return value.strip()


def title(db, row):
    member = store.lookup(db, row)
    if member:
        saved = db.execute('SELECT title FROM conversation_titles WHERE conversation=?',
                           (member['conversation'],)).fetchone()
        if saved:
            return saved['title']
    return row.get('title') or ''


def apply(db, row):
    return dict(row, title=title(db, row))


def rename(db, row, value):
    if not isinstance(value, str) or len(value.strip()) > 200 or '\n' in value or '\r' in value:
        raise ValueError('名称应为不超过 200 字的单行文字。')
    member = store.lookup(db, row) or store.enroll(db, row)
    with db:
        if value.strip():
            db.execute('INSERT OR REPLACE INTO conversation_titles VALUES (?,?)',
                       (member['conversation'], value.strip()))
        else:
            db.execute('DELETE FROM conversation_titles WHERE conversation=?', (member['conversation'],))
    return title(db, row)


def rename_and_sync(db, row, value):
    """Save the logical name and report independent native-name sync results."""
    from setup_backend import atomic_json
    from history_app import native_titles
    previous = title(db, row)
    # Validate before creating journals or changing either title store.
    if not isinstance(value, str) or len(value.strip()) > 200 or '\n' in value or '\r' in value:
        raise ValueError('名称应为不超过 200 字的单行文字。')
    identity = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex
    path = ah.DATA / 'maintenance' / 'title-updates' / (identity + '.json')
    report = dict(id=identity, previous_title=previous, requested_title=value.strip(),
                  native=[], state='prepared', journal=str(path))
    def save():
        atomic_json(path, report)
    save()
    rename(db, row, value)
    report['state'] = 'local-saved'
    save()
    member = store.lookup(db, row)
    rows = [row]
    if member:
        rows.extend(json.loads(item['row_json']) for item in store.members(db, member['conversation'], include_merged=True))
    seen = set()
    for candidate in rows:
        key = (candidate['tool'], candidate['source'], candidate['id'], candidate['path'])
        if key in seen or candidate['tool'] != 'codex':
            continue
        seen.add(key)
        native_titles.update(candidate, value, report, save)
    synced = {item['path'] for item in report['native'] if item['state'] == 'synced'}
    if synced:
        try:
            indexed = ah.collect(db, only_paths=synced)
            report['index_pending'] = len(getattr(indexed, 'failed', []))
        except Exception as error:
            report['index_pending'] = len(synced)
            report['index_error'] = str(error)
    report['state'] = 'partial' if report.get('index_pending') or any(
        item['state'] != 'synced' for item in report['native']) else 'completed'
    save()
    return report
