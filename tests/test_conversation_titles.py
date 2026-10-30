"""Account-prefix display and guarded native title synchronization."""
import json
from unittest.mock import patch

import agent_history as ah
import codex_import as ci
import conversation_titles as titles
from history_app import native_titles
from tests.test_trash import Fixture


class TitleTests(Fixture):
    def test_display_hides_only_known_leading_accounts(self):
        row = dict(source='b', title='[openai1] [a][OPENAI4] 标题 [TODO]')
        self.assertEqual(titles.display(row, ['a', 'b']), '标题 [TODO]')
        self.assertEqual(row['title'], '[openai1] [a][OPENAI4] 标题 [TODO]')
        for value in ('[TODO] 标题', '[论文] [openai1] 内容', '正文 [openai1]', '[a + b] 公式'):
            self.assertEqual(titles.display(dict(source='b', title=value), ['a']), value)
        self.assertEqual(titles.display(dict(source='b', title='[b][openai99]')), '')

    def rpc(self):
        client = patch.object(native_titles, 'CodexRPC')
        factory = client.start()
        self.addCleanup(client.stop)
        rpc = factory.return_value.__enter__.return_value
        state = dict(name='之前的原生名称', preview='原始问题', id='s1', path=str(self.path))
        def call(method, params):
            if method == 'thread/name/set':
                journals = list((ah.DATA / 'maintenance/title-updates').glob('*.json'))
                report = next(value for value in (json.loads(path.read_text(encoding='utf-8')) for path in journals)
                              if value['state'] == 'local-saved' and value['native'][0]['state'] == 'prepared')
                self.assertEqual(report['native'][0]['previous_name'], state['name'])
                state['name'] = params['name']
                return {}
            self.assertEqual(method, 'thread/read')
            return dict(thread=dict(state))
        rpc.call.side_effect = call
        return rpc, state

    def test_rename_updates_native_after_recording_old_name_without_touching_messages(self):
        row = dict(ah.resolve(self.db, 's1'))
        original = self.path.read_bytes()
        rpc, state = self.rpc()
        result = titles.rename_and_sync(self.db, row, '新名称')
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(state['name'], '新名称')
        self.assertEqual(titles.title(self.db, row), '新名称')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual([call.args[0] for call in rpc.call.call_args_list],
                         ['thread/read', 'thread/name/set', 'thread/read'])
        result = titles.rename_and_sync(self.db, row, '')
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(state['name'], '原始问题')

    def test_busy_native_keeps_local_name_and_reports_pending_without_starting_codex(self):
        row = dict(ah.resolve(self.db, 's1'))
        with ci.writer_lock(self.a, 's1') as free, patch.object(native_titles, 'CodexRPC') as rpc:
            self.assertTrue(free)
            result = titles.rename_and_sync(self.db, row, '忙碌时改名')
            rpc.assert_not_called()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['native'][0]['state'], 'busy')
        self.assertEqual(titles.title(self.db, row), '忙碌时改名')

    def test_deleted_native_file_and_failed_interface_do_not_recreate_or_hide_failure(self):
        row = dict(ah.resolve(self.db, 's1'))
        with patch.object(native_titles, 'CodexRPC', side_effect=ValueError('unsupported')):
            result = titles.rename_and_sync(self.db, row, '本地保留')
        self.assertEqual(result['native'][0]['state'], 'failed')
        self.path.unlink()
        with patch.object(native_titles, 'CodexRPC') as rpc:
            result = titles.rename_and_sync(self.db, row, '不得恢复文件')
            rpc.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertEqual(result['state'], 'partial')
