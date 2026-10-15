"""Explicit handoff round trips preserve originals, baselines and concurrent branches."""
import json
from pathlib import Path
from unittest.mock import patch

import agent_history as ah
import conversation_store as store
import launch_backend as backend
import trash
from tests.test_trash import Fixture, age


class ConversationTests(Fixture):
    def test_logical_launch_does_not_ignore_an_unreadable_related_version(self):
        original = dict(ah.resolve(self.db, 's1'))
        copied = self.launch(original, 'b')
        actual_read = store.read_native
        def unreadable_copy(row):
            if row['path'] == copied['path']:
                raise OSError('File temporarily unavailable')
            return actual_read(row)
        with patch.object(store, 'read_native', side_effect=unreadable_copy):
            with self.assertRaisesRegex(ValueError, '部分平台版本读取失败'):
                backend.prepare_launch(self.db, dict(original, logical=True), 'claude', self.cwd, policy='legacy')
        self.assertEqual(self.db.execute('SELECT count(*) FROM imports').fetchone()[0], 0)

    def test_failed_first_snapshot_is_hidden_until_successful_retry(self):
        original = dict(ah.resolve(self.db, 's1'))
        with patch.object(store, 'read_native', side_effect=OSError('File temporarily unavailable')):
            with self.assertRaises(OSError):
                store.enroll(self.db, original)
        version = store.lookup(self.db, original)
        self.assertEqual(store.members(self.db, version['conversation']), [])
        self.assertEqual(store.tips(self.db, version['conversation']), [])
        self.assertEqual(len(store.group_rows(self.db, [original])), 1)
        self.assertEqual(store.sync_all(self.db)['failed'], [])
        self.assertEqual(len(store.tips(self.db, version['conversation'])), 1)

    def test_reverting_native_content_does_not_overwrite_an_older_snapshot(self):
        original = dict(ah.resolve(self.db, 's1'))
        saved = self.path.read_bytes()
        first = store.enroll(self.db, original)
        snapshot_path = Path(self.db.execute('SELECT snapshot FROM conversation_revisions WHERE id=?',
                                            (first['head'],)).fetchone()[0])
        snapshot = snapshot_path.read_bytes()
        self.add(original, '追加后又撤回的问题')
        second = store.lookup(self.db, original)
        self.path.write_bytes(saved)
        store.sync_all(self.db)
        third = store.lookup(self.db, original)
        self.assertNotEqual(third['head'], first['head'])
        self.assertEqual(snapshot_path.read_bytes(), snapshot)
        self.assertEqual(store.revision(self.db, third['head'])['parent'], second['head'])
        self.assertEqual(store.revision(self.db, second['head'])['parent'], first['head'])

    def test_claude_origin_codex_claude_roundtrip_keeps_original_metadata_and_new_text(self):
        source = ah.claude_project_dir(self.cwd) / 'origin-claude.jsonl'
        source.parent.mkdir(parents=True, exist_ok=True)
        records = [dict(type=role, sessionId='origin-claude', cwd=self.cwd, uuid='original-' + role,
                        timestamp='2026-10-01T00:00:00Z', message=dict(content=text, model='fixture-model'))
                   for role, text in [('user', 'Claude 原始问题'), ('assistant', 'Claude 原始回答')]]
        source.write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records), encoding='utf-8')
        baseline = source.read_bytes()
        ah.collect(self.db)
        original = dict(ah.resolve(self.db, 'claude:origin-claude'))
        codex = self.launch(original, 'b')
        self.add(codex, 'Codex 新增内容')
        returned = self.launch(codex, 'claude')
        text = self.text(returned)
        self.assertEqual(text.count('Claude 原始问题'), 1)
        self.assertEqual(text.count('Codex 新增内容'), 1)
        self.assertEqual(source.read_bytes(), baseline)
        imported_records = list(ah.codex_import.records(returned['path']))
        self.assertEqual(imported_records[0]['uuid'], 'original-user')
        self.assertNotEqual(returned['id'], original['id'])

    def test_same_native_id_in_different_files_is_not_silently_grouped(self):
        import history_ui
        duplicate = self.a / 'sessions' / 'another-s1.jsonl'
        from tests.test_trash import rollout
        duplicate.write_text(rollout('s1', '另一个物理版本', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        rows = history_ui.list_rows(self.db)
        self.assertEqual(len(rows), 2)
        for row in rows:
            store.enroll(self.db, row)
        self.assertEqual(len(store.group_rows(self.db, rows)), 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM conversations').fetchone()[0], 2)

    def test_published_intent_recovers_without_new_native_copy(self):
        original = dict(ah.resolve(self.db, 's1'))
        prepared = backend.prepare_launch(self.db, original, 'b', self.cwd)
        ah.collect(self.db)
        copied = dict(ah.resolve(self.db, prepared['session']))
        parent = store.lookup(self.db, original)
        pub = self.db.execute('SELECT * FROM handoff_publications').fetchone()
        with self.db:
            self.db.execute('DELETE FROM handoff_publications')
            self.db.execute("UPDATE handoff_intents SET state='published'")
        store.sync_all(self.db)
        restored = self.db.execute('SELECT * FROM handoff_publications').fetchone()
        self.assertEqual(restored['version'], pub['version'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], 1)
        store.sync_all(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM codex_imports').fetchone()[0], 1)
        again = self.launch(original, 'b')
        self.assertNotEqual(again['id'], copied['id'])

    def test_compaction_and_updated_claude_uuid_preserve_revisions_without_double_append(self):
        original = dict(ah.resolve(self.db, 's1'))
        copied = self.launch(original, 'b')
        with Path(copied['path']).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(type='compacted', payload=dict(message='压缩后的摘要'))) + '\n')
        self.add(copied, '压缩后新增问题')
        returned = self.launch(copied, 'a')
        self.assertEqual(self.text(returned).count('压缩后新增问题'), 1)
        self.assertEqual(self.text(returned).count('原始问题'), 1)
        claude = self.launch(original, 'claude')
        self.add(claude, '一个问题', '旧回答')
        with Path(claude['path']).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(type='assistant', sessionId=claude['id'], cwd=self.cwd, uuid='uuid-旧回答',
                message=dict(content='更新后的回答', model='fixture-model')), ensure_ascii=False) + '\n')
        store.sync_all(self.db)
        version = store.lookup(self.db, claude)
        self.assertEqual(version['state'], 'rewritten')
        value = store.revision(self.db, version['head'])
        text = '\n'.join(event['text'] for event in value['events'])
        self.assertNotIn('旧回答', text)
        self.assertEqual(text.count('更新后的回答'), 1)

    def test_deleted_claude_target_stays_deleted_and_live_codex_target_is_not_reused(self):
        original = dict(ah.resolve(self.db, 's1'))
        claude = self.launch(original, 'claude')
        age(Path(claude['path']))
        entry = trash.delete(self.db, [dict(tool='claude', source='claude', path=claude['path'])])
        fresh = self.launch(original, 'claude')
        self.assertNotEqual(fresh['id'], claude['id'])
        self.assertFalse(Path(claude['path']).exists())
        trash.restore(self.db, entry['id'])
        self.assertTrue(Path(claude['path']).exists())
        self.assertNotEqual(self.launch(original, 'claude')['id'], claude['id'])
        copied = self.launch(original, 'b')
        with ah.codex_import.writer_lock(self.b, copied['id']) as locked:
            self.assertTrue(locked)
            fresh = self.launch(original, 'b')
            self.assertNotEqual(fresh['id'], copied['id'])
            self.assertTrue(Path(copied['path']).exists())

    def launch(self, row, target):
        prepared = backend.prepare_launch(self.db, row, target, self.cwd, policy='legacy')
        ah.collect(self.db)
        return dict(ah.resolve(self.db, prepared['session']))

    def add(self, row, question, answer='新回答'):
        path = Path(row['path'])
        if row['tool'] == 'codex':
            records = [dict(type='response_item', payload=dict(type='message', role=role,
                content=[dict(type='input_text' if role == 'user' else 'output_text', text=text)]))
                for role, text in [('user', question), ('assistant', answer)]]
        else:
            records = [dict(type=role, sessionId=row['id'], cwd=self.cwd, uuid='uuid-' + text,
                message=dict(content=text, model='real-model'), timestamp='2026-10-08T00:00:00Z')
                for role, text in [('user', question), ('assistant', answer)]]
        with path.open('a', encoding='utf-8') as stream:
            stream.write(''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records))
        ah.collect(self.db)
        store.sync_all(self.db)

    def text(self, row):
        return '\n'.join(item['text'] for item in backend.file_messages(row))

    def test_codex_claude_codex_claude_roundtrip_only_adds_new_content(self):
        original = dict(ah.resolve(self.db, 's1'))
        saved = self.path.read_bytes()
        claude = self.launch(original, 'claude')
        claude_original = Path(claude['path']).read_bytes()
        self.add(claude, '在 Claude 新增的问题')
        codex = self.launch(claude, 'a')
        text = self.text(codex)
        self.assertEqual(text.count('原始问题'), 1)
        self.assertEqual(text.count('在 Claude 新增的问题'), 1)
        self.assertNotIn(ah.IMPORT_MARK, text)
        self.add(codex, '回到 Codex 的问题')
        returned = self.launch(codex, 'claude')
        text = self.text(returned)
        self.assertEqual(text.count('原始问题'), 1)
        self.assertEqual(text.count('在 Claude 新增的问题'), 1)
        self.assertEqual(text.count('回到 Codex 的问题'), 1)
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertTrue(Path(claude['path']).read_bytes().startswith(claude_original))
        cid = store.lookup(self.db, original)['conversation']
        self.assertEqual(len(store.members(self.db, cid)), 4)
        self.assertEqual(len(store.tips(self.db, cid)), 1)

    def test_multi_connection_return_uses_fresh_ids_and_preserves_selected_branch(self):
        original = dict(ah.resolve(self.db, 's1'))
        copied = self.launch(original, 'b')
        self.add(copied, '相同问题', '相同回答')
        self.add(copied, '相同问题', '相同回答')
        returned = self.launch(copied, 'a')
        self.assertEqual(self.text(returned).count('相同问题'), 2)
        repeated = self.launch(copied, 'a')
        self.assertNotEqual(repeated['id'], returned['id'])
        self.assertEqual(self.text(repeated), self.text(returned))
        source = store.lookup(self.db, copied)
        before = self.db.execute('SELECT count(*) FROM conversation_revisions').fetchone()[0]
        store.sync_all(self.db)
        store.sync_all(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM conversation_revisions').fetchone()[0], before)
        self.add(original, '原连接也续聊了')
        cid = source['conversation']
        self.assertEqual(len(store.tips(self.db, cid)), 2)
        with self.assertRaisesRegex(ValueError, '多个续聊分支'):
            backend.prepare_launch(self.db, dict(returned, logical=True), 'b', self.cwd, policy='legacy')
        self.assertNotIn('原连接也续聊了', self.text(returned))

    def test_partial_rewrite_deleted_and_restore_do_not_duplicate_or_resurrect(self):
        original = dict(ah.resolve(self.db, 's1'))
        copied = self.launch(original, 'b')
        version = store.lookup(self.db, copied)
        head = version['head']
        path = Path(copied['path'])
        with path.open('ab') as stream:
            stream.write(b'{"type":')
        store.sync_all(self.db)
        self.assertEqual(store.lookup(self.db, copied)['head'], head)
        path.write_bytes(path.read_bytes().rsplit(b'{"type":', 1)[0])
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        records[1]['payload']['content'][0]['text'] = '重写后的独立内容'
        path.write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records), encoding='utf-8')
        store.sync_all(self.db)
        self.assertEqual(store.lookup(self.db, copied)['state'], 'rewritten')
        self.assertEqual(self.text(copied).count('重写后的独立内容'), 1)
        age(path)
        manifest = trash.delete(self.db, [dict(tool='codex', source='b', path=str(path))])
        store.sync_all(self.db)
        self.assertEqual(store.lookup(self.db, copied)['present'], 0)
        self.assertFalse(path.exists())
        fresh = self.launch(original, 'b')
        self.assertNotEqual(fresh['id'], copied['id'])
        self.assertFalse(path.exists())
        trash.restore(self.db, manifest['id'])
        store.sync_all(self.db)
        self.assertEqual(store.lookup(self.db, copied)['present'], 1)
