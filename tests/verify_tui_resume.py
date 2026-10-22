"""Real idle Codex TUI /resume switching with synthetic history and no login.

Requires pywinpty on Windows or pexpect on Linux. A loopback provider refuses
model requests. This does not certify authenticated turns or background work.
"""
import contextlib
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_history as ah
import codex_import as ci
from native_codex import CodexRPC


class Terminal:
    def __init__(self, argv, root, env):
        self.output = ''
        self.stopped = False
        if os.name == 'nt':
            from winpty import PtyProcess
            self.child = PtyProcess.spawn(argv, cwd=str(root), env=env, dimensions=(40, 120))
        else:
            import pexpect
            self.child = pexpect.spawn(argv[0], argv[1:], cwd=str(root), env=env,
                                       encoding='utf-8', dimensions=(40, 120))
        self.worker = threading.Thread(target=self.read, daemon=True)
        self.worker.start()

    def write(self, text):
        if os.name == 'nt':
            self.child.write(text)
        else:
            self.child.send(text)

    def read(self):
        while not self.stopped:
            try:
                if os.name == 'nt':
                    value = self.child.read(65536)
                else:
                    import pexpect
                    try:
                        value = self.child.read_nonblocking(65536, timeout=.2)
                    except pexpect.TIMEOUT:
                        continue
                self.output += value
                if '\x1b[6n' in value:
                    self.write('\x1b[1;1R')
            except Exception:
                return

    def command(self, text):
        offset = len(self.output)
        self.write(text)
        self.wait(lambda: text in self.output[offset:])
        self.write('\r')

    def wait(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if not self.child.isalive():
                self.worker.join(1)
                break
            time.sleep(.05)
        screen = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', self.output)[-4000:]
        raise AssertionError('Synthetic TUI did not reach expected state: ' + repr(screen))

    def close(self):
        try:
            if self.child.isalive():
                self.command('/quit')
                deadline = time.monotonic() + 10
                while self.child.isalive() and time.monotonic() < deadline:
                    time.sleep(.1)
                if self.child.isalive():
                    raise AssertionError('The synthetic TUI did not exit after /quit.')
        finally:
            self.stopped = True
            self.child.close(force=True)
            self.worker.join(2)


def busy(home, sid):
    with ci.writer_lock(home, sid) as free:
        return not free


def verify():
    posts = []
    class Provider(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"data":[]}'
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            posts.append(self.path)
            self.send_error(503, 'Model requests are forbidden in this test')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix='verify-tui-resume-') as temporary:
            root = Path(temporary).resolve()
            home, shared = root / 'codex', root / 'shared'
            home.mkdir(); shared.mkdir()
            config = ('model="validation-model"\nmodel_provider="validation"\n'
                '[model_providers.validation]\nname="Local validation"\n'
                f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
                'wire_api="responses"\nrequires_openai_auth=false\n'
                '[features]\nmemories=false\nplugins=false\napps=false\n')
            (home / 'config.toml').write_text(config, encoding='utf-8')
            threads = []
            with CodexRPC(home) as rpc:
                for index in range(2):
                    threads.append(rpc.call('thread/start', dict(cwd=str(root), model='validation-model'))['thread'])
            stamp = dt.datetime.now(dt.timezone.utc).isoformat()
            for index, thread in enumerate(threads):
                marker = f'synthetic-tui-saved-{index}'
                thread['marker'] = marker
                meta = dict(id=thread['id'], cwd=str(root), source='cli', timestamp=stamp,
                            history_mode='legacy', model_provider='validation', originator='codex_cli_rs',
                            cli_version='0.0.0')
                records = [dict(type='session_meta', timestamp=stamp, payload=meta)]
                for role, text in [('user', marker), ('assistant', marker + '-answer')]:
                    records += [dict(type='response_item', timestamp=stamp, payload=dict(type='message',
                        role=role, content=[dict(type='input_text' if role == 'user' else 'output_text', text=text)])),
                        dict(type='event_msg', timestamp=stamp, payload=dict(
                            type='user_message' if role == 'user' else 'agent_message', message=text))]
                path = Path(thread['path']); path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
                thread['saved_messages'] = [record['payload'] for record in records if record['type'] == 'response_item']
            (shared / 'connections.json').write_text(json.dumps(dict(connections={'a': dict(home=str(home))})), encoding='utf-8')
            with patch.object(ah, 'ROOT', shared), patch.object(ah, 'DATA', root / 'data'), \
                    patch.object(ah, 'CLAUDE_HOMES', []), contextlib.closing(ah.connect()) as db:
                ah.collect(db)
                for thread in threads:
                    row = dict(ah.resolve(db, thread['id']))
                    ci.register_native(home, row['id'], Path(row['path']), row,
                                       ci.first_meta(row['path']), ci.config(home))
            with CodexRPC(home) as rpc:
                for thread in threads:
                    rpc.call('thread/resume', dict(threadId=thread['id']))
            env = {key: value for key, value in os.environ.items() if not key.startswith(('CODEX_', 'OPENAI_'))}
            env.update(CODEX_HOME=str(home), HOME=str(root), USERPROFILE=str(root), TERM='xterm-256color')
            executable = shutil.which('codex')
            help_text = subprocess.run([executable, '--help'], env=env, capture_output=True,
                                       text=True, encoding='utf-8', timeout=20, check=True).stdout
            args = [executable, '--no-alt-screen', '-a', 'never', '-s', 'read-only', '-C', str(root)]
            if '--no-daemon' in help_text:
                args.append('--no-daemon')
            terminal = Terminal([*args, 'resume', threads[0]['id']], root, env)
            try:
                terminal.wait(lambda: threads[0]['marker'] + '-answer' in terminal.output)
                terminal.wait(lambda: busy(home, threads[0]['id']))
                terminal.output = ''
                terminal.command('/resume ' + threads[1]['id'])
                terminal.wait(lambda: threads[1]['marker'] + '-answer' in terminal.output)
                terminal.wait(lambda: busy(home, threads[1]['id']))
                assert terminal.child.isalive()
                old_busy = busy(home, threads[0]['id'])
            finally:
                terminal.close()
            assert not busy(home, threads[0]['id']) and not busy(home, threads[1]['id'])
            for thread in threads:
                saved = [record.get('payload') for record in ci.records(thread['path'])
                         if record.get('type') == 'response_item']
                assert all(message in saved for message in thread['saved_messages'])
            assert not posts, 'The TUI attempted a model request.'
            print(json.dumps(dict(real_tui=True, idle_resume_switch=True, terminal_stayed_alive=True,
                old_lock_retained_after_switch=old_busy, locks_released_after_exit=True,
                original_content_preserved=True, model_requests=0, real_login=False,
                active_turns_tested=False, resume_command='by_id', natural_quit=True)))
    finally:
        server.shutdown(); server.server_close(); worker.join(2)


if __name__ == '__main__':
    verify()
