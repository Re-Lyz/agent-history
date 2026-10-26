"""Official fallback authenticity, archive boundaries and recoverable pointers."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from history_app import release_install as releases
from history_app.paths import RuntimePaths


class ReleaseInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.paths = RuntimePaths(self.base / 'shared', self.base / 'data', self.base / 'prefix', self.base / 'user')
        self.root = self.base / 'packages/standalone'
        self.old = self.root / 'releases/old'
        (self.old / 'bin').mkdir(parents=True)
        self.current = self.root / 'current'
        self.link(self.current, self.old)
        machine = {'amd64': 'x86_64', 'x86_64': 'x86_64', 'arm64': 'aarch64', 'aarch64': 'aarch64'}[platform.machine().lower()]
        self.triple = machine + '-' + {'Windows': 'pc-windows-msvc', 'Linux': 'unknown-linux-musl', 'Darwin': 'apple-darwin'}[platform.system()]
        self.name = 'codex-' + self.triple + ('.exe.zip' if os.name == 'nt' else '.tar.gz')
        self.binary = 'codex-' + self.triple + ('.exe' if os.name == 'nt' else '')

    def link(self, link, target):
        if os.name == 'nt':
            script = self.base / 'link.ps1'
            script.write_text('param($Link,$Target)\nNew-Item -ItemType Junction -Path $Link -Target $Target | Out-Null\n')
            subprocess.run(['powershell.exe', '-NoProfile', '-File', str(script), str(link), str(target)],
                           check=True, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            os.symlink(target, link)

    def archive(self):
        stream = io.BytesIO()
        if os.name == 'nt':
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr(self.binary, b'synthetic-new-binary')
                archive.writestr('../outside-file', b'never extract me')
        else:
            with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                entry = tarfile.TarInfo(self.binary)
                entry.size = len(b'synthetic-new-binary')
                archive.addfile(entry, io.BytesIO(b'synthetic-new-binary'))
                entry = tarfile.TarInfo('../outside-file')
                entry.size = 0
                archive.addfile(entry, io.BytesIO(b''))
        return stream.getvalue()

    def metadata(self, digest):
        return json.dumps(dict(tag_name='rust-v1.2.3', assets=[dict(name=self.name, digest=digest,
            browser_download_url='https://github.com/openai/codex/releases/download/rust-v1.2.3/' + self.name)])).encode()

    def test_bad_digest_never_changes_pointer_or_publishes_executable(self):
        data = self.archive()
        with patch.object(releases, '_read', side_effect=[self.metadata('sha256:' + '0' * 64), data]):
            with self.assertRaisesRegex(ValueError, '摘要不匹配'):
                releases.fallback(self.paths, self.root)
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual(list((self.root / 'releases').iterdir()), [self.old])

    def test_missing_official_digest_refuses_unsigned_fallback(self):
        with patch.object(releases, '_read', return_value=self.metadata('')) as read:
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                releases.fallback(self.paths, self.root)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.current.resolve(), self.old)

    def test_verified_binary_switch_preserves_old_release_and_does_not_extract_paths(self):
        data = self.archive()
        with patch.object(releases, '_read', side_effect=[self.metadata('sha256:' + hashlib.sha256(data).hexdigest()), data]), \
             patch('history_app.programs.version', return_value=dict(state='ok', version='1.2.3')):
            result = releases.fallback(self.paths, self.root)
        self.assertNotEqual(self.current.resolve(), self.old)
        self.assertEqual(Path(result['previous_pointer']).resolve(), self.old)
        binary = self.current / 'bin' / ('codex.exe' if os.name == 'nt' else 'codex')
        self.assertEqual(binary.read_bytes(), b'synthetic-new-binary')
        self.assertFalse(list(self.base.rglob('outside-file')))

    def test_concurrent_pointer_change_is_kept_instead_of_overwritten(self):
        data = self.archive()
        other = self.root / 'releases/other-installer'
        other.mkdir()
        def changed(*args, **kwargs):
            self.current.rename(self.root / 'external-previous')
            self.link(self.current, other)
            return dict(state='ok', version='1.2.3')
        with patch.object(releases, '_read', side_effect=[self.metadata('sha256:' + hashlib.sha256(data).hexdigest()), data]), \
             patch('history_app.programs.version', side_effect=changed):
            with self.assertRaisesRegex(ValueError, '下载期间变化'):
                releases.fallback(self.paths, self.root)
        self.assertEqual(self.current.resolve(), other)
        self.assertTrue(self.old.is_dir())
