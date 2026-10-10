"""Native tool identity and on-demand full details, without index truncation."""
import json
from pathlib import Path
import codex_import as ci
import agent_history as ah


def descriptors(tool, record):
    if tool == 'codex' and record.get('type') == 'response_item':
        p = record.get('payload') or {}
        kind = p.get('type', '')
        if kind in ('function_call', 'custom_tool_call', 'local_shell_call', 'web_search_call'):
            return [dict(role='tool_call', call_id=p.get('call_id') or p.get('id'),
                         name=p.get('name') or kind, block=None)]
        if kind in ('function_call_output', 'custom_tool_call_output'):
            return [dict(role='tool_output', call_id=p.get('call_id'), name='工具输出', block=None)]
    if tool == 'claude' and not record.get('isSidechain'):
        blocks = (record.get('message') or {}).get('content')
        if isinstance(blocks, list):
            return [dict(role='tool_call' if block['type'] == 'tool_use' else 'tool_output',
                         call_id=block.get('id') if block['type'] == 'tool_use' else block.get('tool_use_id'),
                         name=block.get('name') or '工具输出', block=index)
                    for index, block in enumerate(blocks) if block.get('type') in ('tool_use', 'tool_result')]
    return []


def read(locator):
    path = Path(locator['path'])
    stat = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != tuple(locator['stamp']):
        raise ValueError('工具原记录已变化，请刷新对话后展开。')
    record = next((value for ordinal, value in enumerate(ci.records(path))
                   if ordinal == locator['ordinal']), None)
    if record is None:
        raise ValueError('工具原记录已不存在。')
    if locator['tool'] == 'codex':
        p = record['payload']
        value = p.get('output') if locator['role'] == 'tool_output' else \
            p.get('arguments') or p.get('input') or p.get('action') or ''
    else:
        block = record['message']['content'][locator['block']]
        value = block.get('content', '') if locator['role'] == 'tool_output' else block.get('input', {})
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    if locator['role'] == 'tool_call' and isinstance(value, str):
        try:
            text = json.dumps(json.loads(value), ensure_ascii=False, indent=2)
        except ValueError:
            pass
    final = path.stat()
    if (final.st_size, final.st_mtime_ns) != tuple(locator['stamp']):
        raise ValueError('工具记录读取期间变化，请刷新。')
    return text


def groups(messages):
    """Pair only explicit, unambiguous IDs; retain orphan/duplicate records."""
    result, calls = {}, {}
    for index, row in enumerate(messages):
        if row['role'] not in ('tool_call', 'tool_output'):
            continue
        identity = row.get('call_id')
        if row['role'] == 'tool_call':
            result[index] = [index]
            if identity:
                calls.setdefault(identity, []).append(index)
        elif identity and len(calls.get(identity, [])) == 1:
            call = calls.pop(identity)[0]
            result[call].append(index)
            result[index] = None
        else:
            result[index] = [index]
    return result
