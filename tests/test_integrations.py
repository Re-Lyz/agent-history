import json
from pathlib import Path
import tempfile
import unittest
from integrations.install_mcp import install


class IntegrationTests(unittest.TestCase):
    def test_mcp_and_hook_registration_preserves_other_tools_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            user, repo, shared = base / 'user', base / 'source', base / 'config'
            (user / '.claude').mkdir(parents=True)
            mcp_path = user / '.claude.json'
            settings_path = user / '.claude' / 'settings.json'
            other = {'command': 'other-tool', 'args': ['serve']}
            mcp_path.write_text(json.dumps({'mcpServers': {'other': other}, 'theme': 'dark'}))
            settings_path.write_text(json.dumps({'permissions': {'allow': ['Read']}, 'hooks': {
                'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo keep'},
                                          {'type': 'command', 'command': 'python old/agent_history.py sync-claude'}]}]}}))
            install(repo, 'python.exe', user, shared)
            first = settings_path.read_bytes()
            install(repo, 'python.exe', user, shared)
            self.assertEqual(settings_path.read_bytes(), first)
            mcp = json.loads(mcp_path.read_text())
            self.assertEqual(mcp['mcpServers']['other'], other)
            self.assertEqual(mcp['theme'], 'dark')
            self.assertEqual(mcp['mcpServers']['agent-history']['args'],
                             [str(repo / 'agent_history.py'), '--shared-root', str(shared), 'mcp'])
            settings = json.loads(settings_path.read_text())
            self.assertEqual(settings['permissions'], {'allow': ['Read']})
            hooks = [h for group in settings['hooks']['SessionStart'] for h in group['hooks']]
            self.assertEqual(len(hooks), 2)
            self.assertEqual(hooks[0]['command'], 'echo keep')
            self.assertIn(str(repo / 'agent_history.py'), hooks[1]['command'])
            self.assertTrue(hooks[1]['async'])


if __name__ == '__main__':
    unittest.main()
