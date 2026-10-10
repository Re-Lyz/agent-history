"""Unpack and smoke-check a source release without touching user configuration."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

artifact=Path(sys.argv[1]).resolve()
assert artifact.with_suffix(artifact.suffix+'.sha256').read_text().split()[0] == hashlib.sha256(artifact.read_bytes()).hexdigest()
with tempfile.TemporaryDirectory(prefix='agent-history-portable-') as folder:
    base=Path(folder).resolve()
    with zipfile.ZipFile(artifact) as archive:
        names=archive.namelist()
        assert all('agent-history/'+name in names for name in ('LICENSE','NOTICE.md','VERSION','README.en.md','CHANGELOG.md'))
        assert not any(name.endswith(('auth.json','history.db','connections.json')) for name in names)
        assert b'\r\n' not in archive.read('agent-history/integrations/install.sh')
        archive.extractall(base)
    source=base/'agent-history'
    subprocess.run([sys.executable,'-m','compileall','-q',str(source)],check=True)
    command=[sys.executable,str(source/'install.py'),'--user-home',str(base/'user'),
             '--prefix',str(base/'安装 中文 空格'),'--dry-run']
    result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',check=True,timeout=30,
                          env={**os.environ,'PYTHONIOENCODING':'utf-8'})
    plan=json.loads(result.stdout)
    assert plan['tasks']==[] and not any(plan['features'].values())
    assert not (base/'user/.codex/config.toml').exists()
    assert not (base/'安装 中文 空格').exists()
    print(json.dumps(dict(portable_unpacked=True,preflight=True,compile=True,dry_run=True,
                         lf_shell=True,checksum=True,files=len(names))))
