import contextlib
import datetime
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_history as ah
import codex_import as ci
import history_ui as ui
import trash
from tests.test_codex_import import native_db


def rollout(sid, text, cwd, base=None):
    meta = {'id': sid, 'cwd': cwd, 'source': 'cli', 'timestamp': '2026-10-01T00:00:00Z'}
    if base:
        meta['history_base'] = {'thread_id': base}
    records = [{'type': 'session_meta', 'timestamp': '2026-10-01T00:00:00Z', 'payload': meta},
               {'type': 'response_item', 'timestamp': '2026-10-01T00:00:01Z', 'payload': {
                   'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}}]
    return ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records)


def age(path, seconds=3600):
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        from tests.platform_fixture import programs
        programs(self, self.root)
        self.a, self.b = self.root / 'a', self.root / 'b'
        for home in (self.a, self.b):
            (home / 'sessions').mkdir(parents=True)
            (home / 'profile.toml').write_text('model="m"\nmodel_provider="p"\n')
            (home / 'config.toml').write_text('model="m"\nmodel_provider="p"\n')
            native_db(home)
        shared = self.root / 'shared'
        shared.mkdir()
        (shared / 'connections.json').write_text(json.dumps({'connections': {
            'a': {'home': '../a'}, 'b': {'home': '../b'}, 'old': {'home': '../a', 'history_only': True}}}))
        self.claude = self.root / 'claude'
        (self.claude / 'projects').mkdir(parents=True)
        self.patches = [patch.object(ah, 'ROOT', shared), patch.object(ah, 'DATA', self.root / 'data'),
                        patch.object(ah, 'CLAUDE_HOMES', [self.claude])]
        for p in self.patches:
            p.start()
        self.db = ah.connect()
        self.cwd = str(self.root)
        self.path = self.a / 'sessions' / 'rollout-s1.jsonl'
        self.path.write_text(rollout('s1', '原始问题', self.cwd), encoding='utf-8')
        age(self.path)
        with contextlib.closing(sqlite3.connect(self.a / 'state_5.sqlite')) as db:
            db.execute("INSERT INTO threads(id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,"
                       "sandbox_policy,approval_mode) VALUES ('s1',?,1,1,'cli','p',?,'t','{}','x')",
                       (str(self.path), self.cwd))
            db.commit()
        (self.a / 'session_index.jsonl').write_text('{"id":"s1","thread_name":"t"}\n{"id":"keep"}\n')
        (self.a / 'history.jsonl').write_text('{"session_id":"s1","text":"q"}\n')
        ah.collect(self.db)

    def tearDown(self):
        self.db.close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def native_ids(self, home):
        with contextlib.closing(sqlite3.connect(home / 'state_5.sqlite')) as db:
            return [r[0] for r in db.execute('SELECT id FROM threads')]


class TrashTests(Fixture):
    def test_partial_restore_reports_each_location_and_preserves_existing_branch(self):
        copy = ci.import_session(self.db, ah.resolve(self.db, 's1'), self.a, 'b', self.b)
        age(copy['path'])
        ah.collect(self.db)
        manifest = trash.delete(self.db, [dict(tool='codex', source='a', path=str(self.path))]
                                + trash.related_copies(self.db, 'codex:s1'))
        self.assertEqual([item['kind'] for item in manifest['items']], ['main', 'import'])
        self.assertEqual(manifest['items'][0]['cwd'], self.cwd)
        branch = Path(copy['path'])
        branch.write_text('existing continued branch', encoding='utf-8')
        result = trash.restore(self.db, manifest['id'])
        self.assertEqual(result['restored'], 1)
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual([(item['source'], item['restored']) for item in result['outcomes']], [('a', True), ('b', False)])
        self.assertEqual(result['outcomes'][0]['path'], str(self.path.resolve()))
        self.assertIn('未覆盖', result['outcomes'][1]['reason'])
        self.assertEqual(branch.read_text(encoding='utf-8'), 'existing continued branch')
        self.assertTrue(self.path.exists())
        self.assertEqual([item['source'] for item in trash.entries()[0]['items']], ['b'])

    def test_delete_and_restore_round_trip(self):
        original = self.path.read_bytes()
        manifest = trash.delete(self.db, [dict(tool='codex', source='a', path=str(self.path))])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.native_ids(self.a), [])
        self.assertNotIn('"s1"', (self.a / 'session_index.jsonl').read_text())
        self.assertIn('keep', (self.a / 'session_index.jsonl').read_text())
        ah.collect(self.db)
        self.assertIsNone(self.db.execute("SELECT 1 FROM sessions WHERE key='codex:s1'").fetchone())
        expires = datetime.datetime.fromisoformat(manifest['expires_at']) - \
            datetime.datetime.fromisoformat(manifest['deleted_at'])
        self.assertEqual(expires.days, trash.RETENTION_DAYS)
        result = trash.restore(self.db, manifest['id'])
        self.assertEqual((result['restored'], result['errors']), (1, []))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.native_ids(self.a), ['s1'])
        self.assertIn('"s1"', (self.a / 'session_index.jsonl').read_text())
        self.assertIn('s1', (self.a / 'history.jsonl').read_text())
        ah.collect(self.db)
        self.assertEqual(ah.resolve(self.db, 's1')['kind'], 'main')
        self.assertEqual(trash.entries(), [])

    def test_with_copies_and_purge_after_expiry(self):
        copy = ci.import_session(self.db, ah.resolve(self.db, 's1'), self.a, 'b', self.b)
        age(copy['path'])
        ah.collect(self.db)
        related = trash.related_copies(self.db, 'codex:s1')
        self.assertEqual([(r['tool'], r['source']) for r in related], [('codex', 'b')])
        manifest = trash.delete(self.db, [dict(tool='codex', source='a', path=str(self.path))] + related)
        self.assertEqual(len(manifest['items']), 2)
        self.assertEqual(self.native_ids(self.b), [])
        # The import tombstone stays, so background sync does not recreate the copy.
        report = ci.sync(self.db, 'b', {'a': self.a, 'b': self.b}, days=3650, min_turns=1, settle_seconds=0)
        self.assertEqual(report['new'], [])
        self.assertEqual(trash.purge(now=datetime.datetime.now(datetime.timezone.utc)), [])
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=trash.RETENTION_DAYS + 1)
        self.assertEqual(trash.purge(now=later), [manifest['id']])
        self.assertFalse((ah.DATA / 'trash' / manifest['id']).exists())

    def test_refuses_busy_recent_and_fork_parents(self):
        fresh = self.a / 'sessions' / 'rollout-new.jsonl'
        fresh.write_text(rollout('new', '刚写入', self.cwd), encoding='utf-8')
        child = self.a / 'sessions' / 'rollout-child.jsonl'
        child.write_text(rollout('child', '分支', self.cwd, base='s1'), encoding='utf-8')
        age(child)
        ah.collect(self.db)
        with self.assertRaisesRegex(ValueError, '两分钟内'):
            trash.delete(self.db, [dict(tool='codex', source='a', path=str(fresh))])
        with self.assertRaisesRegex(ValueError, '分支'):
            trash.delete(self.db, [dict(tool='codex', source='a', path=str(self.path))])
        # Deleting the parent together with its fork is allowed.
        trash.delete(self.db, [dict(tool='codex', source='a', path=str(p)) for p in (self.path, child)])
        with ci.writer_lock(self.a, 'child') as held:
            self.assertTrue(held)
        self.assertEqual(len(trash.entries()), 1)
        with self.assertRaisesRegex(ValueError, '已知历史目录'):
            trash.delete(self.db, [dict(tool='codex', source='a', path=str(self.root / 'x.jsonl'))])

    def test_batch_skips_blocked_items_and_keeps_entries_separate(self):
        paths = {}
        for sid, seconds in (('b1', 3600), ('b2', 3600), ('busy', 0)):
            paths[sid] = self.a / 'sessions' / f'rollout-{sid}.jsonl'
            paths[sid].write_text(rollout(sid, sid, self.cwd), encoding='utf-8')
            if seconds:
                age(paths[sid], seconds)
        child = self.a / 'sessions' / 'rollout-child.jsonl'
        child.write_text(rollout('child', '分支', self.cwd, base='s1'), encoding='utf-8')
        os.utime(child)          # the fork is still being written, so it will be skipped
        ah.collect(self.db)
        group = lambda p: (p.stem, [dict(tool='codex', source='a', path=str(p))])
        # The fork's parent s1 is in the batch too; once the fork is skipped, s1 must stay.
        report = trash.delete_many(self.db, [group(paths['b1']), group(self.path), group(child),
                                             group(paths['busy']), group(paths['b2'])])
        self.assertEqual(report['deleted'], 2)
        self.assertEqual(len(report['entries']), 2)
        self.assertEqual(sorted(Path(s['path']).name for s in report['skipped']),
                         ['rollout-busy.jsonl', 'rollout-child.jsonl', 'rollout-s1.jsonl'])
        self.assertTrue(self.path.exists() and child.exists() and paths['busy'].exists())
        self.assertFalse(paths['b1'].exists() or paths['b2'].exists())
        trash.restore(self.db, report['entries'][0])
        self.assertEqual(len(trash.entries()), 1)

    def test_claude_session_with_sidecar(self):
        project = ah.claude_project_dir(self.cwd)
        project.mkdir(parents=True)
        path = project / 'c1.jsonl'
        path.write_text(json.dumps({'type': 'user', 'sessionId': 'c1', 'cwd': self.cwd, 'timestamp': 't',
                                    'message': {'role': 'user', 'content': '你好 Claude'}}) + '\n', encoding='utf-8')
        (project / 'c1' / 'subagents').mkdir(parents=True)
        age(path)
        (self.claude / 'history.jsonl').write_text('{"sessionId":"c1","display":"x"}\n{"sessionId":"z"}\n')
        ah.collect(self.db)
        manifest = trash.delete(self.db, [dict(tool='claude', source='claude', path=str(path))])
        self.assertFalse(path.exists() or (project / 'c1').exists())
        self.assertNotIn('c1', (self.claude / 'history.jsonl').read_text())
        trash.restore(self.db, manifest['id'])
        self.assertTrue(path.exists() and (project / 'c1' / 'subagents').is_dir())
        self.assertIn('c1', (self.claude / 'history.jsonl').read_text())


