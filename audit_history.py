"""Inventory local history and generate review lists. Never delete or import sessions."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

import agent_history as ah
import codex_import as ci

UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)
SMOKE = re.compile(r'^(?:\d+|sta|test|测试|hello|hi|你好|reply only with ok\.?|只回复\s*ok)[!！.。\s]*$', re.I)
KNOWLEDGE = re.compile(r'知识[库训练]*.*(?:日期|时间|截止)|(?:训练|knowledge).*cutoff|模型身份', re.I)


def short(value, limit=160):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(value or ''))[:limit]


def topic(session, sample):
    text = ' '.join((session.get('title') or '', session.get('cwd') or '', sample)).lower()
    if SMOKE.fullmatch((session.get('title') or '').strip()) or KNOWLEDGE.search(session.get('title') or ''):
        return '短问答与连通测试'
    if re.search(r'ipf|nnunet|xuzhou|医院', text):
        return 'IPF 与医学数据处理'
    if re.search(r'vla|\bwam\b|机器人', text):
        return 'VLA / WAM 研究'
    if re.search(r'vggt|lfv|endofast|matching|self sup|icra|hamlyn|camera|集群|训练|slurm', text):
        return 'VGGT / LfV 与训练实验'
    if re.search(r'论文|paper|zotero|引用|文献|毕设|poster|ppt|\.tex|汇报|transcript', text):
        return '论文、文献与汇报'
    if re.search(r'purpur|filled_map|map-archive|mc插件|\\jvav', text):
        return 'Minecraft 插件'
    if re.search(r'codex|claude|skill|技能|powershell|wsl|网络|系统|sandbox|harness|历史|连接|权限|mac 地址', text):
        return 'Agent、技能与系统维护'
    return '其他 / 需人工确认'


def stable_hash(path, limit=None):
    path = Path(path)
    try:
        before = path.stat()
        digest = hashlib.sha256()
        remaining = before.st_size if limit is None else limit
        with path.open('rb') as stream:
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    return None
                digest.update(block)
                remaining -= len(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        return digest.hexdigest()
    except OSError:
        return None


def references(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)
    elif isinstance(value, str) and UUID.fullmatch(value):
        yield value


def genuinely_empty(path, meta, native_verified_empty=False):
    if (meta.get('history_base') or meta.get('forked_from_id')
            or (meta.get('history_mode') == 'paginated' and not native_verified_empty)):
        return False
    try:
        path = Path(path)
        before = path.stat()
        with path.open('rb') as stream:
            if before.st_size:
                stream.seek(-1, 2)
                if stream.read(1) != b'\n':
                    return False  # a recoverable partial record is not an empty session
        for record in ci.records(path):
            kind, payload = record.get('type'), record.get('payload') or {}
            if kind in ('session_meta', 'turn_context'):
                continue
            if kind == 'event_msg' and payload.get('type') in ('task_started', 'task_complete', 'turn_aborted', 'token_count'):
                continue
            if kind == 'response_item' and payload.get('type') == 'message':
                if payload.get('role') in ('developer', 'system'):
                    continue
                content = payload.get('content') or []
                if content and all(c.get('text', '').startswith(('<environment_context>', '# AGENTS.md')) for c in content):
                    continue
            return False
        after = path.stat()
        return (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    except (ValueError, OSError):
        return False


def write_csv(path, rows, columns):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            output = {}
            for name in columns:
                value = row.get(name, '')
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, ensure_ascii=False)
                if isinstance(value, str):
                    value = short(value, 2000)
                    if value.startswith(('=', '+', '-', '@')):
                        value = "'" + value
                output[name] = value
            writer.writerow(output)


def write_report(output, summary, primaries, physical):
    """Human-readable review; every proposed action remains advisory."""
    def mb(size):
        return f'{size / 1_000_000:.2f} MB'

    def cell(value):
        return short(value, 100).replace('|', '\\|').replace('<', '&lt;').replace('>', '&gt;')

    small = [p for p in primaries if p['review_reasons']]
    empty_count = len({r['key'] for r in physical if r['empty']})
    smoke_count = sum(bool(p['smoke_only']) for p in primaries)
    qa_count = sum(p['user_messages'] <= 6 and p['tool_calls'] == 0 and bool(KNOWLEDGE.search(p.get('title') or '')) for p in primaries)
    exact = [r for r in physical if r['duplicate_relation'] == 'identical']
    legacy = [r for r in exact if r['source'] == 'legacy' and not r['blockers']]
    continued = [r for r in physical if r['import_state'] == 'continued']
    logical = summary['logical_kinds']
    import_states = summary['import_states']
    import_rows = [r for r in physical if r['kind'] == 'import']
    guardian_rows = [r for r in physical if r['kind'] in ('guardian', 'subagent')]
    lines = ['# 历史对话盘点与清理建议', '', f"快照时间：{summary['created']}。**本次没有删除、归档或导入任何对话。**", '',
        '范围：注册表中的全部 Codex 连接，包括 legacy 的 sessions/archived_sessions，以及 Claude projects 下所有 JSONL。',
        '以会话元数据、统一索引的用户消息/工具统计、首尾用户消息、完整文件哈希和导入映射进行审阅；没有逐字审阅全部工具输出。备份、认证、插件和项目文件不在清理范围。', '',
        f"共 {summary['physical_files']} 个文件、{summary['logical_sessions']} 个去重会话，其中 {summary['primary_sessions']} 个主会话。会话文件大小合计 {summary['transcript_bytes'] / 1e9:.2f} GB，统一数据库 {mb(summary['database_bytes'])}。", '',
        '| 类型 | 去重会话数 | 文件数 | 文件大小合计 |', '| --- | ---: | ---: | ---: |']
    for kind, label in [('main', '主会话（含历史镜像）'), ('guardian', '自动审批'), ('subagent', '子代理'), ('import', '共享导入副本')]:
        rows = [r for r in physical if r['kind'] == kind]
        logical_count = sum(v for k, v in logical.items() if k.endswith(':' + kind))
        lines.append(f'| {label} | {logical_count} | {len(rows)} | {mb(sum(r["size"] for r in rows))} |')
    lines += ['', '## 主会话主题索引', '', '主题是基于标题、目录与首条用户消息的初步分类；详细首尾消息在 [主会话清单](primary-sessions.csv)。', '',
              '| 主题 | 主会话数 |', '| --- | ---: |']
    for name, count in Counter(p['topic'] for p in primaries).most_common():
        lines.append(f'| {name} | {count} |')
    lines += ['', '## 优先审阅的短会话', '',
        f'优先建议：{empty_count} 个确认空会话、{smoke_count} 个无工具调用的连通/输入测试。另有 {qa_count} 个知识截止日期短问答可按账号保留代表。',
        f'这些候选共 {len(small)} 个主会话、{sum(p["physical_files"] for p in small)} 个文件，文件大小 {mb(sum(p["physical_bytes"] for p in small))}；收益主要是列表整洁。保护条件需逐项查看。下表是审阅建议，不是自动删除授权。', '',
        '| 来源 | 会话 ID | 标题 / 首条用户消息 | 建议理由 | 文件数 |', '| --- | --- | --- | --- | ---: |']
    for p in sorted(small, key=lambda p: (p['source'], p['started'] or '')):
        lines.append(f'| {cell(p["source"])} | `{p["id"]}` | {cell(p["title"] or p["first_user"] or "（空）")} | '
                     f'{cell("；".join(p["review_reasons"]))} | {p["physical_files"]} |')
    lines += ['', '## 重复镜像：先核对恢复入口，再归档', '',
        f"{summary['same_id_groups']} 个 ID 存在跨目录副本。确认 {len(exact)} 个额外文件与较长/选定版本逐字节相同，合计 {mb(sum(r['size'] for r in exact))}；另有 {summary['duplicates'].get('prefix', 0)} 个较短前缀快照。",
        f"其中 legacy 下有 {len([r for r in exact if r['source'] == 'legacy'])} 个完全重复文件；本轮未检测到近期修改、元数据父线程依赖或同 ID 差异保护条件的有 {len(legacy)} 个，合计 {mb(sum(r['size'] for r in legacy))}。",
        '如果不再通过 legacy 恢复旧会话，这批可作为下一轮归档候选。其他连接中的相同文件仍承担各账号的恢复入口；直接删除会减少该连接的原生 /resume 历史。',
        '“未检测到保护条件”不等于保证没有外部引用。正式清理前应复查原生状态与正在打开的线程，并同时维护原生索引和统一索引。完整 SHA-256 和保留位置见 [全文件清单](all-files.csv)。', '',
        '## 应保留的内容', '',
        f"- {summary['divergent_id_groups']} 组同 ID 文件存在非前缀差异，保留全部版本。包括 self sup、训练检查等科研会话；不能按 ID 或文件大小直接去重。",
        '- VGGT/LfV、自监督、camera/ICRA、IPF 医学数据、VLA/WAM、论文写作和系统配置的主会话保留。较老或只有一轮不作为删除理由。',
        '- 已检测到父线程/原生 fork 关系、近期修改、继承历史、导入后修改或缺失原来源的文件均标出保护条件。',
        f"- {import_states.get('codex:unchanged_verified', 0)} 个 Codex 共享副本哈希与导入映射一致；Claude {logical.get('claude:import', 0)} 个副本中 {import_states.get('claude:no_continuation_detected', 0)} 个未检测到续聊，{len(continued)} 个已续聊。共享副本合计 {mb(sum(r['size'] for r in import_rows))}，支撑跨连接 /resume。只有不再使用某个连接的共享历史时才考虑减少副本。", '',
        '| 已续聊的 Claude 副本 | 会话 ID |', '| --- | --- |']
    for r in continued:
        lines.append(f'| {cell(r["title"])} | `{r["id"]}` |')
    lines += ['', '## 审批、子代理和数据库', '',
        f"自动审批 {logical.get('codex:guardian', 0)} 个去重会话、子代理 {logical.get('codex:subagent', 0)} 个去重会话，文件大小合计 {mb(sum(r['size'] for r in guardian_rows))}。默认检索列表已经隐藏它们；完成的旧任务记录可按父任务归档，近期及科研排障记录先保留。不要把它们的内部用户消息当成人工开启的主对话。",
        f"统一数据库当前空闲页只有约 {mb(summary['reclaimable_database_pages_bytes'])}，单纯压缩收益有限。数据库还保存导入映射和用户删除副本的记忆，不建议直接删除重建。", '',
        '## 文件清单', '',
        '- [主会话清单](primary-sessions.csv)：主题、来源、完整 ID、标题、目录、首尾用户消息。',
        '- [全文件清单](all-files.csv)：全部原路径、大小、哈希、重复保留位置和保护条件。',
        '- [审阅候选](review-candidates.csv)：包含空/测试记录、镜像、审批/子代理和共享副本；它不是可直接删除的列表。',
        '- [机器可读完整证据](inventory.json) 与 [汇总](summary.json)。', '',
        '分类可能重叠，空间不能简单相加。以上是文件逻辑大小，不是实际可释放磁盘空间承诺；运行中的会话可能继续变化。']
    if (output / 'empty-native-verification.json').is_file():
        lines += ['', '两条空会话另经 Codex 原生 `thread/turns/list` 完整分页读取核实：没有历史 items、没有下一页；未 resume 或调用模型。[原生读取证据](empty-native-verification.json)。']
    (output / 'review.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def audit(output, refresh=False):
    if refresh:
        with ah.collect_lock(wait=True) as locked:
            if not locked:
                raise ValueError('Collector is busy; try again later.')
            db = ah.connect()
            try:
                print(f'Refreshed catalog only: {ah.collect(db)} changed files.', flush=True)
            finally:
                db.close()
    db = ci.readonly(ah.DATA / 'history.db')
    try:
        db.execute('BEGIN')
        sessions = {r['key']: dict(r) for r in db.execute('SELECT * FROM sessions')}
        files = {r['path']: dict(r) for r in db.execute('SELECT * FROM files')}
        native = {r['dst_key']: dict(r) for r in db.execute('SELECT * FROM codex_imports')}
        claude = {r['dst_key']: dict(r) for r in db.execute('SELECT * FROM imports')}
        counts = defaultdict(Counter)
        for r in db.execute('SELECT session_key,role,count(*) n FROM messages GROUP BY session_key,role'):
            counts[r['session_key']][r['role']] = r['n']
        primaries = []
        for key, session in sessions.items():
            if session['kind'] != 'main':
                continue
            users = [r[0] for r in db.execute("SELECT text FROM messages WHERE session_key=? AND role='user' ORDER BY seq LIMIT 2", (key,))]
            last = db.execute("SELECT text FROM messages WHERE session_key=? AND role='user' ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
            row = dict(session, first_user=short(users[0] if users else ''), last_user=short(last[0] if last else ''),
                       user_messages=counts[key]['user'], tool_calls=counts[key]['tool_call'])
            row['smoke_only'] = (0 < row['user_messages'] <= 2 and row['tool_calls'] == 0
                                 and all(SMOKE.fullmatch(user.strip()) for user in users))
            row['topic'] = topic(row, row['first_user'])
            primaries.append(row)
        pages = {name: db.execute('PRAGMA ' + name).fetchone()[0] for name in ('page_size', 'page_count', 'freelist_count')}
    finally:
        db.close()
    now = time.time()
    homes = dict(ah.codex_homes())
    physical, protected, errors = [], set(), []
    for source, home in homes.items():
        for folder in ('sessions', 'archived_sessions'):
            for path in (home / folder).rglob('*.jsonl'):
                try:
                    stat = path.stat()
                    meta = ci.first_meta(path)
                    sid = meta.get('id') or meta.get('session_id')
                    key = 'codex:' + str(sid)
                    session = sessions.get(key, {})
                    parsed = next(ah.parse_codex({'type': 'session_meta', 'payload': meta}))[1]
                    refs = sorted(set(references(meta)) - {sid})
                    protected.update((source, ref) for ref in refs)
                    physical.append(dict(path=str(path), tool='codex', source=source, id=sid, key=key,
                        size=stat.st_size, mtime=stat.st_mtime, modified=dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
                        active_24h=now - stat.st_mtime < 86400, kind=parsed['kind'], title=short(session.get('title')),
                        cwd=meta.get('cwd') or session.get('cwd'), user_messages=counts[key]['user'],
                        tool_calls=counts[key]['tool_call'], refs=refs, inherited=bool(meta.get('history_base') or meta.get('forked_from_id')),
                        empty=counts[key]['user'] == 0 and counts[key]['tool_call'] == 0 and genuinely_empty(path, meta)))
                except (OSError, ValueError, KeyError, StopIteration) as error:
                    errors.append({'path': str(path), 'error': str(error)})
    for home in ah.CLAUDE_HOMES:
        for path in (home / 'projects').rglob('*.jsonl'):
            try:
                stat = path.stat()
                key = 'claude:' + path.stem
                session = sessions.get(key, {})
                physical.append(dict(path=str(path), tool='claude', source='claude', id=path.stem, key=key,
                    size=stat.st_size, mtime=stat.st_mtime, modified=dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
                    active_24h=now - stat.st_mtime < 86400, kind=session.get('kind', 'subagent' if 'subagents' in path.parts else 'unindexed'),
                    title=short(session.get('title')), cwd=session.get('cwd'), user_messages=counts[key]['user'],
                    tool_calls=counts[key]['tool_call'], refs=[], inherited=False, empty=False))
            except OSError as error:
                errors.append({'path': str(path), 'error': str(error)})
    print(f'Inventoried {len(physical)} files; hashing transcripts to distinguish duplicates and continued copies.', flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        hashes = dict(zip((r['path'] for r in physical), pool.map(stable_hash, (r['path'] for r in physical))))
    groups = defaultdict(list)
    for row in physical:
        row['sha256'] = hashes[row['path']]
        row['protected_parent'] = (row['source'], row['id']) in protected
        row['catalogued'] = row['path'] in files
        row['duplicate_relation'], row['canonical_path'] = '', ''
        row['divergent_id'] = False
        row['import_state'], row['original_exists'] = '', ''
        if row['tool'] == 'codex':
            groups[row['id']].append(row)
        ledger = (native if row['tool'] == 'codex' else claude).get(row['key'])
        if ledger:
            row['original_exists'] = ledger['src_key'] in sessions
            row['import_source'] = ledger['src_key']
            if row['tool'] == 'codex':
                row['import_state'] = ('unchanged_verified' if row['sha256'] and row['sha256'] == ledger['dst_hash']
                                       else 'modified_or_busy')
            else:
                row['import_state'] = ('continued' if ah.continued_in_claude(row['path']) else
                                       'no_continuation_detected' if row['size'] == ledger['dst_size'] else 'modified_without_continuation')
    duplicate_groups = []
    for sid, rows in groups.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: (r['size'], r['path'] == sessions.get(r['key'], {}).get('path')), reverse=True)
        canonical = rows[0]
        canonical['duplicate_relation'] = 'canonical'
        for row in rows[1:]:
            row['canonical_path'] = canonical['path']
            if row['sha256'] and row['sha256'] == canonical['sha256']:
                row['duplicate_relation'] = 'identical'
            elif row['size'] < canonical['size'] and row['sha256'] and row['sha256'] == stable_hash(canonical['path'], row['size']):
                row['duplicate_relation'] = 'prefix'
            else:
                row['duplicate_relation'] = 'different_or_busy'
        if any(r['duplicate_relation'] == 'different_or_busy' for r in rows):
            for row in rows:
                row['divergent_id'] = True
        duplicate_groups.append({'id': sid, 'files': [dict(r) for r in rows]})
    primary_by_key = {r['key']: r for r in primaries}
    for row in physical:
        reasons = []
        primary = primary_by_key.get(row['key'])
        if row['empty']:
            reasons.append('空会话（原记录未发现对话、工具或继承历史）')
        if primary and primary['smoke_only']:
            reasons.append('连通/输入测试（无工具调用；需人工确认）')
        if primary and primary['user_messages'] <= 6 and primary['tool_calls'] == 0 and KNOWLEDGE.search(primary.get('title') or ''):
            reasons.append('重复的知识截止日期/模型身份短问答（建议按账号保留代表）')
        if row['kind'] in ('guardian', 'subagent'):
            reasons.append('自动审批/子代理记录（可先归档，需保留任务和审计关系）')
        if row['duplicate_relation'] == 'identical':
            reasons.append('逐字节相同的跨目录副本（删除会影响该连接直接恢复）')
        if row['duplicate_relation'] == 'prefix':
            reasons.append('同 ID 的较短前缀快照（较长版本在其他目录；不直接删除）')
        if row['kind'] == 'import' and row['import_state'] in ('unchanged_verified', 'no_continuation_detected'):
            reasons.append('未检测到续聊的共享副本（共享功能所需，仅不再使用该连接时考虑清理）')
        blockers = []
        if row['active_24h']:
            blockers.append('24 小时内修改：本轮保留')
        if row['protected_parent'] or row['inherited']:
            blockers.append('涉及原生 fork/父线程依赖：保留')
        if not row['sha256']:
            blockers.append('读取期间变化或不可读：保留')
        if row['divergent_id']:
            blockers.append('同 ID 内容存在差异：保留全部版本')
        if row['import_state'] in ('modified_or_busy', 'continued', 'modified_without_continuation'):
            blockers.append('导入后发生修改/续聊：保留')
        if row['kind'] == 'import' and not row['original_exists']:
            blockers.append('未找到原会话：保留现存副本')
        row['review_reasons'], row['blockers'] = reasons, blockers
        row['recommendation'] = '保留（保护条件）' if blockers else '可审阅候选' if reasons else '保留'
    for primary in primaries:
        copies = [r for r in physical if r['key'] == primary['key']]
        primary['physical_files'] = len(copies)
        primary['physical_bytes'] = sum(r['size'] for r in copies)
        primary['review_reasons'] = sorted({reason for r in copies for reason in r['review_reasons'] if not reason.startswith(('逐字节', '同 ID'))})
        primary['blockers'] = sorted({reason for r in copies for reason in r['blockers']})
    candidates = [r for r in physical if r['review_reasons']]
    summary = dict(created=dt.datetime.now().astimezone().isoformat(), refreshed=refresh,
        logical_sessions=len(sessions), primary_sessions=len(primaries), physical_files=len(physical),
        transcript_bytes=sum(r['size'] for r in physical), by_kind=dict(Counter(r['kind'] for r in physical)),
        logical_kinds=dict(Counter(r['tool'] + ':' + r['kind'] for r in sessions.values())),
        same_id_groups=len(duplicate_groups), extra_same_id_files=sum(len(g['files']) - 1 for g in duplicate_groups),
        divergent_id_groups=sum(any(r['divergent_id'] for r in g['files']) for g in duplicate_groups),
        duplicates=dict(Counter(r['duplicate_relation'] for r in physical if r['duplicate_relation'])),
        candidate_files=len(candidates), blocked_candidates=sum(bool(r['blockers']) for r in candidates),
        import_states=dict(Counter(r['tool'] + ':' + r['import_state'] for r in physical if r['import_state'])),
        database_bytes=(ah.DATA / 'history.db').stat().st_size, reclaimable_database_pages_bytes=pages['page_size'] * pages['freelist_count'],
        unstable_hashes=sum(not r['sha256'] for r in physical), scan_errors=errors,
        scope='Registered Codex sessions/archived_sessions and Claude projects recursively; backups, credentials and unrelated projects excluded.')
    output.mkdir(parents=True, exist_ok=False)
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'inventory.json').write_text(json.dumps({'summary': summary, 'primary_sessions': primaries, 'files': physical,
                                                    'duplicate_groups': duplicate_groups}, ensure_ascii=False, indent=2), encoding='utf-8')
    columns = ['tool', 'source', 'kind', 'id', 'title', 'cwd', 'user_messages', 'tool_calls', 'size', 'modified',
               'recommendation', 'review_reasons', 'blockers', 'duplicate_relation', 'canonical_path',
               'import_state', 'original_exists', 'sha256', 'path']
    write_csv(output / 'all-files.csv', physical, columns)
    write_csv(output / 'review-candidates.csv', candidates, columns)
    write_csv(output / 'primary-sessions.csv', primaries, ['topic', 'tool', 'source', 'id', 'title', 'cwd', 'user_messages',
              'tool_calls', 'started', 'updated', 'physical_files', 'physical_bytes', 'first_user', 'last_user', 'review_reasons', 'blockers'])
    write_report(output, summary, primaries, physical)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print('Reports: ' + str(output), flush=True)
    return summary


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true', help='refresh the search catalog only; never synchronize/import')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    destination = args.output or ah.DATA / 'audits' / dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    audit(destination, args.refresh)
