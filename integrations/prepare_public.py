"""Create a new, audited public Git snapshot without rewriting local history.

Only staged / committed files enter this snapshot. Existing output is refused.
This command does not add a remote, push, tag or publish a release.
"""
import argparse
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.build_portable import build
from tests.release_audit import audit


def prepare(output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Output already exists; choose a new directory.')
    archive = build(ROOT / 'dist/public-source.zip')
    report = audit(ROOT, archive, history=False)
    if not report['passed']:
        raise ValueError('Source/artifact audit failed; run tests/release_audit.py for locations.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        # Strip the single known project prefix; audit rejected unsafe paths.
        for entry in source.infolist():
            if entry.is_dir():
                continue
            relative = Path(entry.filename).relative_to('agent-history')
            path = output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read(entry))
    def git(*args):
        subprocess.run(['git', *args], cwd=output, check=True, capture_output=True)
    git('init', '-b', 'main')
    git('add', '.')
    version = (output / 'VERSION').read_text().strip()
    # Explicit public identity, no dependency on a machine's private git email.
    git('-c', 'user.name=Agent History contributors',
        '-c', 'user.email=125275523+Re-Lyz@users.noreply.github.com',
        'commit', '-m', 'Prepare Agent History '+version)
    public = audit(output)
    if not public['passed']:
        raise ValueError('New public Git history audit failed; do not publish this directory.')
    print('Prepared audited public snapshot: '+str(output))
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/public-beta')
    prepare(parser.parse_args().output)
