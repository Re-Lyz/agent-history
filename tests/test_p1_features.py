"""P1 contracts with synthetic histories, isolated installers and real Tk."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent_history as ah
import conversation_store as store
import conversation_titles as titles
import entry_settings
import install
import launch_backend as backend
import tool_details
from tests.test_trash import Fixture
import tests.test_history_ui as viewer_tests


class BackendTests(Fixture):
    def test_user_name_survives_collect_handoff_and_recycle_restore(self):
        import trash
        row = dict(ah.resolve(self.db, 's1'))
        original = self.path.read_bytes()
        titles.rename(self.db, row, '自定义中文对话')
        ah.collect(self.db)
        self.assertEqual(titles.title(self.db, row), '自定义中文对话')
        import history_ui
        self.assertEqual([item['id'] for item in history_ui.list_rows(self.db, query='自定义中文')], ['s1'])
        self.assertEqual(self.path.read_bytes(), original)
        child = backend.prepare_launch(self.db, row, 'b', self.cwd)
        ah.collect(self.db)
        native = dict(ah.resolve(self.db, child['session']))
        self.assertEqual(titles.title(self.db, native), '自定义中文对话')
        manifest = trash.delete(self.db, [dict(tool='codex', source='a', path=str(self.path))])
        trash.restore(self.db, manifest['id'])
        ah.collect(self.db)
        self.assertEqual(titles.title(self.db, dict(ah.resolve(self.db, 's1'))), '自定义中文对话')
        titles.rename(self.db, row, '')
        self.assertEqual(titles.title(self.db, row), row['title'])

    def test_parallel_tool_ids_full_output_and_changed_record(self):
        records = [dict(type='session_meta', payload=dict(id='s1', cwd=self.cwd, source='cli'))]
        for identity in ('x', 'y'):
            records.append(dict(type='response_item', payload=dict(type='function_call', name='exec',
                           call_id=identity, arguments=json.dumps(dict(text='参数 '+identity)))))
        records += [dict(type='response_item', payload=dict(type='function_call_output', call_id=identity,
                            output='结果 '+identity+'\n'+'z'*9000)) for identity in ('y', 'x')]
        self.path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records), encoding='utf-8')
        messages = backend.file_messages(dict(ah.resolve(self.db, 's1')))
        self.assertEqual(tool_details.groups(messages), {0:[0,3],1:[1,2],2:None,3:None})
        self.assertIn('truncated', messages[2]['text'])
        self.assertTrue(tool_details.read(messages[2]['detail']).endswith('z'*9000))
        self.path.write_text(self.path.read_text(encoding='utf-8')+'\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '变化'):
            tool_details.read(messages[2]['detail'])

    def test_existing_entries_can_be_named_disabled_and_reset(self):
        entry_settings.save('a',dict(name='主要入口',enabled=False,favorite=True,default=False,order=0))
        self.assertNotIn('a', backend.launch_targets())
        self.assertEqual(backend.command_label('a'), '主要入口')
        entry_settings.save('a')
        self.assertIn('a',backend.launch_targets())

    def test_claude_tool_identity_missing_result_and_duplicate_id_fallback(self):
        records=[dict(type='assistant',message=dict(content=[dict(type='tool_use',id='x',name='Bash',input={'command':'中文'})])),
                 dict(type='user',message=dict(content=[dict(type='tool_result',tool_use_id='x',content='完整结果')]))]
        descriptors=[tool_details.descriptors('claude',record)[0] for record in records]
        self.assertEqual([value['call_id'] for value in descriptors],['x','x'])
        messages=[dict(role='tool_call',call_id='x'),dict(role='tool_call',call_id='x'),dict(role='tool_output',call_id='x'),
                  dict(role='tool_call',call_id=None),dict(role='tool_output',call_id=None)]
        self.assertEqual(tool_details.groups(messages),{number:[number] for number in range(5)})


class InstallerTests(unittest.TestCase):
    def test_systemd_units_and_timers_are_owned_and_uninstalled(self):
        if os.name=='nt': self.skipTest('Linux systemd contract')
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder).resolve()/'user'; prefix=Path(folder).resolve()/'install'
            with patch.object(install,'preflight',return_value=dict(python=sys.executable)):
                spec=install.plan(prefix,home,sys.executable,collect_task=True,sync_task=True)
            with patch.object(install.subprocess,'run') as run:
                install.install(spec); install.install(spec)
                units=home/'.config/systemd/user'
                self.assertEqual(len(list(units.glob('*.timer'))),2)
                self.assertIn('OnUnitActiveSec=5min',(units/'agenthistorycollect.timer').read_text())
                self.assertIn('--shared-root',(units/'agenthistorycollect.service').read_text())
                install.uninstall(prefix)
                self.assertEqual(list(units.glob('*.timer')),[])
                self.assertTrue(any('disable' in call.args[0] for call in run.call_args_list))
    def test_uninstall_restores_only_owned_registration_and_keeps_later_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()/'user'; home.mkdir(); prefix = Path(folder).resolve()/'install'
            codex = home/'.codex/config.toml'; codex.parent.mkdir()
            original = 'model="test"\n[mcp_servers.other]\ncommand="other"\n'
            codex.write_text(original)
            claude = home/'.claude.json'
            claude.write_text(json.dumps(dict(mcpServers={'agent-history':dict(command='prior')})))
            with patch.object(install,'preflight',return_value=dict(python=sys.executable)):
                spec=install.plan(prefix,home,sys.executable,codex_mcp=True,claude_mcp=True,claude_hook=True)
            spec['shortcuts']=[]
            install.install(spec); install.install(spec)
            later=json.loads(claude.read_text()); later['other']='later'; claude.write_text(json.dumps(later))
            codex.write_text(codex.read_text()+'\n[another]\nvalue="later"\n')
            self.assertTrue(install.uninstall(prefix,dry_run=True)['removed'])
            self.assertIn('mcp_servers.agent-history',codex.read_text())
            install.uninstall(prefix)
            self.assertNotIn('mcp_servers.agent-history',codex.read_text())
            self.assertIn('value="later"',codex.read_text())
            restored=json.loads(claude.read_text())
            self.assertEqual(restored['mcpServers']['agent-history'],dict(command='prior'))
            self.assertEqual(restored['other'],'later')
            self.assertNotIn('SessionStart',json.loads((home/'.claude/settings.json').read_text()).get('hooks',{}))
            install.install(spec)
            modified=json.loads(claude.read_text()); modified['mcpServers']['agent-history']['command']='user-edited'
            claude.write_text(json.dumps(modified))
            result=install.uninstall(prefix)
            self.assertIn(str(claude),result['kept'])
            self.assertEqual(json.loads(claude.read_text())['mcpServers']['agent-history']['command'],'user-edited')

    @unittest.skipUnless(os.name == 'nt', 'Windows shortcut contract')
    def test_real_start_menu_shortcut_is_isolated_and_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder).resolve()/'user'; prefix=Path(folder).resolve()/'install 中文 空格'
            source=Path(folder).resolve()/'source 中文 空格'; source.mkdir()
            output=Path(folder).resolve()/'entry-result.json'
            (source/'history_ui.py').write_text('import os,json\nfrom pathlib import Path\n'+
                'Path('+repr(str(output))+').write_text(json.dumps({key:os.environ[key] for key in '+
                repr(['AGENT_HISTORY_SHARED_ROOT','AGENT_HISTORY_HOME'])+'}))\n',encoding='utf-8')
            with patch.object(install,'ROOT',source):
                spec=install.plan(prefix,home,sys.executable,desktop=True)
                install.install(spec); install.install(spec)
            self.assertTrue(all(Path(path).is_file() for path in spec['shortcuts']))
            self.assertTrue((prefix/'codexui.vbs').read_bytes().startswith(b'\xff\xfe'))
            subprocess.run(['cscript.exe','//Nologo',str(prefix/'codexui.vbs')],capture_output=True,check=True,timeout=10)
            deadline=time.monotonic()+10
            while not output.exists() and time.monotonic()<deadline: time.sleep(.05)
            self.assertEqual(json.loads(output.read_text()),dict(AGENT_HISTORY_SHARED_ROOT=spec['config'],AGENT_HISTORY_HOME=spec['data']))
            install.uninstall(prefix)
            self.assertTrue(all(not Path(path).exists() for path in spec['shortcuts']))

    def test_base_install_is_repeatable_and_does_not_touch_agent_configs(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder).resolve()/'user'; prefix=Path(folder).resolve()/'install'
            with patch.object(install,'preflight',return_value=dict(python=sys.executable)):
                spec=install.plan(prefix,home,sys.executable)
            spec['shortcuts']=[]
            first=install.install(spec)
            second=install.install(spec)
            self.assertEqual(first,second)
            self.assertFalse((home/'.claude.json').exists())
            self.assertFalse((home/'.claude/settings.json').exists())
            self.assertFalse((home/'.codex/config.toml').exists())
            self.assertEqual(first['tasks'],[])
            (home/'.agent-history').mkdir(parents=True)
            data=home/'.agent-history/keep.txt'; data.write_text('preserve')
            result=install.uninstall(prefix)
            self.assertTrue(result['removed'])
            self.assertEqual(data.read_text(),'preserve')

    def test_integrations_are_independent_and_preserve_existing_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder).resolve()/'user'; home.mkdir(); prefix=Path(folder).resolve()/'install'
            original=dict(other='preserve',mcpServers=dict(other=dict(command='other')))
            (home/'.claude.json').write_text(json.dumps(original))
            with patch.object(install,'preflight',return_value=dict(python=sys.executable)):
                spec=install.plan(prefix,home,sys.executable,claude_mcp=True)
            spec['shortcuts']=[]
            install.install(spec); install.install(spec)
            value=json.loads((home/'.claude.json').read_text())
            self.assertEqual(value['other'],'preserve')
            self.assertIn('other',value['mcpServers'])
            self.assertFalse((home/'.claude/settings.json').exists())


class NewViewerTests(viewer_tests.ViewerInteractionTests):
    def test_global_search_discards_old_result_and_locates_older_message(self):
        import history_ui as ui
        entered=threading.Event(); release=threading.Event(); finished=threading.Event()
        original=ui.list_rows
        main_thread=threading.get_ident()
        def search(*args,**kwargs):
            query=kwargs.get('query',args[2] if len(args)>2 else '')
            if query=='slow-fixture':
                self.assertNotEqual(threading.get_ident(),main_thread)
                entered.set(); release.wait(5); finished.set()
                return original(*args[:2],query='two',**kwargs)
            return original(*args,**kwargs)
        def interact(root):
            root.deiconify()
            tree=next(w for w in self.widgets() if isinstance(w,self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children())==2)
            entry=next(w for w in self.widgets() if w.winfo_name()=='global_search')
            entry.focus_force(); root.update()
            entry.delete(0,'end')
            entry.insert(0,'slow-fixture'); entry.event_generate('<Return>')
            self.wait(entered.is_set)
            entry.delete(0,'end'); entry.insert(0,'one 0'); entry.event_generate('<Return>')
            self.wait(lambda: len(tree.get_children())==1 and 'one.jsonl' in tree.get_children()[0])
            tree.selection_set(tree.get_children()[0]); root.update()
            self.wait(lambda: any(isinstance(w,self.tk.Text) and 'one 0' in w.get('1.0','end') for w in self.widgets()))
            self.wait(lambda: any('450 / 450' in value for value in self.statuses()))
            release.set(); self.wait(finished.is_set)
            deadline=time.monotonic()+.2
            while time.monotonic()<deadline: root.update(); time.sleep(.01)
            self.assertEqual(len(tree.get_children()),1)
            self.assertIn('one.jsonl',tree.get_children()[0])
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        try:
            with patch.object(ui,'list_rows',side_effect=search): self.exercise(interact)
        finally: release.set()

    def test_rename_and_existing_entry_form(self):
        def interact(root):
            from tkinter import simpledialog
            from settings_ui import ProfileManager
            tree=next(w for w in self.widgets() if isinstance(w,self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children())==2)
            tree.selection_set(next(i for i in tree.get_children() if 'one.jsonl' in i)); root.update()
            with patch.object(simpledialog,'askstring',return_value='我的项目对话'):
                self.button('修改对话名称').invoke()
            self.wait(lambda: any('我的项目对话' in tree.set(i,'title') for i in tree.get_children()))
            manager=ProfileManager(root)
            self.assertTrue({'a','b','claude'}<=set(manager.tree.get_children()))
            manager.tree.selection_set('a'); root.update()
            manager.values['name'].set('已有连接 A'); manager.save(); root.update()
            self.assertEqual(backend.command_label('a'),'已有连接 A')
            manager.window.destroy()
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)
    def test_tool_panel_expansion_reads_full_content_and_survives_mode_switch(self):
        records=[dict(type='session_meta',payload=dict(id='two',cwd=str(self.path),source='cli')),
                 dict(type='response_item',payload=dict(type='function_call',call_id='call',name='exec',arguments='{}')),
                 dict(type='response_item',payload=dict(type='function_call_output',call_id='call',output='完整输出\n'+'x'*9000))]
        path=self.path/'a/sessions/two.jsonl'
        path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),encoding='utf-8')
        with contextlib.closing(ah.connect()) as db: ah.collect(db)
        def interact(root):
            tree=next(w for w in self.widgets() if isinstance(w,self.ttk.Treeview) and 'check' in w.cget('columns'))
            self.wait(lambda: len(tree.get_children())==2)
            tree.selection_set(next(i for i in tree.get_children() if 'two.jsonl' in i)); root.update()
            self.wait(lambda: any(isinstance(w,self.ttk.Button) and str(w.cget('text')).startswith('▸ exec') for w in self.widgets()))
            button=next(w for w in self.widgets() if isinstance(w,self.ttk.Button) and str(w.cget('text')).startswith('▸ exec'))
            button.invoke()
            self.wait(lambda: any(isinstance(w,self.tk.Text) and '完整输出' in w.get('1.0','end') for w in self.widgets()))
            detail=next(w for w in self.widgets() if isinstance(w,self.tk.Text) and '完整输出' in w.get('1.0','end'))
            self.assertIn('x'*9000,detail.get('1.0','end'))
            mode=next(w for w in self.widgets() if w.winfo_name()=='preview_mode')
            mode.set('原文'); mode.event_generate('<<ComboboxSelected>>'); root.update()
            mode.set('Markdown'); mode.event_generate('<<ComboboxSelected>>'); root.update()
            self.wait(lambda: any(isinstance(w,self.tk.Text) and '完整输出' in w.get('1.0','end') for w in self.widgets()))
            button=next(w for w in self.widgets() if isinstance(w,self.ttk.Button) and str(w.cget('text')).startswith('▾ exec'))
            detail=next(w for w in self.widgets() if isinstance(w,self.tk.Text) and '完整输出' in w.get('1.0','end'))
            button.invoke(); self.assertFalse(detail.winfo_ismapped())
            root.deiconify()
            search=next(w for w in self.widgets() if w.winfo_name()=='global_search')
            search.focus_force(); root.update(); search.delete(0,'end'); search.insert(0,'完整输出')
            search.event_generate('<Return>')
            self.wait(lambda: len(tree.get_children())==1)
            tree.selection_set(tree.get_children()[0]); root.update()
            self.wait(lambda: any(isinstance(w,self.tk.Text) and len(w.tag_ranges('match'))==2 for w in self.widgets()))
            root.tk.call(root.protocol('WM_DELETE_WINDOW'))
        self.exercise(interact)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite([loader.loadTestsFromTestCase(BackendTests), loader.loadTestsFromTestCase(InstallerTests)])
    suite.addTests(NewViewerTests(name) for name in NewViewerTests.__dict__ if name.startswith('test_'))
    return suite
