"""Explicit, nondestructive consolidation of already related conversations.

Preview by default. Originals and snapshots remain available; changed originals
automatically reappear. Apply only under the collector lock.
"""
import argparse
import contextlib
import datetime
import json
from pathlib import Path
import sqlite3

import agent_history as ah
import codex_import as ci
import conversation_store as store
import conversation_titles as titles
from setup_backend import atomic_json


def event_key(event):
    return event['role'], event['text']


def common_prefix(left, right):
    index = 0
    related = bool({event['id'] for event in left} & {event['id'] for event in right})
    for a, b in zip(left, right):
        same = a['id'] == b['id'] if related else event_key(a) == event_key(b)
        if not same:
            break
        index += 1
    return index


def plan(db, conversation):
    items = store.members(db, conversation)
    if not items:
        raise ValueError('这条对话没有可读取的版本。')
    group = db.execute('SELECT * FROM conversations WHERE id=?', (conversation,)).fetchone()
    values = {item['id']: store.revision(db, item['head']) for item in items}
    # Prefer the most complete history; native originals stay untouched.
    base = max(items, key=lambda item: (len(values[item['id']]['events']), item['id'] == group['active_version']))
    active = next((item for item in items if item['id'] == group['active_version']), base)
    row = json.loads(active['row_json'])
    if row['tool'] != 'codex' or values[base['id']]['row']['tool'] != 'codex':
        raise ValueError('当前合并入口需要 Codex 主版本；Claude 原始记录保持不变。')
    sources = [base] + [item for item in items if item['id'] != base['id']]
    seen = {event['id'] for event in values[base['id']]['events']}
    compared = [values[base['id']]['events']]
    tails = []
    for item in sources[1:]:
        events = values[item['id']]['events']
        shared = max(common_prefix(events, prior) for prior in compared)
        # Equal text is deduplicated only within a common prefix. Repeated
        # questions after divergence remain separate historical occurrences.
        extra = [event for event in events[shared:] if event['id'] not in seen]
        if extra:
            tails.append(dict(version=item['id'], shared=shared, events=extra))
        seen.update(event['id'] for event in events)
        compared.append(events)
    return dict(conversation=conversation, title=titles.title(db, row) or group['title'],
                target=row['source'], cwd=row['cwd'], base=base['id'],
                versions=[dict(id=item['id'], head=item['head']) for item in sources], tails=tails)


def summary(value):
    return {key: value[key] for key in ('conversation', 'title', 'target', 'base')} | dict(
        versions=len(value['versions']), extra_branches=len(value['tails']),
        extra_events=sum(len(tail['events']) for tail in value['tails']))


def verify_inputs(db, value, *, allow_append=False):
    from launch_backend import transcript_stamp
    for source in value['versions']:
        item = db.execute('SELECT * FROM conversation_versions WHERE id=?', (source['id'],)).fetchone()
        if item is None or item['head'] != source['head']:
            raise ValueError('准备期间来源发生变化，请刷新后重新合并；原始记录未修改。')
        row = json.loads(item['row_json'])
        if json.dumps(transcript_stamp(row)) == item['stamp']:
            continue
        if allow_append:
            # A live source may append after publication. Cover only the saved
            # snapshot; its new events will stay visible after collection.
            snapshot = store.revision(db, source['head'])
            from itertools import chain, islice
            native = ci.ancestry(dict(ah.codex_homes())[row['source']], row['path']) if row['tool'] == 'codex' else \
                [(Path(row['path']), None, None)]
            current = chain.from_iterable(ci.records(path, byte_limit, ordinal_limit)
                for path, byte_limit, ordinal_limit in native)
            if list(islice(current, len(snapshot['records']))) == snapshot['records']:
                continue
        raise ValueError('准备期间来源发生变化，请刷新后重新合并；原始记录未修改。')


