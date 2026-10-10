"""Run an interactive Agent command and report exit/sync even after the UI closes."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import agent_history as ah
import launch_jobs as jobs
import launch_runner


def checked_inner(inner):
    """PowerShell must forward the Agent exit code instead of returning script success."""
    if inner[:1] == ['-File'] and len(inner) >= 2:
        script = launch_runner.invocation(inner[1], inner[2:])
    elif inner[:1] == ['-EncodedCommand'] and len(inner) == 2:
        script = base64.b64decode(inner[1]).decode('utf-16le')
    else:
        return inner
    script += '\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }'
    return ['-EncodedCommand', base64.b64encode(script.encode('utf-16le')).decode('ascii')]


def run(identity, inner):
    jobs.update(identity, state='running', pid=os.getpid(), attempts=jobs.read(identity)['attempts'] + 1)
    code = 1
    try:
        argv = ['powershell.exe', '-NoLogo', '-ExecutionPolicy', 'Bypass', *checked_inner(inner)] if os.name == 'nt' else inner
        result = subprocess.run(argv, shell=False)
        code = result.returncode
        jobs.update(identity, state='exited' if code == 0 else 'failed', exit_code=code,
                    error='' if code == 0 else 'Agent 命令非正常结束；请查看此终端的具体错误。')
    except OSError as error:
        jobs.update(identity, state='failed', exit_code=None, error=type(error).__name__ + ': 无法启动终端命令。')
    finally:
        try:
            import conversation_store
            with ah.collect_lock(wait=True) as locked:
                if not locked:
                    raise TimeoutError('History collector is busy.')
                import contextlib
                with contextlib.closing(ah.connect()) as db:
                    ah.collect(db)
                    import handoff_lifecycle
                    if handoff_lifecycle.get(db, identity):
                        # finish verifies the owned origin/temporary/durable
                        # revisions directly; unrelated sync is independent.
                        handoff_lifecycle.finish(db, identity)
                    else:
                        report = conversation_store.sync_all(db)
                        job = jobs.read(identity)
                        if job['source_path'] in report.get('skipped', []):
                            raise ValueError('当前历史来源已停用，重新接入后可重试采集。')
                        if jobs.collection_failed(job, report):
                            raise ValueError('当前会话读取失败，请检查历史来源。')
                        jobs.update(identity, sync_state='collected', sync_error='')
        except Exception as error:
            owned = {'handoff_state': 'pending'} if jobs.read(identity).get('handoff_state') else {}
            jobs.update(identity, sync_state='failed', **owned,
                        sync_error=(str(error) if isinstance(error, ValueError) else type(error).__name__) + '；可重试同步。')
    return code


def main():
    payload = json.loads(base64.b64decode(sys.argv[1]).decode('utf-8'))
    return run(payload['job'], payload['inner'])


if __name__ == '__main__':
    sys.exit(main())
