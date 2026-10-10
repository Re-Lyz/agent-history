"""Run Linux checks with temporary fixtures and report the actual environment."""
import os
from pathlib import Path
import platform
import shutil
import sys
import tkinter as tk
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
print('Linux:', platform.platform(), 'Python:', sys.version.split()[0], flush=True)
print('Programs:', {name: shutil.which(name) for name in ('codex','claude','node','npm','xterm','gnome-terminal')}, flush=True)
root=tk.Tk(); root.withdraw()
print('Tk:',root.tk.call('info','patchlevel'),flush=True); root.destroy()
import markdown_preview
print('Markdown:', markdown_preview.available(), flush=True)
suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]) if len(sys.argv)>1 else \
      unittest.defaultTestLoader.discover(str(ROOT/'tests'),top_level_dir=str(ROOT))
result=unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(not result.wasSuccessful())
