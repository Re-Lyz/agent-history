import csv
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import agent_history as ah
import audit_history
import ui_support as support


class ViewerSupportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.program = self.root / 'fixture-cli'
        self.program.write_text('#!/bin/sh\nexit 0\n')
        self.program.chmod(0o755)
        self.shared = self.root / 'shared'
        self.shared.mkdir()
        self.home = self.root / 'connection'
        self.home.mkdir()
        self.data = self.root / 'data'
        self.patches = [patch.object(ah, 'DATA', self.data), patch.object(ah, 'ROOT', self.shared),
                        patch.object(ah, 'CLAUDE_HOMES', [])]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_preferences_persist_only_ui_fields_and_recover_from_bad_values(self):
        raw = dict(geometry='1200x720+50+30', panes=[0.3, 0.7], maximized=True,
                   filters=dict(query='中文搜索', source='work', project=str(self.home), imports=True, internal='bad'),
                   project_targets={str(self.home): 'work'}, default_target='work', preview_mode='raw', password='never persist')
        support.save_preferences(raw)
        loaded = support.load_preferences()
        self.assertEqual(loaded['filters'], dict(query='中文搜索', source='work', project=str(self.home), imports=True))
        self.assertEqual(loaded['panes'], [0.3, 0.7])
        self.assertEqual(loaded['geometry'], '1200x720+50+30')
        self.assertEqual(loaded['preview_mode'], 'raw')
        self.assertNotIn('preview_mode', support.clean_preferences(dict(preview_mode='invalid')))
        self.assertNotIn('password', (self.data / 'ui-preferences.json').read_text(encoding='utf-8'))
        self.assertFalse(any(path.name.startswith('tmp') for path in self.data.iterdir()))
        cleaned = support.clean_preferences(dict(geometry='bad', panes=[True, 0.7], filters=[], project_targets=[]))
        self.assertEqual(cleaned['panes'], [0.4, 0.75])
        (self.data / 'ui-preferences.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(support.load_preferences(), support.clean_preferences({}))
        self.assertTrue(support.error_log_path().is_file())

    def test_failed_preferences_publication_preserves_previous_file(self):
        support.save_preferences(dict(default_target='old'))
        before = (self.data / 'ui-preferences.json').read_bytes()
        with patch.object(support.os, 'replace', side_effect=OSError('publication failed')):
            with self.assertRaises(OSError):
                support.save_preferences(dict(default_target='new'))
        self.assertEqual((self.data / 'ui-preferences.json').read_bytes(), before)
        self.assertEqual([p.name for p in self.data.iterdir()], ['ui-preferences.json'])

    def test_missing_project_command_and_changed_screen_fall_back(self):
        row = dict(cwd=str(self.home), tool='codex', source='a')
        prefs = support.clean_preferences(dict(project_targets={str(self.home): 'gone'}, default_target='gone'))
        self.assertEqual(support.preferred_target(row, ['a', 'b', 'claude'], prefs), 'a')
        prefs['project_targets'][support.project_key(str(self.home))] = 'b'
        self.assertEqual(support.preferred_target(row, ['a', 'b'], prefs), 'b')
        self.assertEqual(support.preferred_target(row, [], prefs), '')
        geometry = support.fit_geometry('2000x1500+5000+4000', 1366, 768)
        self.assertEqual(geometry, '1326x688+40+40')
        self.assertEqual(support.fit_geometry('bad', 1024, 600), '984x520+40+40')

    def configure_connection(self, profile='model="test-model"\n'):
        (self.root / 'codex-switch.ps1').write_text('# fixture\n')
        (self.shared / 'connections.json').write_text(json.dumps({'connections': {
            'work': {'home': str(self.home)}}}))
        (self.shared / 'common.toml').write_text('approval_policy="on-request"\n')
        (self.home / 'profile.toml').write_text(profile)
        (self.home / 'config.toml').write_text(profile)
        (self.home / 'state_5.sqlite').touch()

    def test_recycle_bin_summary_handles_deadlines_and_old_metadata(self):
        now = dt.datetime(2026, 10, 6, tzinfo=dt.timezone.utc)
        manifest = dict(expires_at=(now + dt.timedelta(days=30)).isoformat(), items=[
            dict(source='a', kind='main', key='codex:original'),
            dict(source='b', kind='import', key='codex:copy'),
            dict(source='claude', key='claude:legacy-copy'),
            dict(source='a', key='codex:unknown')])
        imported = {'claude:legacy-copy'}
        summary = support.trash_summary(manifest, imported, now)
        self.assertEqual(summary['remaining'], '30 天')
        self.assertEqual(summary['counts'], '4 项 · 2 副本 · 1 类型未记录')
        self.assertEqual(summary['sources'], 'a、b、claude')
        self.assertFalse(summary['soon'])
        self.assertEqual(support.trash_item_kind(manifest['items'][0], imported), '原会话')
        for delta, label in ((dt.timedelta(hours=1), '不到 1 天'),
                             (dt.timedelta(days=2, seconds=1), '3 天'),
                             (dt.timedelta(), '已到期'), (dt.timedelta(seconds=-1), '已到期')):
            manifest['expires_at'] = (now + delta).isoformat()
            summary = support.trash_summary(manifest, imported, now)
            self.assertEqual(summary['remaining'], label)
            self.assertTrue(summary['soon'])

    def test_launch_check_never_starts_process_and_reports_missing_dependencies(self):
        self.configure_connection()
        with patch.object(support, 'WINDOWS_LAUNCHER', True), \
                patch.object(support.shutil, 'which', return_value=str(self.program)), \
                patch.object(support.subprocess, 'Popen') as start:
            self.assertTrue(support.launch_readiness('work', self.root)['ready'])
            (self.home / 'profile.toml').write_text('model="CHANGE_ME_MODEL"\n')
            (self.home / 'config.toml').write_text('model="CHANGE_ME_MODEL"\n')
            incomplete = support.launch_readiness('work', self.root)
            self.assertFalse(incomplete['ready'])
            self.assertIn('model', incomplete['message'])
            start.assert_not_called()
        with patch.object(support, 'WINDOWS_LAUNCHER', True), patch.object(support.shutil, 'which', return_value=None):
            (self.root / 'codex-switch.ps1').unlink()
            missing = support.launch_readiness('work', self.root)
            if os.name == 'nt':
                self.assertIn('PowerShell', missing['message'])
                self.assertIn('codex-switch.ps1', missing['message'])
            else:
                self.assertIn('终端', missing['message'])
            self.assertIn('codex 程序', missing['message'])
            self.assertIn('claude 程序', support.launch_readiness('claude', self.root)['message'])

    def test_invalid_toml_and_uninitialized_connection_are_actionable(self):
        self.configure_connection('model=[invalid\n')
        (self.home / 'state_5.sqlite').unlink()
        with patch.object(support, 'WINDOWS_LAUNCHER', True), patch.object(support.shutil, 'which', return_value=str(self.program)):
            result = support.launch_readiness('work', self.root)
            self.assertFalse(result['ready'])
            self.assertIn('配置', result['message'])
            (self.home / 'profile.toml').write_text('model="test"\n')
            (self.home / 'config.toml').write_text('model="test"\n')
            self.assertIn('尚未初始化', support.launch_readiness('work', self.root)['message'])

    def create_report(self, name, paths, complete=True):
        report = self.data / 'audits' / name
        report.mkdir(parents=True)
        (report / 'summary.json').write_text(json.dumps({'created': '2026-10-06T10:00:00+08:00'}))
        (report / 'inventory.json').write_text(json.dumps({'files': [
            dict(path=str(path), mtime=path.stat().st_mtime) for path in paths]}))
        with (report / 'review-candidates.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['path', 'size', 'modified', 'recommendation', 'review_reasons', 'blockers'])
            writer.writeheader()
            for path in paths:
                writer.writerow(dict(path=str(path), size=path.stat().st_size,
                                     modified=dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds'),
                                     recommendation='可审阅候选', review_reasons=json.dumps(['测试会话']), blockers='[]'))
        if complete:
            (report / 'review.md').write_text('# complete\n')
        return report

    def test_review_skips_incomplete_new_report_and_excludes_changed_files(self):
        keep, changed, gone = (self.root / name for name in ('keep.jsonl', 'changed.jsonl', 'gone.jsonl'))
        for path in (keep, changed, gone):
            path.write_text('test\n')
        report = self.create_report('complete', [keep, changed, gone])
        incomplete = self.create_report('incomplete', [keep], complete=False)
        latest = (report / 'review-candidates.csv').stat().st_mtime + 10
        os.utime(incomplete / 'review-candidates.csv', (latest, latest))
        changed.write_text('changed content\n')
        gone.unlink()
        snapshot = support.review_snapshot()
        self.assertEqual(snapshot['report'], str(report))
        self.assertEqual(snapshot['candidates'], {str(keep): '测试会话'})
        self.assertEqual(snapshot['outdated'], 2)
        self.assertEqual(snapshot['created'], '2026-10-06T10:00:00+08:00')

    def test_audit_worker_propagates_selected_config_and_failures(self):
        completed = subprocess.CompletedProcess([], 0, stdout='', stderr='')
        with patch.object(support.subprocess, 'run', return_value=completed) as run, \
                patch.object(support, 'review_snapshot', return_value={'candidates': {}}):
            self.assertEqual(support.rebuild_review(), {'candidates': {}})
        args, kwargs = run.call_args
        self.assertIn('--refresh', args[0])
        self.assertIn('--output', args[0])
        self.assertEqual(kwargs['env']['AGENT_HISTORY_HOME'], str(self.data.resolve()))
        self.assertEqual(kwargs['env']['AGENT_HISTORY_SHARED_ROOT'], str(self.shared.resolve()))
        with patch.object(support.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, stderr='fixture failure')):
            with self.assertRaisesRegex(ValueError, 'fixture failure'):
                support.rebuild_review()

    def test_existing_audit_produces_readable_suggestions_without_modifying_source(self):
        self.configure_connection()
        sessions = self.home / 'sessions'
        sessions.mkdir()
        path = sessions / 'smoke.jsonl'
        records = [{'type': 'session_meta', 'timestamp': '2026-01-01T00:00:00Z',
                    'payload': {'id': 'smoke', 'cwd': str(self.home), 'source': 'cli'}},
                   {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': '1'}]}}]
        path.write_text(''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')
        stamp = dt.datetime.now().timestamp() - 2 * 86400
        os.utime(path, (stamp, stamp))
        before = path.read_bytes()
        report = self.data / 'audits' / 'generated'
        with contextlib.redirect_stdout(io.StringIO()):
            audit_history.audit(report, refresh=True)
        snapshot = support.review_snapshot()
        self.assertEqual(snapshot['report'], str(report))
        self.assertIn(str(path), snapshot['candidates'])
        self.assertIn('测试', snapshot['candidates'][str(path)])
        self.assertEqual(path.read_bytes(), before)
        with contextlib.closing(ah.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM imports').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
