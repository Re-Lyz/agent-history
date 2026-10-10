import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import connections


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / 'connections.json').write_text('{"version": 1, "connections": {}}')
        self.home = self.root / 'account'
        self.home.mkdir()
        (self.home / 'profile.toml').write_text('model = "test"\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_register_preserves_registry_and_rejects_duplicate_home(self):
        connections.register(self.root, 'api', self.home, 'MY_API_KEY', True)
        data = connections.load(self.root)
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['connections']['api']['key_env'], 'MY_API_KEY')
        self.assertTrue(data['connections']['api']['sync_history'])
        self.assertEqual(connections.homes(self.root)['api'], self.home.resolve())
        before = (self.root / 'connections.json').read_bytes()
        for name in ('api', 'another'):
            with self.assertRaises(ValueError):
                connections.register(self.root, name, self.home)
        self.assertEqual((self.root / 'connections.json').read_bytes(), before)

    def test_invalid_input_and_failed_publication_leave_registry_unchanged(self):
        path = self.root / 'connections.json'
        before = path.read_bytes()
        for name, directory, key in [('bad/name', self.home, None),
                                      ('api', self.home, 'BAD=KEY'), ('api', self.root / 'missing', None)]:
            with self.assertRaises(ValueError):
                connections.register(self.root, name, directory, key)
        with patch.object(Path, 'replace', side_effect=OSError('publish failed')):
            with self.assertRaises(OSError):
                connections.register(self.root, 'api', self.home)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.glob('connections-*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher')
    def test_launch_respects_auto_sync_switch_and_keeps_explicit_sync(self):
        log = self.root / 'calls.txt'
        for name, label in [('python-fixture.cmd', 'PYTHON'), ('codex.cmd', 'CODEX')]:
            (self.root / name).write_text('@echo off\r\n'
                f'echo {label} %*>> "%AGENT_HISTORY_TEST_LOG%"\r\nexit /b 0\r\n')
        (self.root / 'common.toml').write_text('')
        (self.home / 'state_5.sqlite').touch()
        env = {**os.environ, 'AGENT_HISTORY_SHARED_ROOT': str(self.root),
               'AGENT_HISTORY_PYTHON': str(self.root / 'python-fixture.cmd'),
               'AGENT_HISTORY_TEST_LOG': str(log), 'PATH': str(self.root) + os.pathsep + os.environ['PATH']}
        launcher = Path(connections.__file__).with_name('launch.ps1')
        for enabled, args, expected in [(False, [], ['CODEX']), (True, [], ['PYTHON', 'CODEX']),
                                         (True, ['-NoSync'], ['CODEX']),
                                         (False, ['-SyncHistory'], ['PYTHON'])]:
            (self.root / 'connections.json').write_text(json.dumps(dict(connections=dict(
                api=dict(home=str(self.home), launch_args=[], sync_history=enabled)))))
            log.unlink(missing_ok=True)
            result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(launcher), 'api', *args], env=env,
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([line.split()[0] for line in log.read_text().splitlines()], expected)


if __name__ == '__main__':
    unittest.main()
