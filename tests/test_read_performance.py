"""Read optimizations preserve physical identity, exact search and native boundaries."""
import contextlib
import json
from pathlib import Path
import time
from unittest.mock import Mock, patch

import agent_history as ah
import codex_import as ci
import conversation_store as store
import history_ui as ui
import launch_backend as backend
import launch_jobs as jobs
import tool_details
import ui_support as support
from history_app.reader_data import file_message_page, ReadCancelled, reverse_lines
from tests.test_trash import Fixture, rollout


class ReadPerformanceTests(Fixture):
    def comparable(self, messages):
        result = []
        for row in messages:
            row = dict(row)
            if row.get('detail'):
                row['detail'] = {key: value for key, value in row['detail'].items() if key != 'ordinal'}
            result.append(row)
        return result

    def write(self, path, records, tail=''):
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records) + tail, encoding='utf-8')

    def test_tail_pages_parse_only_needed_records_and_reconstruct_full_history(self):
        records = [dict(type='session_meta', payload=dict(id='s1', cwd=self.cwd, source='cli'))]
        records += [dict(type='response_item', payload=dict(type='message', role='user',
                    content=[dict(type='input_text', text=f'中文 {i} ' + 'x' * 300)])) for i in range(1600)]
        records.append(dict(type='response_item', payload=dict(type='function_call_output', call_id='last', output='尾部工具')))
        self.write(self.path, records, '{"partial":')
        row = dict(ah.resolve(self.db, 's1'))
        parser = ah.PARSERS['codex']
        parsed = Mock(wraps=parser)
        with patch.dict(ah.PARSERS, codex=parsed):
            page = file_message_page(row, 37)
            self.assertLessEqual(parsed.call_count, 38)
        self.assertTrue(page.has_more)
        self.assertEqual(tool_details.read(page[-1]['detail']), '尾部工具')
        messages = list(page)
        while page.has_more:
            page = file_message_page(row, 137, page.cursor)
            messages[:0] = page
        self.assertEqual(self.comparable(messages), self.comparable(backend.file_messages(row)))

    def test_claude_record_can_cross_page_boundary_without_losing_events(self):
        path = self.root / 'claude-messages.jsonl'
        records = [dict(type='assistant', message=dict(content=[
            dict(type='text', text='first'), dict(type='tool_use', id='x', name='Bash', input={'command': '中文'}),
            dict(type='text', text='after'), dict(type='tool_use', id='y', name='Read', input={'file': 'b'})])),
            dict(type='user', message=dict(content=[dict(type='tool_result', tool_use_id='x', content='result x'),
                                                   dict(type='tool_result', tool_use_id='y', content='result y')]))]
        self.write(path, records)
        row = dict(tool='claude', source='claude', path=str(path))
        page = file_message_page(row, 1)
        messages = list(page)
        while page.has_more:
            page = file_message_page(row, 1, page.cursor)
            messages[:0] = page
        self.assertEqual(self.comparable(messages), self.comparable(backend.file_messages(row)))
        for message in messages:
            if message.get('detail'):
                self.assertTrue(tool_details.read(message['detail']))

    def test_fork_prefix_obeys_both_byte_and_ordinal_markers(self):
        parent = self.a / 'sessions' / 'parent.jsonl'
        parent.write_text(rollout('parent', 'inherited', self.cwd) + rollout('extra', 'excluded', self.cwd), encoding='utf-8')
        bound = len(rollout('parent', 'inherited', self.cwd).encode('utf-8'))
        row = dict(ah.resolve(self.db, 's1'))
        for byte_limit, ordinal_limit in ((bound, None), (None, 2), (parent.stat().st_size, 2), (bound, 99), (bound, 0)):
            chain = [(parent, byte_limit, ordinal_limit), (self.path, None, None)]
            with self.subTest(byte_limit=byte_limit, ordinal_limit=ordinal_limit), patch.object(ci, 'ancestry', return_value=chain):
                page = file_message_page(row, 1)
                messages = list(page)
                while page.has_more:
                    page = file_message_page(row, 1, page.cursor)
                    messages[:0] = page
                self.assertEqual(self.comparable(messages), self.comparable(backend.file_messages(row)))
                self.assertFalse(any(message['text'] == 'excluded' for message in messages))

    def test_tool_offsets_skip_prefix_and_legacy_ordinal_still_works(self):
        self.path.write_text(rollout('s1', '中文 prefix', self.cwd) + json.dumps(dict(type='response_item',
            payload=dict(type='function_call_output', call_id='x', output='full ' + 'z' * 9000))) + '\n', encoding='utf-8')
        message = backend.file_messages(dict(ah.resolve(self.db, 's1')))[-1]
        with patch.object(ci, 'records', side_effect=AssertionError('Rescanned prefix')):
            self.assertEqual(tool_details.read(message['detail']), 'full ' + 'z' * 9000)
        legacy = dict(message['detail'])
        legacy.pop('offset')
        self.assertEqual(tool_details.read(legacy), 'full ' + 'z' * 9000)
        with self.assertRaises(ValueError):
            tool_details.read(dict(message['detail'], offset=1))

    def test_obsolete_read_can_stop_without_modifying_transcript(self):
        original = self.path.read_bytes()
        with self.assertRaises(ReadCancelled):
            file_message_page(dict(ah.resolve(self.db, 's1')), cancel=lambda: True)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_bytes(b'{partial')
        self.assertEqual(list(reverse_lines(self.path, self.path.stat().st_size)), [])

    def test_indexed_paging_filters_and_sorts_without_native_io(self):
        for index in range(710):
            sid, path = f'fixture-{index:04}', f'/missing/fixture-{index:04}'
            self.db.execute('INSERT INTO sessions(key,id,tool,source,path,cwd,kind,turns,title) VALUES (?,?,?,?,?,?,?,?,?)',
                            ('codex:' + sid, sid, 'codex', 'a', path, '/project', 'main', index, sid))
            self.db.execute('INSERT INTO files(path,source,session_key,mtime) VALUES (?,?,?,?)',
                            (path, 'a', 'codex:' + sid, index))
        self.db.commit()
        with patch.object(backend, 'physical_row', side_effect=AssertionError('Native I/O')), \
                patch.object(backend, 'file_messages', side_effect=AssertionError('Native I/O')):
            first = ui.list_rows(self.db, indexed=True, logical=True, sort_by='turns', limit=100)
            second = ui.list_rows(self.db, indexed=True, logical=True, sort_by='turns', limit=100, offset=100)
            self.assertEqual(first.total, 711)
            self.assertEqual([row['id'] for row in first], [f'fixture-{i:04}' for i in range(709, 609, -1)])
            self.assertEqual(second[0]['id'], 'fixture-0609')
            self.assertEqual(len({row['path'] for row in [*first, *second]}), 200)
            choices = ui.project_choices(self.db)
            self.assertIn('/project', choices)
            self.assertEqual(ui.list_rows(self.db, indexed=True, project='/project', limit=1).total, 710)
            kept = ui.list_rows(self.db, indexed=True, limit=1, sort_by='turns',
                                keep_paths={'/missing/fixture-0000'})
            self.assertEqual(len(kept), kept.total)
            self.assertEqual(kept[-1]['id'], 'fixture-0000')
        with self.assertRaises(ReadCancelled):
            ui.list_rows(self.db, indexed=True, cancel=lambda: True)

    def test_physical_fts_matches_exact_substrings_and_survives_rewrite_and_delete(self):
        other = self.b / 'sessions' / 'copy.jsonl'
        other.write_text(rollout('s1', 'other 独立版本', self.cwd), encoding='utf-8')
        self.path.write_text(rollout('s1', 'Straße 中文重构性能 "x y" plus a+b and lone xy', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        for query in ('strasse', '中文重构', '重构', '"x y"', 'a+b', 'xy', '独立版本', 'missing', ' '):
            expected = {row['path'] for row in ui.list_rows(self.db, query=query)}
            actual = {row['path'] for row in ui.list_rows(self.db, indexed=True, query=query)}
            self.assertEqual(actual, expected, query)
        self.path.write_text(rollout('s1', 'replacement 最新文本', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        self.assertEqual(ui.list_rows(self.db, indexed=True, query='strasse'), [])
        self.assertEqual([row['path'] for row in ui.list_rows(self.db, indexed=True, query='最新文本')], [str(self.path)])
        with self.db:
            ah.forget_file(self.db, str(self.path))
        self.assertEqual(ui.list_rows(self.db, indexed=True, query='最新文本'), [])
        self.db.execute("INSERT INTO file_views_fts(file_views_fts,rank) VALUES('integrity-check',1)")

    def test_old_database_search_remains_complete_before_incremental_backfill(self):
        self.db.executescript('DROP TRIGGER file_views_bi; DROP TRIGGER file_views_ai; DROP TRIGGER file_views_ad; DROP TRIGGER file_views_au; '
                             'DROP TABLE file_views_fts; DROP TABLE file_view_search_coverage; PRAGMA user_version=3;')
        with contextlib.closing(ah.connect()) as migrated:
            self.assertEqual(migrated.execute('SELECT count(*) FROM file_view_search_coverage').fetchone()[0], 0)
            self.assertEqual([row['id'] for row in ui.list_rows(migrated, indexed=True, query='原始问题')], ['s1'])
            ah.refresh_search_index(migrated)
            self.assertEqual([row['id'] for row in ui.list_rows(migrated, indexed=True, query='原始问题')], ['s1'])
            self.assertEqual(migrated.execute('SELECT count(*) FROM file_view_search_coverage').fetchone()[0], 1)

    def test_legacy_replace_keeps_external_search_index_consistent(self):
        row = self.db.execute('SELECT * FROM file_views WHERE path=?', (str(self.path),)).fetchone()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO file_views VALUES (?,?,?,?,?)',
                            (row['path'], row['stamp'], row['metadata'], row['turns'], 'legacy replacement marker'))
        self.assertEqual(ui.list_rows(self.db, indexed=True, query='原始问题'), [])
        self.assertEqual(len(ui.list_rows(self.db, indexed=True, query='replacement marker')), 1)
        self.db.execute("INSERT INTO file_views_fts(file_views_fts,rank) VALUES('integrity-check',1)")

    def test_stale_report_snapshot_cannot_mark_a_restarted_runner_collected(self):
        identity = jobs.create(dict(ah.resolve(self.db, 's1')), dict(session='s1', cwd=self.cwd, kind='new'), 'a')
        prior = jobs.update(identity, state='exited', sync_state='pending')
        jobs.update(identity, state='running', attempts=1)
        with patch.object(jobs, 'report_catalog', return_value=[prior]):
            jobs.mark_collected(dict(failed=[]))
        self.assertEqual(jobs.read(identity)['sync_state'], 'pending')

    def test_logical_pagination_agrees_with_full_grouping_and_preserves_other_same_id(self):
        row = dict(ah.resolve(self.db, 's1'))
        first = store.enroll(self.db, row)
        child = self.b / 'sessions' / 'child.jsonl'
        child.write_text(rollout('child', 'different branch', self.cwd), encoding='utf-8')
        independent = self.b / 'sessions' / 'same-id.jsonl'
        independent.write_text(rollout('s1', 'unrelated', self.cwd), encoding='utf-8')
        ah.collect(self.db)
        store.enroll(self.db, dict(ah.resolve(self.db, 'child')), conversation=first['conversation'])
        expected = support.sort_history(store.group_rows(self.db, ui.list_rows(self.db, limit=None)))
        actual = ui.list_rows(self.db, indexed=True, logical=True, limit=1)
        tail = ui.list_rows(self.db, indexed=True, logical=True, limit=1, offset=1)
        self.assertEqual(actual.total, len(expected))
        self.assertEqual([row['path'] for row in [*actual, *tail]], [row['path'] for row in expected])
        self.assertIn(str(independent), {row['path'] for row in [*actual, *tail]})

    def test_report_catalog_reuses_completed_reports_and_detects_legacy_updates(self):
        identities = []
        for _ in range(35):
            identity = jobs.create(dict(ah.resolve(self.db, 's1')), dict(session='s1', cwd=self.cwd, kind='new'), 'a')
            jobs.update(identity, state='exited', sync_state='collected')
            identities.append(identity)
        self.assertEqual(len(jobs.reports(limit=None)), 35)
        with patch.object(jobs, 'read', wraps=jobs.read) as read:
            jobs.reports()
            jobs.mark_collected(dict(failed=[]))
            read.assert_not_called()
        jobs.update(identities[0], state='running', sync_state='pending')
        self.assertEqual([item['id'] for item in jobs.report_catalog(limit=None, active_only=True)], [identities[0]])
        # An old runner may overwrite a finished JSON report without os.replace.
        file = jobs.path(identities[1])
        value = json.loads(file.read_text(encoding='utf-8'))
        value.update(state='failed', sync_state='pending')
        file.write_text(json.dumps(value), encoding='utf-8')
        now = time.monotonic()
        with patch.object(jobs.time, 'monotonic', return_value=now + 6):
            self.assertEqual({item['id'] for item in jobs.report_catalog(limit=None, active_only=True)}, set(identities[:2]))
