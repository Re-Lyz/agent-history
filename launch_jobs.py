"""Persistent, credential-free terminal lifecycle reports, independent of the UI."""
import datetime
import json
import os
from pathlib import Path
import uuid

import agent_history as ah
from setup_backend import atomic_json


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def path(identity):
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
        raise ValueError('Invalid launch job ID.')
    return ah.DATA / 'launch-jobs' / (identity + '.json')


def create(row, prepared, target, identity=None):
    identity = identity or uuid.uuid4().hex
    row = row or dict(path='', tool=prepared.get('tool', 'custom'), id='')
    atomic_json(path(identity), dict(id=identity, version=1, state='prepared', created=now(), updated=now(),
        target=target, source_path=row['path'], source_tool=row['tool'], source_id=row['id'],
        session=prepared['session'], cwd=prepared['cwd'], kind=prepared['kind'], attempts=0,
        sync_state='pending', error='', preparation_pid=os.getpid(),
        history_roots=prepared.get('history_roots', [])))
    return identity


def read(identity, *, wait=True):
    # A Windows reader must release its handle before atomic replacement.
    with ah.file_lock(path(identity).with_suffix('.lock'), wait=wait) as locked:
        if not locked:
            raise TimeoutError('Launch report is busy.')
        return _read(identity)


def _read(identity):
    return json.loads(path(identity).read_text(encoding='utf-8'))


def update(identity, expected_state=None, **fields):
    with ah.file_lock(path(identity).with_suffix('.lock'), wait=True) as locked:
        if not locked:
            raise TimeoutError('Launch report is busy.')
        item = _read(identity)
        if expected_state is None or item['state'] == expected_state:
            item.update(fields, updated=now())
            atomic_json(path(identity), item)
        return item


def reports(limit=25):
    result = []
    for file in sorted((ah.DATA / 'launch-jobs').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            result.append(read(file.stem))
        except (OSError, ValueError):
            continue
    return result


def describe(item):
    labels = dict(prepared='已准备，等待终端启动', running='终端命令正在运行',
                  exited='终端命令已结束', failed='终端命令失败', interrupted='运行中断，等待重新检查',
                  cancelled='已取消启动')
    text = labels.get(item['state'], item['state'])
    if item.get('exit_code') is not None:
        text += ' · 退出码 ' + str(item['exit_code'])
    if item.get('error'):
        text += '\n' + item['error']
    text += '\n登录状态和' + ('新会话启动' if item.get('kind') == 'new' else '原生恢复') + '结果以终端提示为准。'
    if item.get('sync_state') == 'failed':
        text += '\n续聊采集失败，可重试：' + item.get('sync_error', '')
    elif item.get('sync_state') == 'collected':
        text += '\n' + ('新会话历史采集已完成。' if item.get('kind') == 'new' else '续聊内容已采集。')
    if item.get('handoff_state') == 'cleaned':
        text += ('\n已保存最新主会话并清理已接替的旧文件。' if item.get('single_head') else
                 '\n已同步到 ' + item.get('durable_source', '') + '，临时副本已回收。')
    elif item.get('handoff_state') in ('preserved', 'pending'):
        text += '\n临时副本保留，等待安全同步／清理。'
    if item.get('durable_session'):
        text += '\n持久会话 ID：' + item['durable_session']
    return text


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied is not proof of exit
        try:
            code = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reconcile(identity=None):
    for item in [read(identity)] if identity else reports(limit=None):
        if item['state'] == 'running' and not process_alive(item.get('pid')):
            update(item['id'], expected_state='running', state='interrupted', sync_state='pending',
                   error='终端报告进程已退出；可刷新历史采集已保存的续聊内容。')
        elif item['state'] == 'prepared' and item.get('preparation_pid') and not process_alive(item['preparation_pid']):
            update(item['id'], expected_state='prepared', state='interrupted', sync_state='pending',
                   error='准备进程已退出；临时会话保留，可重试同步或重新启动。')


def mark_collected(report):
    for item in reports(limit=None):
        if item['source_path'] in report.get('skipped', []):
            continue
        if not item.get('handoff_state') and not item.get('single_head') and item['state'] in ('exited', 'failed', 'interrupted') and \
                item.get('sync_state') not in ('collected', 'not-needed'):
            failed = collection_failed(item, report)
            update(item['id'], sync_state='failed' if failed else 'collected',
                   sync_error='当前会话读取失败，请检查历史来源后重试。' if failed else '')


def collection_failed(item, report):
    if item.get('kind') == 'new':
        roots = [Path(root).resolve() for root in item.get('history_roots', [])]
        return any(not roots or not error.get('path') or any(
            Path(error['path']).resolve().is_relative_to(root) for root in roots)
            for error in report['failed'])
    return any(error.get('path', item['source_path']) == item['source_path'] for error in report['failed'])
