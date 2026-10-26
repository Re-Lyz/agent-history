"""Project/account workspace and attributed context, using the existing history index."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import connections
import ui_support
from . import accounts, indexed_listing
from .reader_data import file_message_page, checkpoint
from .locks import file_lock


def scope(paths):
    try:
        return json.loads((paths.data / 'sources.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


@contextmanager
def database(paths):
    file = paths.data / 'history.db'
    if not file.exists():
        yield None
        return
    # mode=ro protects the main DB; existing listing code uses private TEMP tables.
    db = sqlite3.connect(file.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def jobs(paths):
    rows = []
    for file in (paths.data / 'launch-jobs').glob('*.json'):
        try:
            with file_lock(file.with_suffix('.lock'), wait=False) as locked:
                if not locked:
                    rows.append(dict(id=file.stem, state='busy', cwd='', target='unknown'))
                    continue
                value = json.loads(file.read_text(encoding='utf-8'))
            rows.append({key: value.get(key) for key in
                         ('id', 'state', 'target', 'cwd', 'session', 'updated', 'sync_state', 'exit_code')})
        except (OSError, ValueError, AttributeError):
            rows.append(dict(id=file.stem, state='unreadable', cwd='', target='unknown'))
    return rows


def snapshot(paths, *, limit=50, offset=0):
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError('项目分页参数无效。')
    report = dict(version=1, accounts=accounts.registrations(paths), projects=[], jobs=jobs(paths), total=0)
    for row in report['accounts']:
        row['quota'] = accounts.quota(paths, row['id'])
        related = [job for job in report['jobs'] if job['target'] == row['id']]
        row['tasks'] = len(related)
        row['active_tasks'] = sum(job['state'] in ('prepared', 'running', 'busy') for job in related)
        row['last_used'] = max((job.get('updated') or '' for job in related), default='')
    with database(paths) as db:
        if db is None:
            report['state'] = 'not-indexed'
            return report
        indexed_listing.functions(db, scope(paths), connections.homes(paths.shared))
        groups = {}
        for row in db.execute('''SELECT f.source,s.tool,coalesce(json_extract(v.metadata,'$.cwd'),s.cwd,'') AS cwd,
                               count(*) AS versions,max(f.mtime) AS modified
                               FROM files f JOIN sessions s ON s.key=f.session_key
                               LEFT JOIN file_views v ON v.path=f.path
                               WHERE history_enabled(s.tool,f.source,f.path)
                               GROUP BY coalesce(json_extract(v.metadata,'$.cwd'),s.cwd,''),f.source,s.tool'''):
            key = ui_support.project_key(row['cwd'])
            project = groups.setdefault(key, dict(cwd=row['cwd'], versions=0, sources=[], modified=0))
            project['versions'] += row['versions']
            project['sources'].append(dict(source=row['source'], tool=row['tool'], versions=row['versions']))
            project['modified'] = max(project['modified'], row['modified'] or 0)
        for project in groups.values():
            project['tasks'] = [job for job in report['jobs'] if ui_support.project_key(job['cwd']) == ui_support.project_key(project['cwd'])]
        values = sorted(groups.values(), key=lambda row: row['modified'], reverse=True)
        report.update(state='indexed', total=len(values), projects=values[offset:offset + limit])
    return report


def worktrees(project):
    """Inspect Git metadata on explicit selection; never run hooks or create worktrees."""
    try:
        env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
        base = ['git', '--no-pager', '-C', str(project)]
        result = subprocess.run([*base, 'worktree', 'list', '--porcelain', '-z'], env=env,
                                capture_output=True, timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            return dict(state='not-a-repository', items=[])
        rows, item = [], {}
        for field in result.stdout.decode('utf-8', 'replace').split('\0'):
            if not field:
                if item:
                    rows.append(item)
                    item = {}
            else:
                key, _, value = field.partition(' ')
                if key in ('worktree', 'HEAD', 'branch', 'detached', 'bare'):
                    item[key] = value
        if item:
            rows.append(item)
        return dict(state='ok', items=rows)
    except (OSError, subprocess.SubprocessError):
        return dict(state='unavailable', items=[])


def context(paths, project, query='', *, limit=10, offset=0, cursor=None, cancel=None):
    """Extract source material, never execute historical instructions or call a model.

    File candidates use the viewer's exact substring FTS search. Native message
    pages retain physical file/byte provenance and bounded ancestor expansion.
    """
    if not project or not isinstance(query, str) or not 1 <= limit <= 50 or offset < 0:
        raise ValueError('请指定项目目录及有效分页范围。')
    result = dict(version=1, generated_at=datetime.now(timezone.utc).isoformat(), project=project,
                  query=query, materials=[], unavailable=[], next_cursor=None, total_files=0,
                  boundary='历史摘录，可能已经过时；不表示当前决定或待执行任务。未调用模型。')
    if cursor is not None:
        if cursor.get('project') != project or cursor.get('query') != query:
            raise ValueError('上下文游标不属于当前项目或查询。')
        offset = int(cursor['offset'])
        if offset < 0:
            raise ValueError('上下文游标无效。')
    homes = connections.homes(paths.shared)
    with database(paths) as db:
        if db is None:
            result['state'] = 'not-indexed'
            return result
        page = indexed_listing.page(db, project=project, query=query, limit=limit, offset=offset,
                                    include_imports=True, scope=scope(paths), version_metadata=False, cancel=cancel, homes=homes)
        result['total_files'] = page.total
        for index, row in enumerate(page):
            checkpoint(cancel)
            material_start = len(result['materials'])
            native_cursor = cursor.get('message_cursor') if cursor and index == 0 else None
            if native_cursor is not None:
                stat = Path(row['path']).stat()
                if cursor.get('path') != row['path'] or cursor.get('stamp') != [stat.st_size, stat.st_mtime_ns]:
                    raise ValueError('上下文来源在分页后变化，请重新提取。')
            found = False
            try:
                for _ in range(100 if query else 1):
                    events = file_message_page(row, limit=16, cursor=native_cursor, cancel=cancel, homes=homes, provenance=True)
                    for event in events:
                        if event['role'] not in ('user', 'assistant', 'agent', 'compacted'):
                            continue
                        text = event['text'] or ''
                        at = text.casefold().find(query.casefold()) if query else 0
                        if at < 0:
                            continue
                        start = max(0, at - 200)
                        excerpt = text[start:start + 1200]
                        locator = event['locator']
                        stat = Path(locator['path']).stat()
                        if (stat.st_size, stat.st_mtime_ns) != tuple(locator['stamp']):
                            raise ValueError('来源已变化，请刷新后重试。')
                        result['materials'].append(dict(tool=row['tool'], source=row['source'], session_id=row['id'],
                            path=locator['path'], session_path=row['path'], title=row['title'], timestamp=event.get('ts'), role=event['role'],
                            locator=locator, excerpt=excerpt, truncated=start > 0 or start + 1200 < len(text)))
                        found = True
                    native_cursor = events.cursor if events.has_more else None
                    if found or native_cursor is None:
                        break
                if query and native_cursor is not None:
                    stat = Path(row['path']).stat()
                    result['next_cursor'] = dict(project=project, query=query, offset=offset + index,
                                                 message_cursor=native_cursor, path=row['path'], stamp=[stat.st_size, stat.st_mtime_ns])
                    break
            except (OSError, ValueError, KeyError) as error:
                del result['materials'][material_start:]
                result['unavailable'].append(dict(path=row['path'], session_id=row['id'], reason=type(error).__name__))
        if result['next_cursor'] is None and offset + len(page) < page.total:
            result['next_cursor'] = dict(project=project, query=query, offset=offset + len(page), message_cursor=None)
        result['state'] = 'ok'
    return result


def render_context(report):
    lines = [f"项目：{report['project']}", f"提取时间：{report['generated_at']}", report['boundary'], '']
    for index, item in enumerate(report['materials'], 1):
        locator = item['locator']
        lines.extend([f"{index}. [{item['source']}] 会话：{item['session_id']} · {item['role']}",
                      f"时间：{item['timestamp'] or '未记录'} · {item['title']}",
                      f"来源文件：{item['path']} · 字节：{locator['offset']} · 记录内事件：{locator['event']}",
                      item['excerpt'] + ('\n（摘录已截断）' if item['truncated'] else ''), ''])
    for item in report['unavailable']:
        lines.append('来源不可读：' + item['path'] + ' · ' + item['reason'])
    if not report['materials']:
        lines.append('本页没有匹配材料。')
    if report['next_cursor']:
        lines.append('还有更早材料，可点击“更多上下文”。')
    return '\n'.join(lines)
