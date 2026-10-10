"""Build a portable source ZIP from Git's reviewed source list, never runtime data."""
import argparse
import hashlib
from pathlib import Path
import subprocess


def build(output):
    root = Path(__file__).resolve().parent.parent
    paths = subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True, check=True).stdout.decode('utf-8').split('\0')
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Archive the index tree: normalized LF for Linux scripts and exactly the
    # staged/committed source, without untracked runtime files or unstaged edits.
    tree = subprocess.run(['git','write-tree'],cwd=root,capture_output=True,text=True,check=True).stdout.strip()
    selected = [relative for relative in filter(None,paths) if not any(
        name in relative.lower() for name in ('auth.json','.env','history.db'))]
    required = {'LICENSE', 'NOTICE.md', 'VERSION', 'README.md', 'README.en.md', 'CHANGELOG.md'}
    if not required.issubset(selected):
        raise ValueError('Release metadata must be staged before building: '+', '.join(sorted(required-set(selected))))
    subprocess.run(['git','archive','--format=zip','--prefix=agent-history/','--output',str(output),tree,*selected],
                   cwd=root,check=True)
    output.with_suffix(output.suffix+'.sha256').write_text(hashlib.sha256(output.read_bytes()).hexdigest()+'  '+output.name+'\n',encoding='ascii')
    return output


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=Path('dist/agent-history-portable.zip'))
    print(build(p.parse_args().output))
