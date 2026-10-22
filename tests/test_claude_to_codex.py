"""Claude conversion lifecycle, using only synthetic transcripts and native schemas."""
import contextlib
import json
from pathlib import Path
import sqlite3
import os
import subprocess
import sys
import shutil
from unittest.mock import patch

import agent_history as ah
import claude_to_codex as conversion
import codex_import as ci
import launch_backend as backend
import launch_profiles as profiles
import trash
from tests.test_trash import Fixture, age


class ClaudeToCodexTests(Fixture):
    def setUp(self):
        super().setUp()
        self.source = ah.claude_project_dir(self.cwd) / 'claude-one.jsonl'
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.records = [dict(type='user', sessionId='claude-one', cwd=self.cwd, uuid='u1',
                             timestamp='2026-10-01T00:00:00Z', message=dict(content='中文问题\n' + 'x' * 7000)),
                        dict(type='assistant', sessionId='claude-one', cwd=self.cwd, uuid='a1',
                             timestamp='2026-10-01T00:00:01Z', message=dict(content=[
                                 dict(type='text', text='完整中文回答'),
                                 dict(type='tool_use', name='Bash', input={'command': 'must-not-execute'}),
                                 dict(type='thinking', thinking='private reasoning'),
                                 dict(type='image', source={'data': 'not-an-image'}),
                                 dict(type='document', source={'data': 'not-a-document'})])),
                        dict(type='user', sessionId='claude-one', cwd=self.cwd, uuid='u2',
                             timestamp='2026-10-01T00:00:02Z', message=dict(content=[
                                 dict(type='tool_result', content='tool output\n' + 'y' * 6000)]))]
        self.write_source()

    def write_source(self):
        self.source.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in self.records), encoding='utf-8')
        ah.collect(self.db)
        self.session = dict(ah.resolve(self.db, 'claude:claude-one'))

    def import_one(self, **kwargs):
        return ci.import_session(self.db, self.session, None, 'b', self.b, **kwargs)

    def test_preview_and_import_preserve_full_text_and_never_replay_tools(self):
        original = self.source.read_bytes()
        preview = self.import_one(dry_run=True)
        self.assertFalse(Path(preview['path']).exists())
        report = preview['conversion']
        self.assertEqual((report['text_blocks'], report['tool_summaries']), (2, 2))
        self.assertEqual((report['images'], report['attachments'], report['reasoning_blocks']), (1, 1, 1))
        self.assertEqual(report['truncated_tool_summaries'], 1)
        imported = self.import_one()
        self.assertEqual(self.source.read_bytes(), original)
        target = Path(imported['path'])
        records = list(ci.records(target))
        self.assertNotEqual(imported['session'], self.session['id'])
        self.assertEqual(records[0]['payload']['history_mode'], 'legacy')
        messages = [r['payload'] for r in records if r['type'] == 'response_item']
        self.assertTrue(all(item['type'] == 'message' for item in messages))
        body = target.read_text(encoding='utf-8')
        self.assertIn('x' * 7000, body)
        self.assertIn('未重放', body)
        self.assertNotIn('private reasoning', body)
        self.assertNotIn('not-an-image', body)
        with contextlib.closing(ci.readonly(ci.state_path(self.b))) as db:
            native = db.execute('SELECT * FROM threads WHERE id=?', (imported['session'],)).fetchone()
            self.assertEqual(native['model'], 'm')
            self.assertEqual(native['cwd'], self.cwd)
        ah.collect(self.db)
        indexed = ah.resolve(self.db, imported['session'])
        self.assertEqual((indexed['kind'], indexed['imported_from']), ('import', self.session['key']))
        self.assertEqual(self.import_one()['status'], 'unchanged')

    def test_refresh_continued_copy_and_target_writer_lock(self):
        first = self.import_one()
        self.records.append(dict(type='user', sessionId='claude-one', cwd=self.cwd, uuid='u3',
                                 timestamp='2026-10-01T00:00:03Z', message=dict(content='后来增加的问题')))
        self.write_source()
        with ci.writer_lock(self.b, first['session']) as locked:
            self.assertTrue(locked)
            self.assertEqual(self.import_one()['status'], 'busy')
        refreshed = self.import_one()
        self.assertEqual(refreshed['status'], 'refreshed')
        self.assertEqual(refreshed['session'], first['session'])
        path = Path(first['path'])
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(type='event_msg', payload=dict(type='user_message', message='已接续分支'))) + '\n')
        continued = path.read_bytes()
        forked = self.import_one()
        self.assertEqual(forked['status'], 'forked')
        self.assertNotEqual(forked['session'], first['session'])
        self.assertEqual(path.read_bytes(), continued)

    def test_deleted_import_stays_deleted_and_recycle_bin_restores_mapping(self):
        first = self.import_one()
        target = Path(first['path'])
        age(target)
        ah.collect(self.db)
        entry = trash.delete(self.db, [dict(tool='codex', source='b', path=str(target))], label='fixture')
        self.assertEqual(self.import_one()['status'], 'deleted')
        trash.restore(self.db, entry['id'])
        self.assertEqual(self.import_one()['session'], first['session'])
        self.assertEqual(len(trash.related_copies(self.db, self.session['key'])), 1)

    def test_conversion_detects_changed_missing_and_wrong_source_without_publishing(self):
        snapshot = conversion.snapshot
        def change_source(*args):
            result = snapshot(*args)
            with self.source.open('a', encoding='utf-8') as stream:
                stream.write('{}\n')
            return result
        with patch.object(conversion, 'snapshot', side_effect=change_source):
            self.assertEqual(self.import_one()['status'], 'busy')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM codex_imports').fetchone()[0], 0)
        self.assertEqual(list((self.b / 'sessions').rglob('*.jsonl')), [])
        self.records[0]['sessionId'] = 'wrong'
        self.source.write_text(''.join(json.dumps(r) + '\n' for r in self.records), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'session ID'):
            self.import_one()
        self.source.unlink()
        with self.assertRaises(OSError):
            self.import_one()

    def test_codex_profile_converts_claude_and_uses_new_native_id_and_chosen_cwd(self):
        chosen = self.root / '中文 空格'
        chosen.mkdir()
        profiles.save([dict(id='native', name='目标', type='codex', connection='b')])
        plan = backend.launch_plan(self.db, self.session, 'profile:native', str(chosen))
        self.assertEqual(plan['kind'], 'claude-codex-import')
        self.assertIn('图片与附件不迁移', plan['preview'])
        import handoff_lifecycle
        with patch.object(handoff_lifecycle, 'claude_running', return_value=False):
            prepared = backend.prepare_launch(self.db, self.session, 'profile:native', str(chosen))
        self.assertNotEqual(prepared['session'], self.session['id'])
        if os.name == 'nt':
            self.assertEqual(prepared['inner'][2:4], ['b', '-Session'])
            self.assertEqual(prepared['inner'][4], prepared['session'])
        else:
            self.assertEqual(prepared['inner'][-2:], ['resume', prepared['session']])
            self.assertEqual(prepared['env']['CODEX_HOME'], str(self.b))
        row = ci.source_row(self.b, prepared['session'])
        self.assertEqual(row['cwd'], str(chosen.resolve()))
        # No automatic Claude -> Codex synchronization or conversion loop.
        ah.collect(self.db)
        sync = ci.sync(self.db, 'a', dict(ah.codex_homes()), min_turns=1, settle_seconds=0)
        self.assertEqual(sync['new'], [])

    def test_failed_native_schema_leaves_no_import_file(self):
        with contextlib.closing(sqlite3.connect(ci.state_path(self.b))) as db:
            db.execute('ALTER TABLE threads ADD COLUMN unsupported TEXT NOT NULL')
            db.commit()
        with self.assertRaisesRegex(ValueError, 'Unsupported required'):
            self.import_one()
        self.assertEqual(list((self.b / 'sessions').rglob('*.jsonl')), [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM codex_imports').fetchone()[0], 0)

    def test_duplicate_uuid_keeps_latest_text_and_explicit_tool_prefix_resolves_correct_source(self):
        self.records.append(dict(self.records[0], message=dict(content='最后完整版本')))
        self.write_source()
        imported = self.import_one()
        body = Path(imported['path']).read_text(encoding='utf-8')
        self.assertIn('最后完整版本', body)
        self.assertNotIn('x' * 7000, body)
        self.assertEqual(imported['conversion']['duplicate_records'], 1)
        self.db.execute('INSERT INTO sessions(key,id,tool,kind) VALUES (?,?,?,?)',
                        ('codex:claude-one', 'claude-one', 'codex', 'main'))
        self.db.commit()
        self.assertEqual(ah.resolve(self.db, 'claude:claude-one')['tool'], 'claude')
        self.assertEqual(ah.resolve(self.db, 'codex:claude-one')['tool'], 'codex')

    def test_nested_tool_result_attachments_are_omitted_and_counted(self):
        self.records[2]['message']['content'][0]['content'] = [dict(type='text', text='工具文本仍保留'),
            dict(type='image', source={'data': 'private-nested-image'}),
            dict(type='document', source={'data': 'private-nested-document'}),
            dict(type='thinking', thinking='private-nested-reasoning')]
        self.write_source()
        imported = self.import_one()
        report = imported['conversion']
        self.assertEqual((report['images'], report['attachments'], report['reasoning_blocks']), (2, 2, 2))
        body = Path(imported['path']).read_text(encoding='utf-8')
        self.assertIn('工具文本仍保留', body)
        self.assertNotIn('private-nested', body)

    def test_import_codex_cli_accepts_claude_source_and_dry_run_does_not_publish(self):
        source = self.root / '.claude' / 'projects' / self.source.parent.name / self.source.name
        source.parent.mkdir(parents=True)
        shutil.copyfile(self.source, source)
        env = {**os.environ, 'USERPROFILE': str(self.root), 'AGENT_HISTORY_HOME': str(self.root / 'cli-data'),
               'AGENT_HISTORY_SHARED_ROOT': str(ah.ROOT), 'PYTHONIOENCODING': 'utf-8'}
        if os.name != 'nt':
            env['HOME'] = str(self.root)
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'agent_history.py'), 'import-codex', 'claude-one', '--source', 'claude', '--to', 'b']
        def call(*extra):
            result = subprocess.run(command + list(extra), env=env, capture_output=True, text=True,
                                    encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        preview = call('--dry-run')
        self.assertIn('conversion', preview)
        self.assertFalse(Path(preview['path']).exists())
        imported = call('--cwd', self.cwd)
        self.assertNotEqual(imported['session'], 'claude-one')
        self.assertTrue(Path(imported['path']).is_file())
        self.assertEqual(call('--cwd', self.cwd)['status'], 'unchanged')
