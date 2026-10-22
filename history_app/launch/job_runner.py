"""Run an interactive Agent command and report exit/sync even after the UI closes."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import agent_history as ah
import launch_jobs as jobs
import launch_runner


def observe_native_session(identity, row, home, expected=None):
    """Record actual native writer-lock acquisition, not merely a live terminal."""
    import codex_import
    lock_path = home / 'thread-writer-locks' / (row['id'] + '.lock')
    if not lock_path.exists():
        return False
    with codex_import.writer_lock(home, row['id']) as free:
        if free:
            return False
    report = jobs.read(identity)
    jobs.update(identity, expected_state='running',
                expected_fields=expected or {key: report.get(key) for key in ('pid', 'pid_started', 'attempts')},
                native_session_observed=True)
    return True


def native_observer(db, identity):
    """Observe once, without reading content or touching a native database."""
    report = jobs.read(identity)
    item = db.execute('SELECT row_json FROM conversation_versions WHERE id=?',
                      (report.get('version_id'),)).fetchone()
    if not item:
        return lambda: None
    row = json.loads(item['row_json'])
    home = dict(ah.codex_homes()).get(row['source']) if row['tool'] == 'codex' else None
    if home is None:
        return lambda: None
    stop = threading.Event()
    expected = {key: report.get(key) for key in ('pid', 'pid_started', 'attempts')}
    def observe():
        while not stop.wait(0.2):
            try:
                if observe_native_session(identity, row, home, expected):
                    return
            except (OSError, ValueError, TimeoutError):
                return  # Unknown is not permission to release a conversation.
    thread = threading.Thread(target=observe, daemon=True)
    thread.start()
    def finish():
        stop.set()
        thread.join(timeout=2)
    return finish


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
    code = 1
    claimed = False
    rejected = False
    stop_observing = lambda: None
    try:
        import single_conversation
        with ah.collect_lock(wait=True) as locked:
            if not locked:
                raise TimeoutError('History collector is busy.')
            import contextlib
            with contextlib.closing(ah.connect()) as db:
                jobs.check_claim(jobs.read(identity))
                single_conversation.claim(db, identity)
                jobs.claim(identity)
                claimed = True
                stop_observing = native_observer(db, identity)
        argv = ['powershell.exe', '-NoLogo', '-ExecutionPolicy', 'Bypass', *checked_inner(inner)] if os.name == 'nt' else inner
        result = subprocess.run(argv, shell=False)
        code = result.returncode
        jobs.update(identity, state='exited' if code == 0 else 'failed', exit_code=code,
                    error='' if code == 0 else 'Agent 命令非正常结束；请查看此终端的具体错误。')
    except jobs.LaunchRejected:
        rejected = True
    except (OSError, ValueError, TimeoutError) as error:
        existing = jobs.read(identity)
        if not claimed and existing['state'] == 'running' and jobs.process_matches(existing):
            rejected = True
            return code
        jobs.update(identity, state='failed', exit_code=None,
                    error=str(error) if isinstance(error, ValueError) else type(error).__name__ + ': 无法启动终端命令。')
    finally:
        stop_observing()
        if not claimed:
            if not rejected and jobs.read(identity)['state'] != 'running':
                jobs.update(identity, sync_state='failed', sync_error='启动未获得独占权；请查看最新主对话和启动记录。')
            return code
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
                    elif jobs.read(identity).get('single_head'):
                        single_conversation.finish(db, identity)
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
