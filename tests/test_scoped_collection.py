"""Exact-path refresh avoids unrelated I/O while respecting source boundaries."""
import json
from pathlib import Path
from unittest.mock import patch

import agent_history as ah
import codex_import as ci
from tests.test_trash import Fixture, rollout


class ScopedCollectionTests(Fixture):
    def test_targeted_refresh_never_enumerates_or_reads_unrelated_files(self):
        other = self.a / 'sessions' / 'unrelated.jsonl'
        other.write_text(rollout('unrelated', 'old content', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        other.write_text(rollout('unrelated', 'new content', self.cwd), encoding='utf-8')
        self.path.write_text(rollout('s1', 'target changed', self.cwd), encoding='utf-8')
        first_meta = ci.first_meta
        def owned(path):
            self.assertNotEqual(Path(path), other)
            return first_meta(path)
        with patch.object(Path, 'rglob', side_effect=AssertionError('Full directory walk')), \
                patch.object(Path, 'glob', side_effect=AssertionError('Full directory walk')), \
                patch.object(ci, 'first_meta', side_effect=owned):
            ah.collect(self.db, only_paths={self.path})
        self.assertEqual([r['text'] for r in ah.messages(self.db, 'codex:s1')], ['target changed'])
        self.assertEqual([r['text'] for r in ah.messages(self.db, 'codex:unrelated')], ['old content'])
        ah.collect(self.db)
        self.assertEqual([r['text'] for r in ah.messages(self.db, 'codex:unrelated')], ['new content'])

    def test_outside_paths_and_disabled_connections_are_not_indexed(self):
        outside = self.root / 'outside.jsonl'
        outside.write_text(rollout('outside', 'outside source', self.cwd), encoding='utf-8')
        disabled = self.b / 'sessions' / 'disabled.jsonl'
        disabled.write_text(rollout('disabled', 'disabled source', self.cwd), encoding='utf-8')
        with patch('setup_backend.load_sources', return_value=dict(version=1, codex=['a'], claude=[])):
            ah.collect(self.db, only_paths={outside, disabled, self.path})
        self.assertIsNone(self.db.execute('SELECT 1 FROM files WHERE path=?', (str(outside),)).fetchone())
        self.assertIsNone(self.db.execute('SELECT 1 FROM files WHERE path=?', (str(disabled),)).fetchone())
        self.assertEqual(ah.resolve(self.db, 's1')['source'], 'a')

    def test_claude_and_archived_codex_follow_the_same_discovery_layout(self):
        archived = self.a / 'archived_sessions' / 'archived.jsonl'
        archived.parent.mkdir()
        archived.write_text(rollout('archived', 'archived message', self.cwd), encoding='utf-8')
        valid = self.claude / 'projects' / 'project' / 'claude.jsonl'
        valid.parent.mkdir()
        record = dict(type='user', sessionId='synthetic-claude', cwd=self.cwd,
                      message=dict(content='Claude message'), uuid='synthetic-message')
        valid.write_text(json.dumps(record) + '\n', encoding='utf-8')
        nested = valid.parent / 'nested' / 'ignored.jsonl'
        nested.parent.mkdir()
        nested.write_bytes(valid.read_bytes())
        with patch.object(Path, 'rglob', side_effect=AssertionError('Full walk')), \
                patch.object(Path, 'glob', side_effect=AssertionError('Full walk')):
            ah.collect(self.db, only_paths={archived, valid, nested})
        for path in (archived, valid):
            self.assertIsNotNone(self.db.execute('SELECT 1 FROM files WHERE path=?', (str(path),)).fetchone())
        self.assertIsNone(self.db.execute('SELECT 1 FROM files WHERE path=?', (str(nested),)).fetchone())

    def test_large_exact_path_scope_is_batched_and_does_not_drop_other_rows(self):
        other = self.a / 'sessions' / 'other.jsonl'
        other.write_text(rollout('other', 'kept', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        paths = {self.a / 'sessions' / f'missing-{i}.jsonl' for i in range(1001)} | {self.path}
        ah.collect(self.db, only_paths=paths)
        self.assertIsNotNone(ah.resolve(self.db, 's1'))
        self.assertIsNotNone(ah.resolve(self.db, 'other'))

    def test_deleted_target_is_forgotten_without_recreating_it_or_dropping_other_files(self):
        other = self.a / 'sessions' / 'other.jsonl'
        other.write_text(rollout('other', 'kept', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        self.path.unlink()
        ah.collect(self.db, only_paths={self.path})
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.db.execute('SELECT 1 FROM files WHERE path=?', (str(self.path),)).fetchone())
        self.assertIsNotNone(ah.resolve(self.db, 'other'))
