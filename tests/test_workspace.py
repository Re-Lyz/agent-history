"""Exact project scope, native provenance, history data stays read-only."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_history as ah
from history_app import workspace
from history_app.paths import RuntimePaths
from setup_backend import atomic_json


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.paths = RuntimePaths(self.root / 'shared', self.root / 'data', self.root / 'prefix', self.root / 'user')
        self.paths.shared.mkdir()
        self.home = self.root / 'native'
        (self.home / 'sessions').mkdir(parents=True)
        atomic_json(self.paths.shared / 'connections.json', dict(connections={'one': dict(home=str(self.home), direct=True)}))
        self.patches = [patch.object(ah, 'ROOT', self.paths.shared), patch.object(ah, 'DATA', self.paths.data),
                        patch.object(ah, 'CLAUDE_HOMES', [])]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.project = str(self.root / 'project')
        self.file = self.home / 'sessions/first.jsonl'
        self.write(self.file, 'first', self.project, 40)
        self.write(self.home / 'sessions/second.jsonl', 'second', self.project + '-other', 2)
        with closing(ah.connect()) as db:
            ah.collect(db)

    def write(self, file, identity, project, count):
        rows = [dict(type='session_meta', payload=dict(id=identity, cwd=project, source='cli'))]
        rows += [dict(type='response_item', payload=dict(type='message', role='user',
                    content=[dict(type='input_text', text='Synthetic decision keyword ' + str(index))])) for index in range(count)]
        file.write_text(''.join(json.dumps(item) + '\n' for item in rows), encoding='utf-8')

    def test_context_has_native_byte_provenance_exact_scope_and_no_writes(self):
        before = self.file.read_bytes()
        db_before = (self.paths.data / 'history.db').read_bytes()
        with patch.object(ah, 'connect', side_effect=AssertionError('must not migrate or collect')):
            report = workspace.context(self.paths, self.project, 'keyword')
        self.assertTrue(report['materials'])
        self.assertEqual({item['session_id'] for item in report['materials']}, {'first'})
        for item in report['materials']:
            locator = item['locator']
            with Path(locator['path']).open('rb') as stream:
                stream.seek(locator['offset'])
                self.assertIn('Synthetic decision keyword', stream.readline().decode())
            self.assertIsInstance(locator['event'], int)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual((self.paths.data / 'history.db').read_bytes(), db_before)

    def test_context_cursor_continues_older_messages_and_rejects_other_scope(self):
        first = workspace.context(self.paths, self.project, 'keyword')
        self.assertIsNotNone(first['next_cursor'])
        second = workspace.context(self.paths, self.project, 'keyword', cursor=first['next_cursor'])
        old = {item['locator']['offset'] for item in first['materials']}
        self.assertFalse(old & {item['locator']['offset'] for item in second['materials']})
        with self.assertRaisesRegex(ValueError, '不属于'):
            workspace.context(self.paths, self.project + '-other', 'keyword', cursor=first['next_cursor'])

    def test_disabled_source_missing_history_and_mcp_do_not_restore_conversations(self):
        self.file.unlink()
        report = workspace.context(self.paths, self.project)
        self.assertEqual(len(report['unavailable']), 1)
        self.assertFalse(self.file.exists())
        atomic_json(self.paths.data / 'sources.json', dict(version=1, codex=[], claude=[]))
        report = workspace.context(self.paths, self.project, 'keyword')
        self.assertEqual(report['total_files'], 0)
        with patch.object(ah, 'collect', side_effect=AssertionError('MCP context is read-only')):
            report = json.loads(ah.mcp_call('project_context', dict(project=self.project)))
        self.assertEqual(report['materials'], [])
        replacement = self.root / 'replacement-home'
        (replacement / 'sessions').mkdir(parents=True)
        atomic_json(self.paths.shared / 'connections.json', dict(connections={'one': dict(home=str(replacement))}))
        atomic_json(self.paths.data / 'sources.json', dict(version=1, codex=['one'], claude=[]))
        self.assertEqual(workspace.context(self.paths, self.project)['total_files'], 0)
        self.assertEqual(workspace.snapshot(self.paths)['total'], 0)

    def test_context_rejects_a_cursor_after_rewrite(self):
        report = workspace.context(self.paths, self.project, 'keyword')
        self.write(self.file, 'first', self.project, 10)
        with self.assertRaisesRegex(ValueError, '分页后变化'):
            workspace.context(self.paths, self.project, 'keyword', cursor=report['next_cursor'])

    def test_workspace_preserves_unknown_old_task_state_and_project_pagination(self):
        self.write(self.home / 'sessions/physical-variant.jsonl', 'first', self.project + '-other', 3)
        with closing(ah.connect()) as db:
            ah.collect(db)
        all_projects = workspace.snapshot(self.paths)['projects']
        self.assertEqual({item['cwd']: item['versions'] for item in all_projects},
                         {self.project: 1, self.project + '-other': 2})
        atomic_json(self.paths.data / 'launch-jobs/old.json', dict(id='old', target='one', cwd=self.project,
                                                                state='interrupted', sync_state='failed'))
        report = workspace.snapshot(self.paths, limit=1)
        self.assertEqual(report['total'], 2)
        self.assertEqual(len(report['projects']), 1)
        self.assertEqual(report['jobs'][0]['state'], 'interrupted')
        self.assertIsNone(report['jobs'][0]['exit_code'])
        self.assertEqual(workspace.snapshot(self.paths, limit=1, offset=1)['total'], 2)
