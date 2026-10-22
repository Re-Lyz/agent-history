"""Exercise real legacy entry points and package imports in fresh processes."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from history_app.paths import SOURCE


ALIASES = {
    'agent_history': 'history_app.storage.history',
    'conversation_store': 'history_app.storage.revisions',
    'conversation_merge': 'history_app.storage.merge',
    'native_codex': 'history_app.adapters.codex_rpc',
    'codex_import': 'history_app.adapters.codex',
    'claude_to_codex': 'history_app.adapters.claude',
    'launch_backend': 'history_app.launch.planning',
    'single_conversation': 'history_app.launch.reservations',
    'handoff_lifecycle': 'history_app.launch.handoff',
    'launch_jobs': 'history_app.launch.jobs',
    'launch_job_runner': 'history_app.launch.job_runner',
    'launch_runner': 'history_app.launch.runner',
    'history_ui': 'history_app.ui.application',
}


class ModuleBoundaryTests(unittest.TestCase):
    def test_import_order_preserves_identity_and_requires_no_desktop(self):
        script = '''
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
aliases = json.loads(sys.argv[2])
viewer = aliases.pop('history_ui')
for legacy, canonical in aliases.items():
    first, second = (canonical, legacy) if sys.argv[3] == 'package' else (legacy, canonical)
    a, b = importlib.import_module(first), importlib.import_module(second)
    assert a is b, (legacy, canonical)
import agent_history as ah, conversation_store as store, launch_backend as launch
assert store.ah is ah and launch.ah is ah
assert not any(name == 'tkinter' or name.startswith('tkinter.') for name in sys.modules)
assert importlib.import_module('history_ui') is importlib.import_module(viewer)
'''
        with tempfile.TemporaryDirectory() as folder:
            for order in ('legacy', 'package'):
                with self.subTest(order=order):
                    subprocess.run([sys.executable, '-c', script, str(SOURCE),
                                    json.dumps(ALIASES), order], cwd=folder, check=True, timeout=30)

    def test_runner_entry_preserves_nonzero_exit_and_literal_arguments(self):
        payload = base64.b64encode(json.dumps(dict(program=sys.executable,
            args=['-c', 'import sys; assert sys.argv[1] == "中文 & space"; sys.exit(23)',
                  '中文 & space'])).encode()).decode()
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, str(SOURCE / 'launch_runner.py'), payload],
                                    cwd=folder, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 23, result.stderr)

    def test_history_cli_custom_paths_do_not_depend_on_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared, data = root / '配置 中文', root / '历史 中文'
            shared.mkdir()
            data.mkdir()
            (shared / 'connections.json').write_text('{"connections": {}}', encoding='utf-8')
            (data / 'sources.json').write_text(
                '{"version": 1, "codex": [], "claude": []}', encoding='utf-8')
            command = [sys.executable, str(SOURCE / 'agent_history.py'), '--shared-root',
                       str(shared), '--data-dir', str(data)]
            for operation in (['collect'], ['sessions'], ['search', 'synthetic-no-match']):
                result = subprocess.run(command + operation, cwd=root, capture_output=True,
                                        text=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((data / 'history.db').is_file())
            self.assertFalse((root / 'history.db').exists())
