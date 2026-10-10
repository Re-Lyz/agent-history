"""Local Codex app-server transport. Never starts a model turn."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time


class CodexRPC:
    def __init__(self, home, timeout=45):
        self.home = Path(home).resolve()
        self.timeout = timeout
        self.responses = queue.Queue()
        self.errors = []
        self.serial = 0
        self.process = None

    def __enter__(self):
        executable = shutil.which('codex')
        if not executable:
            raise ValueError('Codex executable not found on PATH.')
        env = os.environ.copy()
        env['CODEX_HOME'] = str(self.home)
        env.pop('CODEX_SQLITE_HOME', None)
        self.process = subprocess.Popen(
            [executable, 'app-server', '--stdio'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            cwd=self.home, text=True, encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        try:
            self.call('initialize', {'clientInfo': {'name': 'agent_history', 'version': '0.2.0'},
                                     'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
            return self
        except BaseException:
            self.close()
            raise

    def _read(self):
        for line in self.process.stdout:
            try:
                self.responses.put(json.loads(line))
            except ValueError:
                pass
        self.responses.put(None)

    def _stderr(self):
        for line in self.process.stderr:
            self.errors.append(line.rstrip())
            del self.errors[:-30]

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def call(self, method, params):
        self.serial += 1
        serial = self.serial
        self.send({'id': serial, 'method': method, 'params': params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                response = self.responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(f'Codex {method} timed out after {self.timeout}s') from None
            if response is None:
                raise RuntimeError('Codex app-server exited: ' + '\n'.join(self.errors[-5:]))
            if response.get('id') == serial:
                if 'error' in response:
                    raise ValueError(f"Codex {method}: {response['error']['message']}")
                return response['result']

    def close(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()

    def __exit__(self, *args):
        self.close()