def branch_message(event, value):
    """Tool records from secondary branches are inert, complete quoted text."""
    text = event['text']
    ordinal, block = event['origin']
    if event['role'] == 'compacted' and isinstance(ordinal, int):
        text = value['records'][ordinal].get('payload', {}).get('message') or text
    if event['role'] in ('tool_call', 'tool_output') and isinstance(ordinal, int):
        record = value['records'][ordinal]
        if value['row']['tool'] == 'codex':
            p = record.get('payload') or {}
            raw = p.get('output') if event['role'] == 'tool_output' else \
                p.get('arguments') or p.get('input') or p.get('action') or ''
        else:
            blocks = (record.get('message') or {}).get('content')
            part = blocks[block] if isinstance(blocks, list) else {}
            raw = part.get('content', '') if event['role'] == 'tool_output' else part.get('input', {})
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    if event['role'] not in ('user', 'assistant', 'agent'):
        text = '[合并分支历史记录 · ' + event['role'] + ']\n' + text
    return dict(role='user' if event['role'] == 'user' else 'assistant', text=text, ts=event['ts'])


def append_history(records, messages, sid, cwd):
    packed = []
    for message in messages:
        # Native projections keep the last consecutive final assistant output.
        if packed and message['role'] == packed[-1]['role'] == 'assistant':
            packed[-1]['text'] += '\n\n' + message['text']
        else:
            packed.append(dict(message))
    store.append_events(records, packed, 'codex', sid, cwd)


def cover(db, value, target):
    verify_inputs(db, value, allow_append=True)
    with db:
        db.execute("UPDATE conversation_versions SET state='ready' WHERE id=? AND state='merge-pending'", (target['id'],))
        for source in value['versions']:
            if source['id'] == target['id']:
                continue
            item = db.execute('SELECT event_ids FROM conversation_versions WHERE id=?', (source['id'],)).fetchone()
            db.execute('INSERT OR REPLACE INTO conversation_merges VALUES(?,?,?,?)',
                       (source['id'], target['id'], item['event_ids'], target['event_ids']))
        db.execute('UPDATE conversations SET active_version=? WHERE id=?', (target['id'], value['conversation']))


