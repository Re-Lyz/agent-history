"""Synthetic executable discovery for tests that never invoke an Agent."""
import os
from unittest.mock import patch


def programs(case, folder):
    if os.name == 'nt':
        return
    folder = folder / 'fixture-bin'
    folder.mkdir()
    for name in ('codex', 'claude', 'xterm'):
        path = folder / name
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
    environment = patch.dict(os.environ, PATH=str(folder) + os.pathsep + os.environ.get('PATH', ''))
    environment.start()
    case.addCleanup(environment.stop)
