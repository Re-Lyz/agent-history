"""Regression checks for publication gates, using fake values and temporary Git."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

from tests.release_audit import audit, findings


class AuditTests(unittest.TestCase):
    def test_reports_categories_without_exposing_values(self):
        fake = 'sk-' + 'z'*30
        data = ('token='+fake+'\n'+'api_key="'+'opaque-secret-value'+'"').encode()
        result = findings('README.md', data)
        self.assertEqual({x['category'] for x in result}, {'credential-token','credential-value'})
        self.assertNotIn(fake,json.dumps(result))
        self.assertFalse(findings('test.py', b'password="never persist"'))
        self.assertFalse(findings('template.toml', b'api_key="YOUR_KEY_HERE"'))
        private = b'C:'+bytes([92])+b'Users'+bytes([92])+b'synthetic-person'+bytes([92])+b'project'
        self.assertEqual(findings('README.md',private)[0]['category'],'private-user-path')

    def test_deleted_historical_secret_and_private_artifact_are_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            def git(*args):
                return subprocess.run(['git',*args],cwd=root,check=True,capture_output=True)
            git('init')
            git('config','user.name','Synthetic test')
            git('config','user.email','synthetic@example.invalid')
            path = root/'old.md'
            path.write_text('ghp_'+'z'*30)
            git('add','old.md'); git('commit','-m','Synthetic old blob')
            path.unlink()
            (root/'README.md').write_text('Generated public data')
            git('add','-A'); git('commit','-m','Remove old blob')
            artifact=root/'package.zip'
            with zipfile.ZipFile(artifact,'w') as archive:
                archive.writestr('agent-history/auth.json','{}')
                archive.writestr('../outside.md','synthetic')
            report = audit(root,artifact)
            self.assertFalse(report['passed'])
            self.assertTrue(any(x['scope']=='history' and x['category']=='credential-token' for x in report['findings']))
            self.assertTrue(any(x['category']=='runtime-or-private-file' for x in report['findings']))
            self.assertTrue(any(x['category']=='unsafe-archive-path' for x in report['findings']))
