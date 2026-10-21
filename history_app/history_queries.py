"""History filtering and reader data preparation, independent of Tk."""
import contextlib
from pathlib import Path
import agent_history as ah
import connections
import conversation_store as conversations
import conversation_titles as titles
import ui_support as support
MESSAGE_LIMIT = 400


def review_candidates():
    """Only current candidates from the latest complete audit."""
    return support.review_snapshot()['candidates']


@connections.snapshot()
def list_rows(db, source='', query='', include_imports=False, include_internal=False, only=None, limit=500,
              project=None, sort_by='time', descending=True):
    """Filter all transcript versions and sort before applying a page limit."""
    kinds = {'main'} | ({'import'} if include_imports else set()) | \
        ({'guardian', 'subagent'} if include_internal else set())
    rows, seen = [], set()
    import setup_backend
    scope = setup_backend.load_sources()
    for raw in db.execute('''SELECT s.*, f.source AS file_source, f.path AS file_path, f.mtime AS modified
                             FROM files f JOIN sessions s ON s.key=f.session_key ORDER BY f.mtime DESC'''):
        r = dict(raw)
        r['indexed_path'] = r['path']
        r['source'], r['path'] = r.pop('file_source'), r.pop('file_path')
        if source and r['source'] != source:
            continue
        if only is not None and str(Path(r['path'])) not in only:
            continue
        if scope is not None and (r['source'] not in scope['codex'] if r['tool'] == 'codex' else
                                 str(Path(r['path']).parent.parent.parent) not in scope['claude']):
            continue
        if (r['source'], r['path']) in seen:
            continue
        seen.add((r['source'], r['path']))
        matched = False
        with contextlib.suppress(ValueError, OSError):
            r, matched = ah.physical_view(db, r, query)
        r = titles.apply(db, r)
        if only is None and r['kind'] not in kinds and not (r['kind'] == 'import' and conversations.lookup(db, r)):
            continue
        if project is not None and support.project_key(r['cwd']) != support.project_key(project):
            continue
        if query:
            text = ' '.join(str(r[k] or '') for k in ('title', 'cwd', 'id')).casefold()
            if query.casefold() not in text:
                if matched:
                    rows.append(r)
                continue
        rows.append(r)
    rows = support.sort_history(rows, sort_by, descending)
    return rows if limit is None else rows[:limit]


def project_choices(db, list_fn=None):
    """Include every indexed project, including missing directories and hidden session kinds."""
    projects = {}
    for row in (list_fn or list_rows)(db, include_imports=True, include_internal=True, limit=None):
        projects.setdefault(support.project_key(row['cwd']), row['cwd'] or '')
    return {'全部项目': None, **{cwd or '未记录目录': cwd for cwd in sorted(projects.values(), key=str.casefold)}}


def recovery_job(db, selected, fallback=None):
    """A selected conversation must never retry another conversation's job."""
    if not selected:
        return fallback
    member = conversations.lookup(db, selected)
    if not member:
        return None
    reservation = db.execute('SELECT job FROM conversation_runs WHERE conversation=?',
                             (member['conversation'],)).fetchone()
    if reservation:
        return reservation['job']
    owner = db.execute("SELECT id FROM temporary_handoffs WHERE temporary_version=? OR durable_version=? "
                       "ORDER BY (state='cleaned'), rowid DESC LIMIT 1",
                       (member['id'], member['id'])).fetchone()
    return owner['id'] if owner else None


def render_messages(rows, limit=MESSAGE_LIMIT):
    """Plain text for the viewer: tool traffic folded to one line each."""
    out = []
    shown = rows[-limit:] if limit is not None else rows
    if len(rows) > len(shown):
        out.append(('note', f'（仅显示最近 {limit} 条，共 {len(rows)} 条；可加载更早消息）\n\n'))
    for r in shown:
        role, text = r['role'], r['text'] or ''
        if role in ('tool_call', 'tool_output'):
            line = ' '.join(text.split())[:160]
            out.append(('tool', f'  ▸ {role}: {line}\n'))
            continue
        stamp = (r['ts'] or '')[:16].replace('T', ' ')
        out.append(('head_' + ('user' if role == 'user' else 'other'), f'\n{role.upper()}  {stamp}\n'))
        out.append(('body', text.rstrip() + '\n'))
    return out
