"""Verify a real Linux terminal, argv, cwd, environment, exit reporting and installer.

Provide an installed xterm or AGENT_HISTORY_TERMINAL JSON executable prefix.
All histories, shortcuts and program output are created in a temporary directory.
"""
import contextlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_history as ah
import install
import launch_backend as backend
import launch_profiles as profiles
import launch_jobs

if len(sys.argv) > 1:
    os.environ['AGENT_HISTORY_TERMINAL'] = json.dumps([sys.argv[1], '-e'])

with tempfile.TemporaryDirectory(prefix='agent-history-linux-') as temporary:
    root = Path(temporary)
    chosen = root / "中文 项目 '引号' $ &"
    chosen.mkdir()
    shared = root / 'config'; shared.mkdir()
    (shared / 'connections.json').write_text('{"connections":{}}')
    ah.ROOT, ah.DATA, ah.CLAUDE_HOMES = shared, root / 'data', []
    output = root / 'terminal-result.json'
    script = root / "中文 recorder '$.py"
    script.write_text('import json,os,sys\nfrom pathlib import Path\n'
        'Path(sys.argv[1]).write_text(json.dumps(dict(args=sys.argv[2:],cwd=os.getcwd(),'
        'value=os.environ["VERIFY_VALUE"]),ensure_ascii=False))\nsys.exit(23)\n')
    args = ['中文 空格', 'a"b', "a'b", '$(touch should-not-exist); & | %']
    profiles.save([dict(id='terminal', name='验证', type='custom', program=sys.executable,
        args=[str(script),str(output),*args], env={'VERIFY_VALUE':'中文 环境'})])
    prepared = backend.prepare_new('profile:terminal', str(chosen))
    prepared['kind'] = 'custom'
    prepared['job'] = launch_jobs.create(dict(path=str(script), tool='custom', id='fixture'), prepared, 'profile:terminal')
    prepared['env'].update(AGENT_HISTORY_JOB_ID=prepared['job'],AGENT_HISTORY_HOME=str(ah.DATA),
                           AGENT_HISTORY_SHARED_ROOT=str(ah.ROOT))
    backend.start_terminal(prepared['cwd'],prepared['inner'],env=prepared['env'])
    deadline = time.monotonic()+30
    while time.monotonic()<deadline:
        if output.exists() and launch_jobs.read(prepared['job']).get('exit_code') == 23:
            break
        time.sleep(.1)
    else:
        raise AssertionError('Terminal did not finish: '+repr(launch_jobs.read(prepared['job'])))
    result = json.loads(output.read_text())
    assert result == dict(args=args,cwd=str(chosen),value='中文 环境'), result
    assert not (chosen/'should-not-exist').exists()
    # Core CLI works without a graphical session and without real source discovery.
    (ah.DATA/'sources.json').write_text(json.dumps(dict(version=1,codex=[],claude=[])))
    headless={**os.environ, 'DISPLAY':'', 'WAYLAND_DISPLAY':'', 'AGENT_HISTORY_HOME':str(ah.DATA),
              'AGENT_HISTORY_SHARED_ROOT':str(shared)}
    command=[sys.executable,str(Path(ah.__file__)),'--data-dir',str(ah.DATA)]
    for operation in (['collect'],['sessions'],['search','synthetic-no-match']):
        completed=subprocess.run(command+operation,env=headless,capture_output=True,text=True,timeout=20)
        assert completed.returncode==0,completed.stderr
    user = root/'user'; prefix = root/'portable 中文 空格'
    spec = install.plan(prefix,user,sys.executable,desktop=True)
    install.install(spec); install.install(spec)
    assert (prefix/'codexui').stat().st_mode & 0o111
    assert (user/'.local/share/applications/agent-history.desktop').exists()
    keep = user/'.agent-history/keep.json'; keep.parent.mkdir(parents=True); keep.write_text('preserve')
    install.uninstall(prefix)
    assert keep.read_text() == 'preserve'
    assert not (user/'.local/share/applications/agent-history.desktop').exists()
    print(json.dumps(dict(terminal=True,argv=True,environment=True,unicode_cwd=True,exit_code=23,
                         install_repeat=True,uninstall_preserves_history=True,headless_cli=True)))
