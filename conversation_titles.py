"""User titles belong to logical conversations; native transcripts stay untouched."""
import conversation_store as store

SCHEMA = 'CREATE TABLE IF NOT EXISTS conversation_titles(conversation TEXT PRIMARY KEY, title TEXT NOT NULL);'


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
