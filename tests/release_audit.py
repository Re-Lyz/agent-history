"""Audit source, every reachable Git blob and release ZIP; never print secret values.

This is a reproducible heuristic check, not proof that arbitrary prose is public.
Reports contain locations and categories only. Review documentation and synthetic
image provenance separately before publication.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'credential-token': re.compile(r'(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,}|AKIA[A-Z0-9]{16})'),
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'credential-value': re.compile(r'''(?i)["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)["']?\s*[:=]\s*["']([^"'\r\n]{8,})["']'''),
    'private-user-path': re.compile(r'(?i)(?:[A-Z]:[\\/]+Users[\\/]+|/hom[e]/)(?!<|\{|\$|YOUR_USER|example|test-user|user(?:[\\/]|$))[^\s\\/"\'<>]+'),
    'embedded-auth-url': re.compile(r'https?://[^\s/:]+:[^\s/@]+@'),
}
PLACEHOLDERS = re.compile(r'(?i)^(?:example|placeholder|dummy|fake|test|your[_-]|\$|\{|<|env:|os\.|MY_|never persist$)')
TEXT = {'.py', '.ps1', '.sh', '.md', '.json', '.toml', '.yml', '.yaml', '.txt', '.svg', '.gitattributes', '.gitignore'}


def findings(path, data):
    name = PurePosixPath(path).name.lower()
    parts = {p.lower() for p in PurePosixPath(path).parts}
    issues = []
    if name in {'auth.json', 'connections.json', 'common.toml', 'profile.toml', 'installation.json'} or \
            name.startswith('.env') or name.endswith(('.db', '.sqlite', '.sqlite3', '.jsonl', '.log', '.pem', '.p12')) or \
            parts & {'.git', 'trash', 'native-backups', 'sessions', 'archived_sessions', 'node_modules'}:
        issues.append(dict(category='runtime-or-private-file', line=None))
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == '.png':
        return issues  # Visual provenance must be reviewed, not interpreted as text.
    if suffix not in TEXT and name not in {'license', 'version', '.gitignore', '.gitattributes'} and data:
        issues.append(dict(category='unexpected-file-type', line=None))
    try:
        content = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return issues + [dict(category='non-utf8-content', line=None)]
    for line_number, line in enumerate(content.splitlines(), 1):
        for category, pattern in PATTERNS.items():
            for match in pattern.finditer(line):
                if category == 'credential-value' and PLACEHOLDERS.search(match.group(1)):
                    continue
                issues.append(dict(category=category, line=line_number))
    return issues


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root)


def audit(root=ROOT, artifact=None, history=True):
    root = Path(root)
    result = dict(version=1, heuristic=True, scope=dict(history=history, artifact=bool(artifact)),
                  commits=0, history_blobs=0, current_files=0, artifact_files=0, findings=[])
    def inspect(scope, path, data, blob=None):
        for issue in findings(path, data):
            result['findings'].append(dict(scope=scope, path=path, blob=blob, **issue))
    paths = git(root, 'ls-files', '-z').decode('utf-8').split('\0')
    for path in filter(None, paths):
        inspect('current', path, (root/path).read_bytes())
        result['current_files'] += 1
    if history:
        result['commits'] = int(git(root, 'rev-list', '--all', '--count'))
        object_paths = {}
        for commit in git(root, 'rev-list', '--all').decode('ascii').splitlines():
            for item in filter(None, git(root, 'ls-tree', '-r', '-z', commit).split(b'\0')):
                metadata, path = item.split(b'\t', 1)
                oid = metadata.decode('ascii').split()[2]
                object_paths.setdefault(oid, set()).add(path.decode('utf-8'))
        # rev-list --objects gives each object once; cat-file identifies blobs.
        objects = git(root, 'rev-list', '--objects', '--all').decode('utf-8').splitlines()
        process = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=root,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            for item in objects:
                oid, _, path = item.partition(' ')
                process.stdin.write((oid+'\n').encode('ascii')); process.stdin.flush()
                header = process.stdout.readline().decode('ascii').split()
                if len(header) != 3:
                    raise ValueError('Cannot read Git object '+oid)
                data = process.stdout.read(int(header[2])); process.stdout.read(1)
                if header[1] == 'blob':
                    for location in sorted(object_paths.get(oid, {path})):
                        inspect('history', location, data, oid)
                    result['history_blobs'] += 1
                elif header[1] in ('commit', 'tag'):
                    inspect('history-metadata', 'git-message.md', data, oid)
        finally:
            process.stdin.close(); process.stdout.close(); process.wait()
    if artifact:
        artifact = Path(artifact)
        result['artifact_sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        with zipfile.ZipFile(artifact) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                path = PurePosixPath(entry.filename)
                if path.is_absolute() or '..' in path.parts:
                    result['findings'].append(dict(scope='artifact',path=entry.filename,category='unsafe-archive-path',line=None))
                inspect('artifact', entry.filename, archive.read(entry))
                result['artifact_files'] += 1
    result['passed'] = not result['findings']
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--artifact', type=Path)
    parser.add_argument('--no-history', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = audit(args.root, args.artifact, not args.no_history)
    body = json.dumps(report, ensure_ascii=False, indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(body,encoding='utf-8')
    print(json.dumps(dict(passed=report['passed'], commits=report['commits'],
                         blobs=report['history_blobs'], current_files=report['current_files'],
                         artifact_files=report['artifact_files'], findings=len(report['findings']))))
    raise SystemExit(0 if report['passed'] else 1)
