"""Opt-in local Linux beta rehearsal in a new venv, using an existing CLI cache.

This is a local helper, not required by the product or GitHub CI. Dependencies
install only into dist/linux-beta-venv. No system packages or real homes change.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
cache=Path.home()/'.cache/agent-history-verification'
venv=root/'dist/linux-beta-venv'
if '--native-tail' not in sys.argv:
    subprocess.run([sys.executable,'-m','venv',str(venv)],check=True)
python=venv/'bin/python'
environment=dict(os.environ)
environment.pop('PYTHONPATH',None)
if '--native-tail' not in sys.argv:
    subprocess.run([str(python),'-m','pip','install','-r',str(root/'requirements-ui.txt')],check=True,env=environment)
environment['PATH']=str(cache/'bin')+os.pathsep+environment.get('PATH','')
terminal=cache/'terminal/usr/bin/xterm'
environment.pop('AGENT_HISTORY_TERMINAL',None)
environment['LD_LIBRARY_PATH']=str(cache/'terminal/usr/lib/x86_64-linux-gnu')+os.pathsep+environment.get('LD_LIBRARY_PATH','')
environment['PYTHONIOENCODING']='utf-8'
checks = [
    ['tests/linux_check.py'],
    ['tests/portable_check.py','dist/agent-history-0.1.0-beta.1.zip'],
    ['tests/first_use_check.py','dist/agent-history-0.1.0-beta.1.zip'],
    *[['tests/'+name+'.py'] for name in ('verify_ui_resume','verify_claude_to_codex','verify_handoff','verify_temporary_handoff')],
    ['tests/linux_terminal_check.py',str(terminal)],
]
for args in checks[-3:] if '--native-tail' in sys.argv else checks:
    if args[0].startswith('tests/verify_') or args[0]=='tests/linux_terminal_check.py':
        environment['AGENT_HISTORY_TERMINAL']=json.dumps([str(terminal),'-e'])
    subprocess.run([str(python),*args],check=True,cwd=root,env=environment)
