"""Synthetic executable discovery for tests that never invoke an Agent."""
import os
import shutil
import sys
from unittest.mock import patch


def programs(case, folder):
    folder = folder / 'fixture-bin'
    folder.mkdir()
    for name in ('codex', 'claude', 'xterm'):
        if os.name == 'nt':
            # Discoverable native placeholders; unit tests never run an Agent.
            shutil.copyfile(sys.executable, folder / (name + '.exe'))
        else:
            path = folder / name
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
    environment = patch.dict(os.environ, PATH=str(folder) + os.pathsep + os.environ.get('PATH', ''))
    environment.start()
    case.addCleanup(environment.stop)
