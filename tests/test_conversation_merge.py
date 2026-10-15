"""Consolidation preserves originals and literal branch traffic, without models."""
import json
from pathlib import Path
from unittest.mock import patch

import agent_history as ah
import conversation_merge as merge
import conversation_store as store
import handoff_lifecycle
from tests.test_conversation_store import ConversationTests


class MergeTests(ConversationTests):
    def test_common_prefix_reuses_complete_native_and_reactivates_changed_original(self):
        original = dict(ah.resolve(self.db, 's1'))
        copied = self.launch(original, 'b')
        self.add(copied, '后续内容')
        cid = store.lookup(self.db, original)['conversation']
        saved = {row['path']: Path(row['path']).read_bytes() for row in (original, copied)}
        value = merge.plan(self.db, cid)
        self.assertEqual(value['tails'], [])
        with patch.object(ah.codex_import, 'import_session', side_effect=AssertionError('No new copy needed')):
            result = merge.apply(self.db, value)
        self.assertEqual(result['row']['id'], copied['id'])
        self.assertEqual(len(store.members(self.db, cid)), 1)
        self.assertEqual(len(store.members(self.db, cid, include_merged=True)), 2)
        self.assertTrue(all(Path(path).read_bytes() == body for path, body in saved.items()))
        self.add(original, '旧版本继续发的新问题')
        self.assertEqual(len(store.members(self.db, cid)), 2)
        self.assertEqual(len(store.tips(self.db, cid)), 2)

    def test_divergent_merge_keeps_repeated_questions_and_full_inert_tools(self):
        original = dict(ah.resolve(self.db, 's1'))
        copied = self.launch(original, 'b')
        self.add(original, '重复问题', '主版本回答')
        self.add(original, '主版本尾部', '完整主历史')
        self.add(copied, '重复问题', '分支的不同回答')
        with Path(copied['path']).open('a', encoding='utf-8') as stream:
            for payload in [dict(type='function_call', name='must_not_execute', call_id='call', arguments='{}'),
                            dict(type='function_call_output', call_id='call', output='工具全文' + 'z' * 9000 + '\n\n')]:
                stream.write(json.dumps(dict(type='response_item', payload=payload), ensure_ascii=False) + '\n')
        store.sync_all(self.db)
        # Make the original the longest spine; secondary tools must be inert text.
        self.add(original, '再补充的问题', '主分支最新回答')
        saved = {row['path']: Path(row['path']).read_bytes() for row in (original, copied)}
        cid = store.lookup(self.db, original)['conversation']
        value = merge.plan(self.db, cid)
        self.assertEqual(len(value['tails']), 1)
        with patch.object(handoff_lifecycle, 'verify_native', side_effect=lambda db, child, final: child) as verify:
            result = merge.apply(self.db, value)
        verify.assert_called_once()
        row = result['row']
        self.assertNotIn(row['id'], (original['id'], copied['id']))
        text = self.text(row)
        # These are two independently sent questions after the known fork.
        self.assertEqual(text.count('重复问题'), 2)
        self.assertIn('主版本回答', text)
        self.assertIn('分支的不同回答', text)
        self.assertIn('z' * 9000, text)
        self.assertEqual(text.count('原始问题'), 1)
        self.assertEqual(len(store.tips(self.db, cid)), 1)
        self.assertEqual(len(store.members(self.db, cid)), 1)
        self.assertEqual(len(store.members(self.db, cid, include_merged=True)), 3)
        records = list(ah.codex_import.records(row['path']))
        self.assertFalse(any(record.get('payload', {}).get('name') == 'must_not_execute' for record in records))
        self.assertTrue(all(Path(path).read_bytes() == body for path, body in saved.items()))
        self.assertEqual(len(merge.plan(self.db, cid)['versions']), 1)
        # Removing the merged file restores both original branches in the UI.
        Path(row['path']).unlink()
        self.assertEqual(len(store.members(self.db, cid)), 2)

    def test_identical_legacy_prefixes_and_repeated_tail_occurrences(self):
        original = dict(ah.resolve(self.db, 's1'))
        copied = self.launch(original, 'b')
        self.add(original, '左分支问题', '左分支回答')
        self.add(copied, '右分支问题', '右分支回答')
        self.add(copied, '原始问题', '重复是新发生的消息')
        self.add(copied, '原始问题', '重复是新发生的消息')
        cid = store.lookup(self.db, original)['conversation']
        # Legacy enrollments do not share canonical IDs, only a common prefix.
        item = store.lookup(self.db, copied)
        with self.db:
            self.db.execute('UPDATE conversation_versions SET parent_revision=NULL,baseline=NULL,head=NULL WHERE id=?',
                            (item['id'],))
        store.sync_all(self.db)
        with patch.object(handoff_lifecycle, 'verify_native', side_effect=lambda db, child, final: child):
            result = merge.apply(self.db, merge.plan(self.db, cid))
        text = self.text(result['row'])
        self.assertEqual(text.count('原始问题'), 3)
        self.assertIn('左分支问题', text)
        self.assertIn('右分支问题', text)

    def test_changes_after_preview_are_rejected_before_publication(self):
        original = dict(ah.resolve(self.db, 's1'))
        copied = self.launch(original, 'b')
        cid = store.lookup(self.db, original)['conversation']
        value = merge.plan(self.db, cid)
        self.add(copied, '预览之后新增的内容')
        with patch.object(ah.codex_import, 'import_session') as publish:
            with self.assertRaisesRegex(ValueError, '来源发生变化'):
                merge.apply(self.db, value)
        publish.assert_not_called()
        self.assertEqual(len(store.members(self.db, cid)), 2)

    def test_native_verification_failure_keeps_both_branches_visible(self):
        original = dict(ah.resolve(self.db, 's1'))
        copied = self.launch(original, 'b')
        self.add(original, '左分支')
        self.add(copied, '右分支')
        cid = store.lookup(self.db, original)['conversation']
        value = merge.plan(self.db, cid)
        with patch.object(handoff_lifecycle, 'verify_native', side_effect=ValueError('fixture native failure')):
            with self.assertRaisesRegex(ValueError, 'fixture native failure'):
                merge.apply(self.db, value)
        self.assertEqual(self.db.execute('SELECT count(*) FROM conversation_merges').fetchone()[0], 0)
        self.assertTrue({store.lookup(self.db, row)['id'] for row in (original, copied)} <=
                        {item['id'] for item in store.members(self.db, cid)})
        with patch.object(handoff_lifecycle, 'verify_native', side_effect=lambda db, child, final: child):
            with patch.object(ah.codex_import, 'import_session', side_effect=AssertionError('Resume existing publication')):
                merge.apply(self.db, value)
        self.assertEqual(len(store.members(self.db, cid)), 1)

    def test_append_during_publication_preserves_snapshot_and_new_traffic(self):
        original = dict(ah.resolve(self.db, 's1'))
        copied = self.launch(original, 'b')
        self.add(original, '左分支')
        self.add(copied, '右分支')
        cid = store.lookup(self.db, original)['conversation']
        value = merge.plan(self.db, cid)
        def check(db, child, final):
            # A running terminal may append after the merge was published.
            with Path(original['path']).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(type='response_item', payload=dict(type='message', role='user',
                    content=[dict(type='input_text', text='合并期间追加的新问题')])), ensure_ascii=False) + '\n')
            return child
        with patch.object(handoff_lifecycle, 'verify_native', side_effect=check):
            result = merge.apply(self.db, value)
        store.sync_all(self.db)
        self.assertIn('左分支', self.text(result['row']))
        self.assertIn('右分支', self.text(result['row']))
        self.assertNotIn('合并期间追加的新问题', self.text(result['row']))
        self.assertEqual(len(store.members(self.db, cid)), 2)
        self.assertTrue(any(item['id'] == store.lookup(self.db, original)['id'] for item in store.members(self.db, cid)))


def load_tests(loader, tests, pattern):
    # Inherit only fixture helpers; run inherited contracts in their own module.
    import unittest
    return unittest.TestSuite(MergeTests(name) for name in MergeTests.__dict__ if name.startswith('test_'))
