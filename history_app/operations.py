"""Maintenance and launch preparation use the same collector lock."""
from contextlib import contextmanager
import json

from .locks import file_lock


@contextmanager
def idle_operation(paths):
    with file_lock(paths.data / 'collect.lock', wait=False) as locked:
        if not locked:
            raise ValueError('采集、启动或维护任务正在运行，请稍后重试。')
        import launch_jobs
        for path in (paths.data / 'launch-jobs').glob('*.json'):
            try:
                item = json.loads(path.read_text(encoding='utf-8'))
                field = 'preparation_pid' if item['state'] == 'prepared' else 'pid'
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ValueError('启动任务记录无法核对，请先运行诊断。') from error
            if item.get('sync_state') != 'collected' and launch_jobs.process_matches(item, field):
                raise ValueError('启动任务或终端仍在运行，请先结束任务并完成同步。')
        yield