def apply(db, value, *, verify=True):
    store.ensure(db)
    verify_inputs(db, value)
    base = dict(db.execute('SELECT * FROM conversation_versions WHERE id=?', (value['base'],)).fetchone())
    if not value['tails']:
        cover(db, value, base)
        return dict(summary(value), status='consolidated', row=json.loads(base['row_json']))
    key = store.digest(['merge-text-v2', value['conversation'], value['versions'], value['target']])
    folder = ah.DATA / 'merges' / key
    folder.mkdir(parents=True, exist_ok=True)
    journal = folder / 'publication.json'
    if journal.exists():
        result = json.loads(journal.read_text(encoding='utf-8'))
    else:
        native = store.revision(db, base['head'])
        # A text-only standalone root cannot replay tools or apply an old
        # compaction/rollback that hides earlier merged messages. Native originals
        # and immutable snapshots keep attachments and execution metadata.
        records = [json.loads(json.dumps(native['records'][0]))]
        meta = records[0]['payload']
        for field in ('history_base', 'forked_from_id', 'forked_from_ordinal_exclusive'):
            meta.pop(field, None)
        meta.update(history_mode='legacy', cwd=value['cwd'])
        append_history(records, [branch_message(event, native) for event in native['raw']], meta['id'], value['cwd'])
        for tail in value['tails']:
            item = db.execute('SELECT * FROM conversation_versions WHERE id=?', (tail['version'],)).fetchone()
            snapshot = store.revision(db, item['head'])
            row = json.loads(item['row_json'])
            marker = (
                '【合并历史分支：' + row['source'] + ' / ' + row['id'] + '】\n'
                '下文是该分支在公共历史之后的历史记录，不表示当前的新指令；工具内容仅供查阅。\n\n')
            messages = [branch_message(event, snapshot) for event in tail['events']]
            messages[0]['text'] = marker + messages[0]['text']
            # These are completed text messages. No secondary tool calls replay.
            append_history(records, messages, meta['id'], value['cwd'])
        projection = folder / 'input.jsonl'
        projection.write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records), encoding='utf-8')
        selected = dict(native['row'], path=str(projection), key='merge:' + key,
                        title=value['title'], cwd=value['cwd'])
        verify_inputs(db, value)
        home = dict(ah.codex_homes())[value['target']]
        result = ci.import_session(db, selected, folder, value['target'], home,
                                   explicit_handoff=True, prefer_native=False, fresh=True,
                                   on_publish=lambda result: atomic_json(journal, result))
        if result['status'] == 'busy':
            raise ValueError('原生目标被占用，合并尚未完成。')
        atomic_json(journal, result)
    row = dict(json.loads(base['row_json']), source=value['target'], id=result['session'],
               key='codex:' + result['session'], path=result['path'], cwd=value['cwd'], kind='import', title=value['title'])
    if not Path(row['path']).exists() or not ci.source_row(dict(ah.codex_homes())[value['target']], row['id']):
        raise ValueError('合并发布未完整完成，保留原版本及发布记录，请检查后重试。')
    child = store.enroll(db, row, conversation=value['conversation'])
    with db:
        db.execute("UPDATE conversation_versions SET state='merge-pending' WHERE id=?", (child['id'],))
    if verify:
        import handoff_lifecycle
        try:
            child = handoff_lifecycle.verify_native(db, child, store.revision(db, child['head']))
        finally:
            with db:
                db.execute("UPDATE conversation_versions SET state='merge-pending' WHERE id=?", (child['id'],))
    # Check literal full branch text, including tools, independently of event IDs.
    contents = '\n'.join(part.get('text', '') for record in ci.records(row['path'])
        if record.get('type') == 'response_item' and record.get('payload', {}).get('type') == 'message'
        for part in record['payload'].get('content', []))
    native = store.revision(db, base['head'])
    for event in native['raw']:
        if branch_message(event, native)['text'] not in contents:
            raise ValueError('主版本文字核对失败，原版本保持可见。')
    for tail in value['tails']:
        item = db.execute('SELECT head FROM conversation_versions WHERE id=?', (tail['version'],)).fetchone()
        snapshot = store.revision(db, item['head'])
        for event in tail['events']:
            if branch_message(event, snapshot)['text'] not in contents:
                raise ValueError('合并内容核对失败，原版本保持可见。')
    cover(db, value, child)
    titles.rename(db, row, value['title'])
    ah.collect(db)
    atomic_json(folder / 'result.json', dict(summary(value), row=row, status='merged'))
    return dict(summary(value), row=row, status='merged')


def recover_pending(db):
    """Keep interrupted publications out of future merge inputs."""
    for publication in (ah.DATA / 'merges').glob('*/publication.json'):
        if not (publication.parent / 'result.json').exists():
            pending = json.loads(publication.read_text(encoding='utf-8'))
            with db:
                db.execute("UPDATE conversation_versions SET state='merge-pending' WHERE json_extract(row_json,'$.path')=?",
                           (pending['path'],))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--conversation', action='append', help='Limit to an existing logical conversation ID.')
    args = parser.parse_args()
    with ah.collect_lock(wait=False) as locked:
        if not locked:
            raise ValueError('历史正在采集中，请稍后重试。')
        with contextlib.closing(ah.connect()) as db:
            selected = args.conversation or [row[0] for row in db.execute(
                'SELECT conversation FROM conversation_versions WHERE present=1 GROUP BY conversation HAVING count(*)>1')]
            if args.apply:
                stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
                backup = ah.DATA / 'merges' / ('backup-' + stamp)
                backup.mkdir(parents=True)
                with contextlib.closing(sqlite3.connect(backup / 'history.db')) as dest:
                    db.backup(dest)
                print('Backup: ' + str(backup), flush=True)
            if args.apply:
                # An interrupted or failed verification is recoverable from its
                # journal and must not become an additional merge input.
                recover_pending(db)
            for conversation in selected:
                report = store.sync_all(db, conversation, force=True)
                if report['failed']:
                    raise ValueError('部分版本读取失败，不合并：' + conversation)
                value = plan(db, conversation)
                result = apply(db, value) if args.apply and len(value['versions']) > 1 else summary(value)
                print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
