"""Private configuration recovery and installation failures on synthetic homes."""
from dataclasses import replace
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_history as ah
from history_app import config_backup as backups, diagnostics, installation, installer
from history_app.paths import RuntimePaths, SOURCE
from history_app.locks import file_lock
from setup_backend import atomic_json


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.paths = RuntimePaths(self.root / 'config', self.root / 'user/.agent-history',
                                  self.root / 'install', self.root / 'user')
        self.paths.shared.mkdir(parents=True)
        self.paths.data.mkdir(parents=True)
        self.config = self.paths.configuration()
        atomic_json(self.config['connections'], dict(connections={}))
        atomic_json(self.config['sources'], dict(version=1, codex=[], claude=[]))

    def content(self):
        return {name: backups.fingerprint(path)[1] for name, path in self.config.items()}

    def install(self, **features):
        spec = installer.plan(self.paths.prefix, self.paths.user, sys.executable,
            shared_root=self.paths.shared, data_dir=self.paths.data, **features)
        spec['shortcuts'] = []  # COM shortcuts have a separate actual-platform test.
        return installer.install(spec)

    def test_diagnosis_does_not_create_or_migrate_database_and_export_omits_private_values(self):
        secret = 'synthetic-sensitive-value'
        atomic_json(self.config['launch-profiles'], dict(version=1, profiles=[dict(
            id='fixture', type='custom', program='fixture-agent', args=[], env=dict(PRIVATE_SETTING=secret))]))
        with patch.object(ah, 'connect', side_effect=AssertionError('diagnostics must be read-only')):
            report = diagnostics.diagnose(self.paths, deep=True)
        public = diagnostics.render(report)
        self.assertNotIn(str(self.root), public)
        self.assertNotIn(secret, public)
        self.assertNotIn('PRIVATE_SETTING', public)
        self.assertEqual(report['configuration']['launch-profiles'], 'ok')
        self.assertEqual(report['database']['state'], 'missing')
        self.assertFalse((self.paths.data / 'history.db').exists())

    def test_readonly_database_check_preserves_unsupported_schema_and_deleted_records(self):
        path = self.paths.data / 'history.db'
        with closing(sqlite3.connect(path)) as db:
            db.executescript('PRAGMA user_version=99; CREATE TABLE deleted_copies(id); INSERT INTO deleted_copies VALUES(7);')
        before = path.read_bytes()
        report = diagnostics.diagnose(self.paths, deep=True)
        self.assertEqual(report['database']['integrity'], 'ok')
        self.assertEqual(report['database']['compatibility'], 'needs-review')
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_configuration_and_job_payload_are_reported_without_contents(self):
        self.config['connections'].write_text('{not-json private fixture text', encoding='utf-8')
        jobs = self.paths.data / 'launch-jobs'
        jobs.mkdir()
        atomic_json(jobs / 'broken.json', dict(state='private-fixture-state', source_path='private-fixture-path'))
        (jobs / 'malformed.json').write_text('[', encoding='utf-8')
        report = diagnostics.diagnose(self.paths)
        exported = diagnostics.render(report)
        self.assertEqual(report['configuration']['connections'], 'invalid')
        self.assertEqual(report['jobs'], dict(states={'unknown': 1}, unreadable=1))
        self.assertNotIn('private-fixture', exported)

    def test_restore_preserves_database_auth_and_immutable_snapshots_and_backs_up_replaced_config(self):
        original = self.content()
        identity = backups.backup(self.paths)
        manifest = (self.paths.backups / identity / 'manifest.json').read_bytes()
        atomic_json(self.config['connections'], dict(connections={'new': dict(home='new-home')}))
        atomic_json(self.config['ui-preferences'], dict(version=1, geometry='1200x700+20+20'))
        protected = [self.paths.data / 'history.db', self.paths.user / '.codex/auth.json',
                     self.paths.data / 'conversations/revisions/immutable.json']
        for path in protected:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic protected bytes')
        changed = self.content()
        result = backups.restore(self.paths, backups.preview_restore(self.paths, identity))
        self.assertEqual(self.content(), original)
        self.assertNotEqual(result['recovery_backup'], identity)
        for name, data in changed.items():
            if data is not None:
                self.assertEqual((self.paths.backups / result['recovery_backup'] / (name + '.bin')).read_bytes(), data)
        self.assertEqual((self.paths.backups / identity / 'manifest.json').read_bytes(), manifest)
        self.assertTrue(all(path.read_bytes() == b'synthetic protected bytes' for path in protected))

    def test_corrupt_backup_or_foreign_manifest_cannot_partially_restore(self):
        identity = backups.backup(self.paths)
        before = self.content()
        (self.paths.backups / identity / 'sources.bin').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            backups.preview_restore(self.paths, identity)
        self.assertEqual(self.content(), before)
        with self.assertRaises(ValueError):
            backups.preview_restore(replace(self.paths, shared=self.root / 'other'), identity)
        with self.assertRaises(ValueError):
            backups.preview_restore(self.paths, '../escape')

    def test_restore_refuses_new_edits_and_live_jobs(self):
        identity = backups.backup(self.paths)
        plan = backups.preview_restore(self.paths, identity)
        atomic_json(self.config['connections'], dict(connections={'changed': dict(home='new-home')}))
        with self.assertRaisesRegex(ValueError, '预览后'):
            backups.restore(self.paths, plan)
        plan = backups.preview_restore(self.paths, identity)
        atomic_json(self.paths.data / 'launch-jobs/live.json', dict(state='running', pid=os.getpid(), sync_state='pending'))
        with self.assertRaisesRegex(ValueError, '仍在运行'):
            backups.restore(self.paths, plan)
        self.assertEqual(len(backups.inventory(self.paths)), 1)

    def test_collector_lock_blocks_restore_instead_of_waiting_or_deleting_data(self):
        identity = backups.backup(self.paths)
        plan = backups.preview_restore(self.paths, identity)
        with file_lock(self.paths.data / 'collect.lock', False) as locked:
            self.assertTrue(locked)
            with self.assertRaisesRegex(ValueError, '正在运行'):
                backups.restore(self.paths, plan)

    def test_mid_restore_write_failure_rolls_back_and_keeps_recovery_snapshot(self):
        identity = backups.backup(self.paths)
        atomic_json(self.config['connections'], dict(connections={'new': dict(home='fixture')}))
        atomic_json(self.config['sources'], dict(version=1, codex=['new'], claude=[]))
        before = self.content()
        write, failed = backups._write, []
        def fail_once(path, data):
            if path == self.config['sources'] and not failed:
                failed.append(True)
                raise OSError('synthetic write failure')
            return write(path, data)
        with patch.object(backups, '_write', side_effect=fail_once):
            with self.assertRaises(OSError):
                backups.restore(self.paths, backups.preview_restore(self.paths, identity))
        self.assertEqual(self.content(), before)
        self.assertEqual(len(backups.inventory(self.paths)), 2)

    def test_update_preserves_features_and_custom_data_location_and_uninstall_retains_history(self):
        self.install(claude_mcp=True)
        config = self.paths.user / '.claude.json'
        value = json.loads(config.read_text(encoding='utf-8'))
        value['unrelated'] = 'preserve'
        atomic_json(config, value)
        plan = installation.preview(self.paths, 'upgrade')
        self.assertTrue(plan['spec']['features']['claude_mcp'])
        self.assertFalse(plan['spec']['features']['claude_hook'])
        self.assertEqual(plan['spec']['data'], str(self.paths.data))
        plan['spec']['shortcuts'] = []
        result = installation.apply(self.paths, plan)
        self.assertTrue(result['result']['updated'])
        self.assertEqual(json.loads(config.read_text())['unrelated'], 'preserve')
        history = self.paths.data / 'history.db'
        history.write_bytes(b'preserve history and deleted-copy records')
        plan = installation.preview(self.paths, 'uninstall')
        self.assertTrue((self.paths.prefix / 'installation.json').exists())
        installation.apply(self.paths, plan)
        self.assertEqual(history.read_bytes(), b'preserve history and deleted-copy records')
        self.assertEqual(json.loads(config.read_text()), dict(mcpServers={}, unrelated='preserve'))

    def test_update_rejects_modified_owned_entry_and_stale_previews(self):
        self.install()
        plan = installation.preview(self.paths, 'uninstall')
        manifest = self.paths.prefix / 'installation.json'
        value = json.loads(manifest.read_text())
        entry = Path(next(iter(value['files'])))
        entry.write_bytes(b'user modified entry')
        with self.assertRaisesRegex(ValueError, '预览后'):
            installation.apply(self.paths, plan)
        with self.assertRaisesRegex(ValueError, '用户修改'):
            installation.preview(self.paths, 'upgrade')
        self.assertEqual(entry.read_bytes(), b'user modified entry')

    def test_partial_install_can_be_uninstalled_without_losing_prior_integration(self):
        config = self.paths.user / '.claude.json'
        config.parent.mkdir(parents=True, exist_ok=True)
        original = dict(mcpServers={'other': dict(command='keep')}, theme='keep')
        atomic_json(config, original)
        from integrations.install_mcp import install as integrate
        calls = []
        def fail_hook(*args, **kwargs):
            calls.append(kwargs)
            if kwargs.get('startup_hook'):
                raise OSError('synthetic hook registration failure')
            return integrate(*args, **kwargs)
        with patch('integrations.install_mcp.install', side_effect=fail_hook):
            with self.assertRaises(OSError):
                self.install(claude_mcp=True, claude_hook=True)
        self.assertTrue((self.paths.prefix / 'installation.json').exists())
        installer.uninstall(self.paths.prefix)
        self.assertEqual(json.loads(config.read_text()), original)

    def test_cli_and_install_entry_work_from_another_directory_without_loading_tk(self):
        script = ('import sys; sys.path.insert(0,sys.argv[1]); import install; '
                  'import history_app.cli, history_app.diagnostics, history_app.installation; '
                  'assert not any(x=="tkinter" or x.startswith("tkinter.") for x in sys.modules); '
                  'assert install.install is history_app.installation.installer.install')
        subprocess.run([sys.executable, '-c', script, str(SOURCE)], cwd=self.root, check=True, timeout=20)
        result = subprocess.run([sys.executable, str(SOURCE / 'maintenance.py'), 'diagnose',
            '--shared-root', str(self.paths.shared), '--data-dir', str(self.paths.data), '--prefix', str(self.paths.prefix)],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=20, check=True)
        self.assertNotIn(str(self.root), result.stdout)
        self.assertEqual(json.loads(result.stdout)['database']['state'], 'missing')
