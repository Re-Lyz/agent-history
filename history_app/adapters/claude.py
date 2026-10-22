"""Convert Claude text into standalone legacy Codex history; never replay tools."""
import json
import datetime
import os
from functools import lru_cache
from pathlib import Path

import codex_import as ci

VERSION = 1
TOOL_LIMIT = 4000


def omit_block(block, report):
    category = block.get('type')
    key = 'images' if category == 'image' else 'attachments' if category == 'document' else \
        'reasoning_blocks' if category in ('thinking', 'redacted_thinking') else 'unsupported_blocks'
    report[key] += 1


def tool_result_text(content, report):
    if isinstance(content, str):
        return content
    if content is None:
        return ''
    if not isinstance(content, list):
        report['unsupported_blocks'] += 1
        return '[未转换的工具结果格式]'
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
            texts.append(block['text'])
        elif isinstance(block, dict):
            omit_block(block, report)
        else:
            report['unsupported_blocks'] += 1
    return '\n'.join(texts)


def converted(path, session_id, report):
    latest, identified = {}, False
    for ordinal, record in enumerate(ci.records(path)):
        if not isinstance(record, dict):
            raise ValueError('Claude transcript contains a non-object record.')
        if record.get('type') in ('user', 'assistant') and not record.get('isSidechain') and not record.get('isMeta'):
            if record.get('uuid'):
                latest[record['uuid']] = ordinal
    for ordinal, record in enumerate(ci.records(path)):
        if not isinstance(record, dict):
            raise ValueError('Claude transcript contains a non-object record.')
        kind = record.get('type')
        if kind not in ('user', 'assistant'):
            if kind in ('summary', 'compact-boundary'):
                report['compaction_records'] += 1
            continue
        if record.get('isSidechain') or record.get('isMeta'):
            report['excluded_records'] += 1
            continue
        if record.get('sessionId') != session_id:
            raise ValueError('Claude source session ID is missing or differs from the selected session.')
        identified = True
        identity = record.get('uuid')
        if identity and latest[identity] != ordinal:
            report['duplicate_records'] += 1
            continue
        message = record.get('message') or {}
        content = message.get('content')
        blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else content or []
        if not isinstance(blocks, list):
            raise ValueError('Claude message content must be text or an array.')
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError('Claude message block must be an object.')
            category = block.get('type')
            role, value = kind, ''
            if category == 'text':
                value = block.get('text') or ''
                if not isinstance(value, str):
                    raise ValueError('Claude text block must contain a string.')
                if not value.strip():
                    continue
                report['text_blocks'] += 1
            elif category in ('tool_use', 'tool_result'):
                role = 'assistant'
                body = (str(block.get('name') or '') + ': ' + json.dumps(block.get('input'), ensure_ascii=False)) \
                    if category == 'tool_use' else tool_result_text(block.get('content'), report)
                if len(body) > TOOL_LIMIT:
                    report['truncated_tool_summaries'] += 1
                    body = body[:TOOL_LIMIT] + '\n[工具摘要已截断]'
                value = '[历史工具调用，未重放] ' + body if category == 'tool_use' else '[历史工具结果摘要] ' + body
                report['tool_summaries'] += 1
            else:
                omit_block(block, report)
                continue
            report['messages'] += 1
            yield role, value, record.get('timestamp')
    if not identified or not report['text_blocks']:
        raise ValueError('Claude source has no convertible main conversation text.')


def empty_report():
    return dict(version=VERSION, messages=0, text_blocks=0, tool_summaries=0,
                truncated_tool_summaries=0, images=0, attachments=0, reasoning_blocks=0,
                unsupported_blocks=0, compaction_records=0, excluded_records=0, duplicate_records=0,
                limitations=['只迁移文字与工具摘要；不重放工具，不迁移图片、附件、隐藏推理或执行状态。',
                             '文件路径作为原文保留；目标环境需要另行提供对应文件。',
                             '压缩边界不重建；只转换选定文件中保留的主对话记录。'])


@lru_cache(maxsize=128)
def cached_preview(path, sid, size, mtime):
    report = empty_report()
    for _ in converted(path, sid, report):
        pass
    return report


def preview(session):
    path = Path(session['path'])
    stat = path.stat()
    report = cached_preview(str(path.resolve()), session['id'], stat.st_size, stat.st_mtime_ns)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        raise ValueError('Claude source changed during conversion preview; refresh and retry.')
    return dict(report, limitations=list(report['limitations']))


def snapshot(session, output, sid, provider, model):
    report = empty_report()
    timestamp = session.get('started') or session.get('updated') or datetime.datetime.now(datetime.timezone.utc).isoformat()
    meta = dict(id=sid, timestamp=timestamp, cwd=session['cwd'], originator='agent_history',
                cli_version='0.0.0', source='cli', model_provider=provider,
                history_mode='legacy', agent_history_import=dict(source=session['key'],
                                                                conversion='claude-text-v1'))
    with output.open('w', encoding='utf-8', newline='\n') as stream:
        def emit(kind, payload, ts):
            stream.write(json.dumps(dict(type=kind, timestamp=ts or timestamp, payload=payload), ensure_ascii=False) + '\n')
        emit('session_meta', meta, timestamp)
        # Store text as completed message history, never as pending function calls.
        for role, value, ts in converted(session['path'], session['id'], report):
            emit('response_item', dict(type='message', role=role, content=[dict(
                type='input_text' if role == 'user' else 'output_text', text=value)]), ts)
            emit('event_msg', dict(type='user_message' if role == 'user' else 'agent_message', message=value), ts)
        stream.flush()
        os.fsync(stream.fileno())
    return report
