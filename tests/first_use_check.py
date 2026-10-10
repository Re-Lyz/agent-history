"""Exercise an extracted release with a fresh temporary user, never real homes.

python tests/first_use_check.py PATH_TO_RELEASE.zip
Requires real Tk (xvfb-run on headless Linux). It uses the selected interpreter's
installed dependencies; a fresh venv must be prepared separately. Not a fresh OS
or real Agent login test. Native Codex compatibility scripts remain separate.
"""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import zipfile


def run_source(base):
    source=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(source))
    home,prefix=base/'user',base/'安装 中文 空格'
    shared,data=prefix/'config',home/'.agent-history'
    for key in ('HOME','USERPROFILE'):
        os.environ[key]=str(home)
    for key in ('CODEX_HOME','CODEX_SQLITE_HOME','CLAUDE_CONFIG_DIR','AGENT_HISTORY_TERMINAL'):
        os.environ.pop(key,None)
    os.environ.update(AGENT_HISTORY_SHARED_ROOT=str(shared),AGENT_HISTORY_HOME=str(data),PYTHONIOENCODING='utf-8')
    home.mkdir()
    def install(root=source,*extra):
        result=subprocess.run([sys.executable,str(root/'install.py'),'--user-home',str(home),
             '--prefix',str(prefix),*extra],capture_output=True,text=True,encoding='utf-8',timeout=60)
        if result.returncode:
            raise RuntimeError('Isolated installer failed: '+result.stderr[-1000:])
        return result.stdout
    install(source,'--dry-run')
    assert not prefix.exists()
    install(); install()
    assert not (home/'.codex/config.toml').exists()
    assert not (home/'.claude.json').exists()
    codex=home/'.codex'; (codex/'sessions').mkdir(parents=True)
    config='model="demo-model"\n[features]\nmemories=false\n'
    (codex/'config.toml').write_text(config,encoding='utf-8')
    original_config=(codex/'config.toml').read_bytes()
    auth=b'{"synthetic":true}\n'; (codex/'auth.json').write_bytes(auth)
    sid='00000000-0000-4000-8000-000000000002'
    stamp=dt.datetime.now(dt.timezone.utc).isoformat()
    records=[dict(type='session_meta',timestamp=stamp,payload=dict(id=sid,cwd=str(base),source='cli')),
             dict(type='response_item',timestamp=stamp,payload=dict(type='message',role='user',
                content=[dict(type='input_text',text='Synthetic first-use question')])),
             dict(type='response_item',timestamp=stamp,payload=dict(type='message',role='assistant',
                content=[dict(type='output_text',text='# Synthetic answer\n\n**Markdown** works.\n\n```python\nprint(1)\n```')]))]
    transcript=codex/'sessions/demo.jsonl'
    transcript.write_text(''.join(json.dumps(r)+'\n' for r in records),encoding='utf-8')
    baseline=transcript.read_bytes()
    import agent_history as ah
    import setup_backend
    ah.CLAUDE_HOMES=[]
    discovered=setup_backend.discover(home)['candidates']
    candidate=next(c for c in discovered if Path(c['home'])==codex)
    candidate['name']='Synthetic Codex'
    setup_backend.configure([candidate])
    assert (codex/'config.toml').read_bytes()==original_config
    assert (codex/'auth.json').read_bytes()==auth
    with contextlib.closing(ah.connect()) as db:
        ah.collect(db)
        assert ah.resolve(db,sid)['id']==sid
    import history_ui as ui
    import tkinter as tk
    from tkinter import ttk
    root=tk.Tk(); root.withdraw()
    errors=[]
    def mainloop(window):
        def widgets():
            found=[]; pending=[root]
            while pending:
                widget=pending.pop(); found.append(widget); pending.extend(widget.winfo_children())
            return found
        tree=next(w for w in widgets() if isinstance(w,ttk.Treeview) and 'check' in w.cget('columns'))
        deadline=time.monotonic()+20
        selected=False
        while time.monotonic()<deadline:
            root.update()
            if tree.get_children() and not selected:
                tree.selection_set(tree.get_children()[0]); tree.event_generate('<<TreeviewSelect>>'); selected=True
            if selected and any(isinstance(w,tk.Text) and 'Synthetic answer' in w.get('1.0','end') for w in widgets()):
                break
            time.sleep(0.02)
        else:
            raise TimeoutError('Isolated first-use viewer did not render history')
        mode=next(w for w in widgets() if isinstance(w,ttk.Combobox) and w.winfo_name()=='preview_mode')
        assert mode.get()=='Markdown'
        root.tk.call(root.protocol('WM_DELETE_WINDOW'))
    from tkinter import messagebox
    with patch.object(tk,'Tk',return_value=root),patch.object(type(root),'mainloop',mainloop), \
            patch.object(ui,'start_terminal',side_effect=AssertionError('Must not start an Agent')), \
            patch.object(messagebox,'showerror',side_effect=lambda *args: errors.append(args)):
        ui.run_ui()
    assert not errors
    assert (data/'ui-preferences.json').exists()
    before=(data/'history.db').read_bytes()
    original_sources=(data/'sources.json').read_bytes()
    # Optional configuration registrations use only the synthetic user home.
    install(source,'--codex-mcp','--claude-mcp','--claude-hook')
    install(source,'--codex-mcp','--claude-mcp','--claude-hook')
    assert 'agent-history' in (codex/'config.toml').read_text(encoding='utf-8')
    assert json.loads((home/'.claude.json').read_text(encoding='utf-8'))['mcpServers']['agent-history']
    upgrade=base/'updated-source'
    shutil.copytree(source,upgrade,ignore=shutil.ignore_patterns('__pycache__','.git','.venv','dist'))
    install(upgrade)
    manifest=json.loads((prefix/'installation.json').read_text(encoding='utf-8'))
    assert manifest['root']==str(upgrade)
    assert (data/'sources.json').read_bytes()==original_sources
    assert (data/'history.db').read_bytes()==before
    install(upgrade,'--uninstall','--dry-run')
    assert (prefix/'installation.json').exists()
    install(upgrade,'--uninstall')
    assert (data/'history.db').read_bytes()==before
    assert (data/'sources.json').read_bytes()==original_sources
    assert transcript.read_bytes()==baseline
    assert (codex/'auth.json').read_bytes()==auth
    assert (codex/'config.toml').read_text(encoding='utf-8').strip()==config.strip()
    assert not json.loads((home/'.claude.json').read_text(encoding='utf-8')).get('mcpServers',{}).get('agent-history')
    assert not json.loads((home/'.claude/settings.json').read_text(encoding='utf-8')).get('hooks',{}).get('SessionStart')
    assert not any(Path(path).exists() for path in manifest['files'])
    print(json.dumps(dict(fresh_user=True,install=True,repeat_install=True,source_discovery=True,
        configuration=True,markdown_viewer=True,optional_integrations=True,upgrade=True,
        uninstall=True,user_data_preserved=True,real_agent_login=False,fresh_os=False)))


def run_archive(artifact):
    artifact=Path(artifact).resolve()
    assert artifact.with_suffix(artifact.suffix+'.sha256').read_text().split()[0]==hashlib.sha256(artifact.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='agent-history-first-use-') as folder:
        base=Path(folder)
        with zipfile.ZipFile(artifact) as archive:
            for name in archive.namelist():
                path=Path(name)
                if path.is_absolute() or '..' in path.parts:
                    raise ValueError('Unsafe ZIP entry')
            archive.extractall(base)
        source=base/'agent-history'
        environment={**os.environ,'PYTHONIOENCODING':'utf-8'}
        subprocess.run([sys.executable,str(source/'tests/first_use_check.py'),'--source-check',str(base)],
                       check=True,env=environment,cwd=source,timeout=180)


if __name__=='__main__':
    if sys.argv[1]=='--source-check': run_source(Path(sys.argv[2]))
    else: run_archive(sys.argv[1])
