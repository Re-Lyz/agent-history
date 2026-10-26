"""Program ownership, independent account failures and native update guards."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_history as ah
from history_app import programs, accounts, program_maintenance as updates, compatibility
from history_app.paths import RuntimePaths
from history_app.adapters.codex import writer_lock
from setup_backend import atomic_json


class ProgramMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.paths = RuntimePaths(self.root / 'config', self.root / 'data', self.root / 'prefix', self.root / 'user')
        self.paths.shared.mkdir()
        self.paths.data.mkdir()
        self.homes = {key: self.root / key for key in ('one', 'two', 'three')}
        for home in self.homes.values():
            home.mkdir()
        atomic_json(self.paths.shared / 'connections.json', dict(connections={
            name: dict(home=str(home), program=sys.executable, direct=True) for name, home in self.homes.items()}))
        self.program_id = programs.describe(sys.executable, probe=False)['id']
        atomic_json(self.paths.data / 'program-policy.json', dict(version=1, pinned_accounts=['three'],
            adapters={self.program_id: dict(program=sys.executable, args=['--version'])}))

    def managed(self, name):
        binary = self.homes[name] / 'packages/app-server-daemon/current/bin' / ('codex.exe' if os.name == 'nt' else 'codex')
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'synthetic-binary')
        binary.chmod(0o755)
        return binary

    def test_path_releases_follow_current_without_caching_but_explicit_paths_remain_pinned(self):
        root = self.root / 'packages/standalone'
        old = root / 'releases/one/bin/codex'
        current = root / 'current/bin/codex'
        for path in (old, current):
            path.parent.mkdir(parents=True)
            path.write_text('fixture')
            path.chmod(0o755)
        with patch('shutil.which', side_effect=lambda name: str(old) if name == 'codex' else None):
            self.assertEqual(programs.resolve('codex'), str(current))
            self.assertEqual(programs.resolve(str(old)), str(old))
        self.assertEqual(programs.python(), str(Path(sys.executable)))
        placeholder = self.root / 'WindowsApps/python.exe'
        placeholder.parent.mkdir()
        placeholder.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'WindowsApps'):
            programs.resolve(str(placeholder))

    def test_home_and_profile_override_do_not_change_other_accounts(self):
        with patch.object(ah, 'ROOT', self.paths.shared):
            self.assertEqual(programs.for_home(self.homes['one']), sys.executable)
            with programs.using(self.homes['one'], 'explicit-fixture'):
                self.assertEqual(programs.for_home(self.homes['one']), 'explicit-fixture')
                self.assertEqual(programs.for_home(self.homes['two']), sys.executable)
            self.assertEqual(programs.for_home(self.homes['one']), sys.executable)

    def test_npm_updates_use_own_prefix_and_local_project_requires_an_adapter(self):
        prefix = self.root / 'npm-prefix'
        parent = prefix if os.name == 'nt' else prefix / 'lib'
        binary = parent / 'node_modules/@openai/codex/vendor/codex'
        binary.parent.mkdir(parents=True)
        binary.write_text('synthetic')
        binary.chmod(0o755)
        item = programs.describe(str(binary), probe=False)
        self.assertEqual(item['channel'], 'npm')
        self.assertEqual(item['npm_prefix'], str(prefix))
        with patch.object(programs, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            updates._update_cli(self.paths, item, dict(adapters={}))
        self.assertIn(str(prefix), run.call_args.args[1])
        (parent / 'package.json').write_text('{}')
        local = programs.describe(str(binary), probe=False)
        self.assertEqual(local['channel'], 'custom')

    def test_custom_entry_is_not_automatically_executed_by_inventory(self):
        entry = self.root / 'custom.ps1'
        entry.write_text('throw "must not run"')
        with patch.object(programs, 'run', side_effect=AssertionError('no custom commands')):
            result = programs.describe(str(entry), tool='custom')
        self.assertEqual(result['state'], 'not-probed')

    def test_inventory_separates_installed_and_unreachable_service_and_redacts_paths(self):
        self.managed('one')
        def run(program, args, **kwargs):
            if args == ['--version']:
                return subprocess.CompletedProcess(args, 0, 'codex 1.2.3', '')
            raise subprocess.TimeoutExpired(args, 1)
        with patch.object(programs, 'run', side_effect=run):
            report = accounts.inventory(self.paths)
        one = next(item for item in report['accounts'] if item['id'] == 'one')
        self.assertEqual(one['daemon']['state'], 'unreachable')
        self.assertEqual(one['daemon']['installed'], '1.2.3')
        self.assertEqual(len(report['programs']), 1)
        self.assertNotIn(str(self.root), json.dumps(accounts.shareable(report)))
        self.assertFalse((self.paths.data / 'history.db').exists())

    def test_partial_update_retries_independent_accounts_and_preserves_pins(self):
        for name in self.homes:
            self.managed(name)
        calls, synced = [], set()
        def run(program, args, home=None, **kwargs):
            home = str(home) if home is not None else None
            calls.append((args, home))
            if args == ['--version']:
                return subprocess.CompletedProcess(args, 0, 'codex 2.0.0', '')
            if args[-1:] == ['version']:
                value = dict(status='stopped', managedCodexVersion='2.0.0' if home in synced else '1.0.0')
                return subprocess.CompletedProcess(args, 0, json.dumps(value), '')
            if 'update' in args:
                if home == str(self.homes['one']):
                    return subprocess.CompletedProcess(args, 8, '', 'synthetic download failure')
                synced.add(home)
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(programs, 'run', side_effect=run):
            plan = updates.preview(self.paths, self.program_id)
            result = updates.apply(self.paths, plan, verify=False)
            retried = updates.apply(self.paths, updates.preview(self.paths, self.program_id), verify=False)
        self.assertEqual(result['state'], 'partial')
        states = {item['account']: item['state'] for item in result['stages'] if item['stage'] == 'daemon'}
        self.assertEqual(states, {'one': 'failed', 'two': 'passed', 'three': 'preserved-pin'})
        self.assertFalse(any('update' in args and home == str(self.homes['three']) for args, home in calls))
        self.assertEqual(next(item['state'] for item in retried['stages'] if item.get('account') == 'two'), 'already-current')
        self.assertEqual(sum('update' in args and home == str(self.homes['two']) for args, home in calls), 1)
        self.assertEqual(len(list(self.paths.backups.glob('*/manifest.json'))), 2)
        self.assertFalse((self.paths.data / 'history.db').exists())

    def test_active_native_writer_and_stale_preview_prevent_all_updates(self):
        with patch.object(programs, 'version', return_value=dict(state='ok', version='1.0.0')):
            plan = updates.preview(self.paths, self.program_id)
        with writer_lock(self.homes['one'], 'fixture') as held, patch.object(updates, '_update_cli') as update:
            self.assertTrue(held)
            with self.assertRaisesRegex(ValueError, '原生会话'):
                updates.apply(self.paths, plan, verify=False)
            update.assert_not_called()
        script = self.root / 'update.py'
        script.write_text('print("synthetic before")')
        atomic_json(self.paths.data / 'program-policy.json', dict(version=1, pinned_accounts=[],
            adapters={self.program_id: dict(program=sys.executable, args=[str(script), '{program}'])}))
        reviewed = updates.preview(self.paths, self.program_id)
        self.assertIn(sys.executable, reviewed['cli_operation']['args'])
        script.write_text('print("synthetic changed after review")')
        with patch.object(updates, '_update_cli') as update, self.assertRaisesRegex(ValueError, '脚本在预览后变化'):
            updates.apply(self.paths, reviewed, verify=False)
        update.assert_not_called()
        atomic_json(self.paths.data / 'program-policy.json', dict(version=1, pinned_accounts=[], adapters={}))
        with patch.object(updates, '_update_cli') as update, self.assertRaisesRegex(ValueError, '预览后变化'):
            updates.apply(self.paths, plan, verify=False)
        update.assert_not_called()

    def test_cli_failure_does_not_sync_daemons_or_recover_database(self):
        with patch.object(programs, 'version', return_value=dict(state='ok', version='1.0.0')):
            plan = updates.preview(self.paths, self.program_id)
        database = self.paths.data / 'history.db'
        database.write_bytes(b'synthetic-preserved-history')
        with patch.object(updates, '_update_cli', side_effect=ValueError('synthetic TLS failure')):
            result = updates.apply(self.paths, plan, verify=False)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(len(result['stages']), 1)
        self.assertEqual(database.read_bytes(), b'synthetic-preserved-history')

    def test_optional_quota_provider_cache_does_not_store_credentials(self):
        result = accounts.cache_quota(self.paths, 'one', dict(source='synthetic-provider',
            access_token='fake-private-token', limits=[dict(window='weekly', used_percent=25, resets_at=2000000000)]))
        self.assertEqual(result['state'], 'cached')
        self.assertEqual(result['limits'][0]['window'], 'weekly')
        self.assertTrue(result['checked_at'])
        stored = next((self.paths.data / 'maintenance/quotas').glob('*.json')).read_text(encoding='utf-8')
        self.assertNotIn('fake-private-token', stored)
        with self.assertRaises(ValueError):
            accounts.cache_quota(self.paths, 'one', dict(limits=[dict(used_percent=float('nan'))]))

    def test_native_checks_are_version_pinned_synthetic_and_record_partial_failures(self):
        calls = []
        real_run = subprocess.run
        def run(args, **kwargs):
            if not isinstance(args, list) or not str(args[-1]).endswith(('verify_ui_resume.py', 'verify_single_conversation.py')):
                return real_run(args, **kwargs)
            calls.append((args, kwargs['env']))
            return subprocess.CompletedProcess(args, 1 if len(calls) == 1 else 0, b'synthetic log', b'')
        with patch('subprocess.run', side_effect=run):
            result = compatibility.check(self.paths, sys.executable, names=['verify_ui_resume', 'verify_single_conversation'])
        self.assertEqual(result['state'], 'failed')
        self.assertEqual([item['state'] for item in result['checks']], ['failed', 'passed'])
        self.assertEqual(result['model_requests'], 0)
        for args, env in calls:
            self.assertNotEqual(env['HOME'], str(self.paths.user))
            self.assertEqual(env['AGENT_HISTORY_CODEX_PROGRAM'], sys.executable)
            self.assertNotIn('OPENAI_API_KEY', env)
        self.assertEqual(compatibility.latest(self.paths)[0]['id'], result['id'])