class LaunchPlanTests(Fixture):
    def test_same_id_previews_search_and_claude_conversion_use_selected_file(self):
        duplicate = self.b / 'sessions' / 'same-id.jsonl'
        duplicate.write_text(rollout('s1', '另一连接的独立新增内容', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        rows = {row['source']: row for row in ui.list_rows(self.db)}
        self.assertEqual([msg['text'] for msg in ui.file_messages(rows['a'])], ['原始问题'])
        self.assertEqual([msg['text'] for msg in ui.file_messages(rows['b'])], ['另一连接的独立新增内容'])
        self.assertEqual([row['source'] for row in ui.list_rows(self.db, query='独立新增内容')], ['b'])
        self.assertEqual([row['source'] for row in ui.list_rows(self.db, query='原始问题')], ['a'])
        converted = ui.claude_copy(self.db, rows['b'], self.cwd)
        path = ah.claude_project_dir(self.cwd) / (converted + '.jsonl')
        content = path.read_text(encoding='utf-8')
        self.assertIn('独立新增内容', content)
        self.assertNotIn('原始问题', content)
        # Reusing the same imported mapping must refresh from the newly selected file.
        self.assertEqual(ui.claude_copy(self.db, rows['a'], self.cwd), converted)
        content = path.read_text(encoding='utf-8')
        self.assertIn('原始问题', content)
        self.assertNotIn('独立新增内容', content)

    def test_preview_and_claude_conversion_include_only_bounded_ancestor_history(self):
        parent = self.a / 'sessions' / 'rollout-parent.jsonl'
        body = rollout('parent', '应继承的文字', self.cwd)
        body += json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                       'content': [{'type': 'input_text', 'text': '分支之后的文字不应继承'}]}}, ensure_ascii=False) + '\n'
        parent.write_text(body, encoding='utf-8')
        records = [json.loads(line) for line in rollout('child', '分支的新增文字', self.cwd).splitlines()]
        records[0]['payload']['history_base'] = dict(thread_id='parent', end_ordinal_exclusive=2)
        child = self.a / 'sessions' / 'rollout-child.jsonl'
        child.write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records), encoding='utf-8')
        ah.collect(self.db)
        row = next(row for row in ui.list_rows(self.db) if row['id'] == 'child')
        self.assertEqual([msg['text'] for msg in ui.file_messages(row)], ['应继承的文字', '分支的新增文字'])
        converted = ui.claude_copy(self.db, row, self.cwd)
        content = (ah.claude_project_dir(self.cwd) / (converted + '.jsonl')).read_text(encoding='utf-8')
        self.assertIn('应继承的文字', content)
        self.assertIn('分支的新增文字', content)
        self.assertNotIn('分支之后的文字', content)
        parent.unlink()
        with self.assertRaisesRegex(ValueError, 'ancestor'):
            ui.file_messages(row)

    def test_mismatched_native_path_and_deleted_target_cannot_be_resumed_silently(self):
        row = ui.list_rows(self.db)[0]
        with contextlib.closing(sqlite3.connect(self.a / 'state_5.sqlite')) as db:
            db.execute("UPDATE threads SET rollout_path='another-file' WHERE id='s1'")
            db.commit()
        with self.assertRaisesRegex(ValueError, '另一个文件'):
            self.plan(row, 'a')
        copy = ci.import_session(self.db, row, self.a, 'b', self.b)
        Path(copy['path']).unlink()
        self.assertEqual(self.plan(row, 'b')['kind'], 'codex-import')
        self.assertFalse(Path(copy['path']).exists())

    def test_project_choices_include_hidden_projects_beyond_list_limit(self):
        for index in range(501):
            sid = f'project-{index}'
            path = str(self.a / 'sessions' / f'{sid}.jsonl')
            cwd = str(self.root / sid)
            self.db.execute('INSERT INTO sessions(key,id,tool,source,path,cwd,kind) VALUES (?,?,?,?,?,?,?)',
                            (f'codex:{sid}', sid, 'codex', 'a', path, cwd, 'import'))
            self.db.execute('INSERT INTO files(path,source,session_key,mtime) VALUES (?,?,?,?)',
                            (path, 'a', f'codex:{sid}', index + 1))
        self.db.commit()
        self.assertEqual(len(ui.list_rows(self.db, include_imports=True)), 500)
        choices = ui.project_choices(self.db)
        self.assertIn(str(self.root / 'project-0'), choices)
        self.assertIn(str(self.root / 'project-500'), choices)
        self.assertIn(self.cwd, choices)

    def test_project_filter_uses_physical_file_directory_and_exact_match(self):
        other = str(self.root / 'missing-project')
        duplicate = self.b / 'sessions' / 'same-id.jsonl'
        duplicate.write_text(rollout('s1', '另一连接', other), encoding='utf-8')
        nested = self.a / 'sessions' / 'nested.jsonl'
        nested.write_text(rollout('nested', '子目录', self.cwd + '/nested'), encoding='utf-8')
        unknown = self.a / 'sessions' / 'unknown.jsonl'
        unknown.write_text(rollout('unknown', '未记录目录', ''), encoding='utf-8')
        ah.collect(self.db)
        rows = ui.list_rows(self.db, project=other)
        self.assertEqual([(r['source'], r['id']) for r in rows], [('b', 's1')])
        self.assertEqual([r['source'] for r in ui.list_rows(self.db, project=self.cwd)], ['a'])
        self.assertEqual([r['id'] for r in ui.list_rows(self.db, project='')], ['unknown'])
        self.assertEqual(len(ui.list_rows(self.db, query='missing-project', project=other)), 1)
        self.assertEqual(ui.list_rows(self.db, source='a', project=other), [])
        self.assertEqual(ui.list_rows(self.db, only={str(duplicate)}, project=self.cwd), [])
        choices = ui.project_choices(self.db)
        self.assertEqual(choices['全部项目'], None)
        self.assertEqual(choices['未记录目录'], '')
        self.assertEqual(choices[other], other)
        self.assertIn(self.cwd + '/nested', choices)

    def plan(self, row, target, cwd=None):
        return ui.launch_plan(self.db, row, target, cwd or self.cwd)

    def test_matrix(self):
        rows = {r['source']: r for r in ui.list_rows(self.db)}
        original = rows['a']
        self.assertEqual(ui.launch_targets(), ['a', 'b', 'claude'])
        self.assertEqual(self.plan(original, 'a')['args'], ['a', '-Session', 's1'])
        self.assertEqual(self.plan(original, 'b')['args'], ['b', '-From', 'a', '-Session', 's1'])
        self.assertEqual(self.plan(original, 'claude')['kind'], 'claude-import')
        with self.assertRaisesRegex(ValueError, '目录'):
            self.plan(original, 'a', str(self.root / 'missing'))
        copy = ci.import_session(self.db, ah.resolve(self.db, 's1'), self.a, 'b', self.b)
        ah.collect(self.db)
        copy_row = next(r for r in ui.list_rows(self.db, include_imports=True) if r['id'] == copy['session'])
        self.assertEqual(self.plan(copy_row, 'b')['args'], ['b', '-Session', copy['session']])
        # Never silently replace the selected copy/branch with its original.
        self.assertEqual(self.plan(copy_row, 'a')['kind'], 'codex-import')
        sid = ui.claude_copy(self.db, ah.resolve(self.db, 's1'), self.cwd)
        self.assertEqual(ui.claude_copy(self.db, ah.resolve(self.db, 's1'), self.cwd), sid)
        ah.collect(self.db)
        claude_row = next(r for r in ui.list_rows(self.db, include_imports=True) if r['id'] == sid)
        self.assertEqual(self.plan(claude_row, 'claude')['kind'], 'claude')
        with self.assertRaisesRegex(ValueError, '原目录'):
            self.plan(claude_row, 'claude', str(self.a))
        self.assertEqual(self.plan(claude_row, 'b')['kind'], 'claude-codex-import')

    def test_search_and_terminal_command(self):
        self.assertEqual([r['id'] for r in ui.list_rows(self.db, query='原始')], ['s1'])
        self.assertEqual([r['id'] for r in ui.list_rows(self.db, only={str(self.path)})], ['s1'])
        with patch.object(ui.shutil, 'which', return_value='wt.exe'):
            cmd, cwd = ui.terminal_command('C:/x', ['-Command', 'a; b'])
        if os.name == 'nt':
            self.assertEqual(cmd[:4], ['wt.exe', 'new-tab', '-d', 'C:/x'])
            self.assertEqual(cmd[-1], r'a\; b')
        else:
            self.assertEqual(cmd[-2:], ['-Command', 'a; b'])
            self.assertEqual(cwd, 'C:/x')


if __name__ == '__main__':
    unittest.main()
