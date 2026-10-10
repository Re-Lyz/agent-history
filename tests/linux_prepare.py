"""Opt-in, isolated Linux validation dependencies; never install into system paths.

Run explicitly in WSL/Linux. The cache contains Python wheels and a pinned Codex
binary from the official npm package, not credentials or personal histories.
"""
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request

root = Path(__file__).resolve().parents[1]
cache = Path.home() / '.cache/agent-history-verification'
cache.mkdir(parents=True, exist_ok=True)
deps = cache / 'deps'
subprocess.run([sys.executable, '-m', 'pip', 'install', '--upgrade', '--target', str(deps),
                '-r', str(root / 'requirements-ui.txt')], check=True)
package = json.load(urllib.request.urlopen('https://registry.npmjs.org/@openai%2fcodex/0.159.2', timeout=30))
dependency = package['optionalDependencies']['@openai/codex-linux-x64']
name, version = dependency.removeprefix('npm:').rsplit('@', 1)
metadata = json.load(urllib.request.urlopen('https://registry.npmjs.org/' + name.replace('/', '%2f') + '/' + version, timeout=30))
archive = cache / 'codex.tgz'
urllib.request.urlretrieve(metadata['dist']['tarball'], archive)
with tarfile.open(archive) as source:
    member = next(item for item in source if item.isfile() and item.name.rsplit('/', 1)[-1] == 'codex')
    binary = cache / 'bin/codex'
    binary.parent.mkdir(exist_ok=True)
    binary.write_bytes(source.extractfile(member).read())
    binary.chmod(0o755)
print('Dependencies:', deps)
print('Native CLI:', subprocess.check_output([str(binary), '--version'], text=True).strip())
