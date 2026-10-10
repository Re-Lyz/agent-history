"""Launch configuration and adapter contracts, with synthetic history and no model turns."""
import base64
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import agent_history as ah
import codex_import as ci
import launch_backend as backend
import launch_profiles as profiles
import launch_runner
import ui_support as support
from tests.test_trash import Fixture


class LaunchBackendTests(Fixture):
    def test_explicit_copy_handoff_preserves_continuation_and_bulk_sync_excludes_it(self):
        row = dict(ah.resolve(self.db, 's1'))
        first = ci.import_session(self.db, row, self.a, 'b', self.b)
        path = Path(first['path'])
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(type='response_item', payload=dict(type='message', role='user',
                content=[dict(type='input_text', text='副本中的新增问题') ])), ensure_ascii=False) + '\n')
        ah.collect(self.db)
        selected = dict(ah.resolve(self.db, first['session']))
        prepared = backend.prepare_launch(self.db, selected, 'a', self.cwd)
        self.assertNotIn(prepared['session'], ('s1', first['session']))
        mapped = self.db.execute('SELECT dst_path FROM codex_imports WHERE dst_key=?',
                                 ('codex:' + prepared['session'],)).fetchone()
        content = Path(mapped['dst_path']).read_text(encoding='utf-8')
        self.assertIn('副本中的新增问题', content)
        self.assertIn('原始问题', content)
        with self.assertRaisesRegex(ValueError, 'original Codex'):
            ci.import_session(self.db, selected, self.b, 'a', self.a)
        self.assertNotIn('副本中的新增问题', self.path.read_text(encoding='utf-8'))

    def profile(self, **changes):
        return dict(id='work', name='我的命令', type='custom', program=sys.executable,
                    args=[], resume_args=['--session', '{session_id}'], resume_tools=['codex'],
                    **changes)

    def test_profile_validation_atomic_save_and_invalid_entries_are_isolated(self):
        raw = self.profile()
        profiles.save([raw])
        before = profiles.config_path().read_bytes()
        for bad in (dict(raw, args='shell text'), dict(raw, resume_args=['{session_id.__class__}']),
                    dict(raw, resume_args=['{{session_id}}']), dict(raw, env={'bad-name': 'x'}),
                    dict(raw, cwd_policy='fixed', cwd='relative'), dict(raw, enabled='yes')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                profiles.save([bad])
            self.assertEqual(profiles.config_path().read_bytes(), before)
        with patch.object(profiles.os, 'replace', side_effect=PermissionError('fixture')):
            with self.assertRaises(PermissionError):
                profiles.save([dict(raw, name='changed')])
        self.assertEqual(profiles.config_path().read_bytes(), before)
        self.assertEqual(len(list(profiles.config_path().parent.glob('tmp*'))), 0)
        profiles.config_path().write_text(json.dumps(dict(version=1, profiles=[raw, dict(raw, id='bad', args='bad')])) )
        valid, errors = profiles.load()
        self.assertEqual(list(valid), ['profile:work'])
        self.assertIn('args', errors[0])
        self.assertEqual(backend.launch_targets(), ['a', 'b', 'claude', 'profile:work'])

    def test_custom_resume_uses_exact_id_cwd_and_environment_without_leaking_values(self):
        profile = self.profile()
        profile.update(args=['中文 空格', 'a"b', "a'b", '$(not-a-command); & | %'],
                       env={'API_TOKEN': {'from_env': 'FIXTURE_TOKEN'}}, cwd_policy='fixed', cwd=self.cwd)
        profiles.save([profile])
        row = dict(ah.resolve(self.db, 's1'))
        with patch.dict(os.environ, FIXTURE_TOKEN='fixture-private-value'):
            plan = backend.launch_plan(self.db, row, 'profile:work', str(self.root / 'missing'))
            prepared = backend.prepare_launch(self.db, row, 'profile:work', self.cwd)
        self.assertNotIn('fixture-private-value', plan['preview'])
        self.assertEqual(prepared['env']['API_TOKEN'], 'fixture-private-value')
        self.assertEqual(prepared['env']['AGENT_HISTORY_JOB_ID'], prepared['job'])
        self.assertEqual(prepared['session'], 's1')
        self.assertEqual(prepared['cwd'], str(self.root.resolve()))
        if os.name == 'nt':
            code = base64.b64decode(prepared['inner'][1]).decode('utf-16le')
            payload = json.loads(base64.b64decode(code.rsplit("'", 2)[1]).decode('utf-8'))
            self.assertEqual(payload['args'], profile['args'] + ['--session', 's1'])
        else:
            self.assertEqual(prepared['inner'], [sys.executable, *profile['args'], '--session', 's1'])
        with patch.object(backend.subprocess, 'Popen') as popen, patch.object(backend.shutil, 'which', return_value='wt.exe'):
            backend.start_terminal(prepared['cwd'], prepared['inner'], env=prepared['env'])
        self.assertEqual(popen.call_args.args[0][0], 'powershell.exe' if os.name == 'nt' else 'gnome-terminal')
        self.assertEqual(popen.call_args.kwargs['env']['API_TOKEN'], 'fixture-private-value')

    def test_typed_codex_profile_prepares_target_id_and_new_only_profile_rejects_resume(self):
        profiles.save([dict(id='native', name='目标 Codex', type='codex', connection='b'),
                       dict(id='new', name='新命令', type='custom', program=sys.executable)])
        row = dict(ah.resolve(self.db, 's1'))
        prepared = backend.prepare_launch(self.db, row, 'profile:native', self.cwd)
        self.assertNotEqual(prepared['session'], 's1')
        if os.name == 'nt':
            self.assertEqual(prepared['inner'][2:4], ['b', '-Session'])
            self.assertEqual(prepared['inner'][4], prepared['session'])
            self.assertIn('-NoSync', prepared['inner'])
        else:
            self.assertEqual(prepared['inner'][-2:], ['resume', prepared['session']])
            self.assertEqual(prepared['env']['CODEX_HOME'], str(self.b))
        with self.assertRaisesRegex(ValueError, '新会话'):
            backend.launch_plan(self.db, row, 'profile:new', self.cwd)
        new = backend.prepare_new('profile:new', self.cwd)
        self.assertIsNone(new['session'])
        stale = backend.transcript_stamp(row)
        with Path(row['path']).open('a', encoding='utf-8') as stream:
            stream.write('{}\n')
        with self.assertRaisesRegex(ValueError, '已变化'):
            backend.prepare_launch(self.db, row, 'profile:native', self.cwd, stale)

    def test_typed_claude_and_overridden_codex_program_show_actual_resume_ids(self):
        profiles.save([dict(id='claude-profile', name='Claude', type='claude', args=['--model', 'example-model']),
                       dict(id='codex-profile', name='Codex', type='codex', connection='a',
                            program=sys.executable, resume_args=['--session', '{session_id}'])])
        row = dict(ah.resolve(self.db, 's1'))
        plan = backend.launch_plan(self.db, row, 'profile:claude-profile', self.cwd)
        self.assertEqual(plan['kind'], 'claude-import')
        self.assertIn('example-model', plan['preview'])
        self.assertIn('图片', plan['preview'])
        prepared = backend.prepare_launch(self.db, row, 'profile:claude-profile', self.cwd)
        self.assertNotEqual(prepared['session'], row['id'])
        plan = backend.launch_plan(self.db, row, 'profile:codex-profile', self.cwd)
        self.assertIn("'s1'" if os.name == 'nt' else 's1', plan['preview'])
        self.assertNotIn('<目标会话ID>', plan['preview'])
        prepared = backend.prepare_launch(self.db, row, 'profile:codex-profile', self.cwd)
        self.assertEqual(prepared['env']['CODEX_HOME'], str(self.a))
        profiles.save([dict(id='readonly', name='invalid', type='codex', connection='old')])
        with self.assertRaisesRegex(ValueError, '可启动'):
            backend.launch_plan(self.db, row, 'profile:readonly', self.cwd)

    def test_check_is_read_only_and_reports_missing_environment_and_program(self):
        profile = self.profile()
        profile.update(program='missing-fixture-program', env={'TOKEN': {'from_env': 'MISSING_FIXTURE_TOKEN'}})
        profiles.save([profile])
        with patch.object(subprocess, 'Popen') as popen, patch.object(backend.shutil, 'which', return_value=None):
            ready = support.launch_readiness('profile:work')
        self.assertFalse(ready['ready'])
        self.assertIn('MISSING_FIXTURE_TOKEN', ready['message'])
        self.assertIn('missing-fixture-program', ready['message'])
        popen.assert_not_called()
        if os.name != 'nt':
            script = self.root / 'not-executable'
            script.write_text('#!/bin/sh\nexit 0\n'); script.chmod(0o644)
            profile.update(program=str(script), env={})
            report=backend.check_profile(profile)
            self.assertFalse(report['ready'])
            self.assertIn('执行权限',report['message'])

    def test_cli_profile_edit_plan_prepare_and_new_plan_are_reviewable(self):
        config = self.root / 'profile.json'
        config.write_text(json.dumps(dict(id='native', name='Codex', type='codex', connection='b')))
        env = {**os.environ, 'USERPROFILE': str(self.root), 'AGENT_HISTORY_HOME': str(ah.DATA),
               'AGENT_HISTORY_SHARED_ROOT': str(ah.ROOT), 'PYTHONIOENCODING': 'utf-8'}
        def call(*args):
            result = subprocess.run([sys.executable, str(Path(profiles.__file__)), *args], env=env,
                                    capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        call('put', str(config))
        self.assertEqual(json.loads(call('list'))['profiles'][0]['id'], 'native')
        plan = json.loads(call('plan', 'native', '--session', 's1', '--source', 'a', '--cwd', self.cwd))
        self.assertEqual(plan['kind'], 'codex-import')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM codex_imports').fetchone()[0], 0)
        prepared = json.loads(call('prepare', 'native', '--session', 's1', '--source', 'a', '--cwd', self.cwd))
        self.assertNotEqual(prepared['session'], 's1')
        self.assertNotIn('env', prepared)
        self.assertTrue(json.loads(call('new-plan', 'native', '--cwd', self.cwd))['program'].endswith('codex-switch.ps1' if os.name == 'nt' else 'codex'))
        call('remove', 'native')
        self.assertEqual(profiles.load(), ({}, []))

    def test_sorting_filters_all_matches_before_paging(self):
        import history_ui
        for index in range(505):
            sid = f'fixture-{index:03}'
            self.db.execute('INSERT INTO sessions(key,id,tool,source,path,kind,turns,title) VALUES (?,?,?,?,?,?,?,?)',
                            ('codex:' + sid, sid, 'codex', 'a', '/missing/' + sid, 'main', index, sid))
            self.db.execute('INSERT INTO files(path,source,session_key,mtime) VALUES (?,?,?,?)',
                            ('/missing/' + sid, 'a', 'codex:' + sid, index + 1))
        self.db.commit()
        rows = history_ui.list_rows(self.db, sort_by='time', descending=False, limit=2)
        self.assertEqual([row['id'] for row in rows], ['fixture-000', 'fixture-001'])
        rows = history_ui.list_rows(self.db, sort_by='turns', descending=True, limit=2)
        self.assertEqual([row['id'] for row in rows], ['fixture-504', 'fixture-503'])
        rows = history_ui.list_rows(self.db, query='fixture-00', sort_by='title', descending=True, limit=2)
        self.assertEqual([row['id'] for row in rows], ['fixture-009', 'fixture-008'])

    @unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows PowerShell required.')
    def test_real_terminal_command_preserves_executable_and_powershell_script_arguments(self):
        output = self.root / 'argv.json'
        capture = self.root / 'capture.py'
        capture.write_text('import json,sys\nfrom pathlib import Path\n'
                           'Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:],ensure_ascii=False),encoding="utf-8")\n')
        values = ['中文 空格', 'a"b', "a'b", '$(not-a-command); & | % !', '', 'trailing\\']
        command = backend.program_command(sys.executable, [str(capture), str(output), *values])
        result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', *command], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text(encoding='utf-8')), values)
        script = self.root / 'capture.ps1'
        script.write_text('param([string]$OutputPath)\n'
                          '$args | ConvertTo-Json | Set-Content -LiteralPath $OutputPath -Encoding UTF8\n')
        command = backend.program_command(str(script), [str(output), *values])
        result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', *command], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text(encoding='utf-8-sig')), values)
        with self.assertRaisesRegex(ValueError, 'Batch'):
            launch_runner.command('fixture.cmd', ['x & y'])

    @unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows PowerShell required.')
    def test_codex_profile_forwards_real_array_and_named_resume_parameters(self):
        output = self.root / 'named.json'
        script = self.root / 'codex-switch.ps1'
        script.write_text('param([Parameter(Position=0)][string]$Target,[string]$Session,[switch]$NoSync,[string[]]$CodexArgs)\n'
                          '@{target=$Target;session=$Session;noSync=[bool]$NoSync;args=$CodexArgs} | '
                          'ConvertTo-Json | Set-Content -LiteralPath $env:FIXTURE_OUTPUT -Encoding UTF8\n')
        values = ['--model', '中文 空格', 'a"b', "a'b", 'literal; & | %']
        profiles.save([dict(id='native', name='native', type='codex', connection='b', args=values)])
        row = dict(ah.resolve(self.db, 's1'))
        with patch.object(backend.Path, 'home', return_value=self.root):
            prepared = backend.prepare_launch(self.db, row, 'profile:native', self.cwd)
            new = backend.prepare_new('profile:native', self.cwd)
        env = {**os.environ, 'FIXTURE_OUTPUT': str(output)}
        result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', *prepared['inner']], env=env,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(output.read_text(encoding='utf-8-sig'))
        self.assertEqual((data['target'], data['session'], data['noSync']), ('b', prepared['session'], True))
        self.assertEqual(data['args'], values)
        result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', *new['inner']], env=env,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(output.read_text(encoding='utf-8-sig'))
        self.assertEqual(data['session'], '')
        self.assertEqual(data['args'], values)


class HistorySortTests(unittest.TestCase):
    def test_numeric_time_casefold_stable_ties_and_missing_values(self):
        rows = [dict(id='b', source='same', title='中文', turns=10, modified=10),
                dict(id='a', source='same', title='abc', turns=2, modified=2),
                dict(id='c', source='same', title='ABC', turns=2, modified=3),
                dict(id='null', source='', title=None, turns=None, modified=None)]
        self.assertEqual([r['id'] for r in support.sort_history(rows, 'turns', False)], ['a', 'c', 'b', 'null'])
        self.assertEqual([r['id'] for r in support.sort_history(rows, 'turns', True)], ['b', 'a', 'c', 'null'])
        self.assertEqual([r['id'] for r in support.sort_history(rows, 'time', False)], ['a', 'c', 'b', 'null'])
        self.assertEqual([r['id'] for r in support.sort_history(rows, 'title', False)], ['a', 'c', 'b', 'null'])
        self.assertEqual(support.clean_preferences(dict(sort=dict(column='turns', descending=False)))['sort'],
                         dict(column='turns', descending=False))
        self.assertEqual(support.clean_preferences(dict(sort=dict(column='unknown', descending='bad')))['sort'],
                         dict(column='time', descending=True))
