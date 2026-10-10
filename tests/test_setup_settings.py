"""First-use discovery and real Tk form interactions, using local synthetic homes."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_history as ah
import connections
import launch_backend as backend
import launch_profiles as profiles
import setup_backend as setup
from tests.test_trash import Fixture


class SetupTests(Fixture):
    def test_claude_only_missing_registry_and_selected_scope(self):
        (ah.ROOT / 'connections.json').unlink()
        result = setup.configure([dict(tool='claude', name='仅 Claude', home=str(self.claude))])
        self.assertEqual(result['codex'], [])
        self.assertEqual(backend.launch_targets(), ['claude', 'profile:setup_claude'])
        self.assertEqual(profiles.get('profile:setup_claude')['name'], '仅 Claude')
        self.assertEqual(list(ah.codex_homes()), [])
        self.assertEqual(ah.claude_homes(), [self.claude])

    def test_existing_direct_home_keeps_config_and_auth_and_registers_once(self):
        home = self.root / '.codex'
        home.mkdir()
        config, auth = home / 'config.toml', home / 'auth.json'
        config.write_text('model="fixture-model"\n', encoding='utf-8')
        auth.write_text('fixture-never-read', encoding='utf-8')
        row = dict(tool='codex', name='本机入口', home=str(home))
        result = setup.configure([row])
        repeated = setup.configure([dict(row, name='改名后的入口')])
        self.assertEqual(result['codex'], repeated['codex'])
        connection = result['codex'][0]
        self.assertTrue(connections.load(ah.ROOT)['connections'][connection]['direct'])
        self.assertEqual(auth.read_text(encoding='utf-8'), 'fixture-never-read')
        self.assertEqual(config.read_text(encoding='utf-8'), 'model="fixture-model"\n')
        self.assertEqual(profiles.get('profile:setup_' + connection)['name'], '改名后的入口')
        discovered = setup.discover(user_home=self.root)
        self.assertEqual(sum(item['home'] == str(home) for item in discovered['candidates']), 1)

    def test_selected_scope_hides_previously_indexed_unselected_history(self):
        import history_ui
        self.assertTrue(history_ui.list_rows(self.db))
        setup.configure([dict(tool='claude', name='仅 Claude', home=str(self.claude))])
        self.assertEqual(history_ui.list_rows(self.db), [])


    def test_invalid_selection_does_not_publish_partial_registry(self):
        original = (ah.ROOT / 'connections.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'config.toml'):
            setup.configure([dict(tool='codex', name='错误入口', home=str(self.root))])
        self.assertEqual((ah.ROOT / 'connections.json').read_bytes(), original)
        self.assertFalse((ah.DATA / 'sources.json').exists())


class SettingsFormTests(Fixture):
    def setUp(self):
        super().setUp()
        import tkinter as tk
        try:
            self.tk = tk
            self.window = tk.Tk()
            self.window.withdraw()
        except tk.TclError:
            self.skipTest('No Tk desktop')

    def tearDown(self):
        if hasattr(self, 'window'):
            self.window.destroy()
        super().tearDown()

    def test_manager_save_edit_order_default_check_and_delete(self):
        from settings_ui import ProfileManager
        manager = ProfileManager(self.window)
        for key, value in dict(id='first', name='第一入口', type='codex', connection='a', cwd_policy='chosen').items():
            manager.values[key].set(value)
        manager.flags['enabled'].set(True)
        manager.flags['favorite'].set(True)
        manager.flags['default'].set(True)
        manager.save()
        self.window.update()
        self.assertEqual(profiles.get('profile:first')['name'], '第一入口')
        manager.new()
        for key, value in dict(id='second', name='第二入口', type='claude', cwd_policy='chosen').items():
            manager.values[key].set(value)
        manager.flags['default'].set(True)
        manager.save()
        self.window.update()
        self.assertFalse(profiles.get('profile:first')['default'])
        self.assertEqual(backend.launch_targets()[0], 'profile:second')
        manager.move(-1)
        self.window.update()
        self.assertEqual(profiles.get('profile:second')['order'], 0)
        manager.values['name'].set('已编辑')
        manager.save()
        self.window.update()
        self.assertEqual(profiles.get('profile:second')['name'], '已编辑')
        with patch.object(backend, 'check_profile', return_value=dict(message='检查通过')) as check:
            manager.check()
            check.assert_called_once()
            self.assertEqual(manager.message.get(), '检查通过')
        manager.values['id'].set('bad id')
        manager.save()
        self.assertIn('id:', manager.message.get())
        self.assertEqual(len(profiles.load()[0]), 2)
        manager.remove()
        self.window.update()
        self.assertEqual(len(profiles.load()[0]), 1)

    def test_wizard_select_rename_apply_scope_and_callback(self):
        from settings_ui import SetupWizard
        changed = []
        wizard = SetupWizard(self.window, lambda: changed.append(True))
        wizard.checked = {key for key, row in wizard.rows.items() if row['tool'] == 'claude'}
        key = next(iter(wizard.checked))
        wizard.tree.selection_set(key)
        self.window.update()
        wizard.name.set('我的 Claude')
        wizard.apply()
        self.assertEqual(changed, [True])
        self.assertEqual(setup.load_sources()['codex'], [])
        self.assertEqual(setup.load_sources()['claude'], [str(self.claude)])
