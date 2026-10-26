"""Verified official standalone fallback, preserving previous releases/pointers."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tarfile
import urllib.request
import uuid
import zipfile

MAX_DOWNLOAD = 200_000_000


def _read(url, limit):
    request = urllib.request.Request(url, headers={'User-Agent': 'agent-history-maintenance'})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('下载超过大小限制。')
    return data


def fallback(paths, installation):
    root = Path(installation).resolve()
    current = root / 'current'
    is_link = current.is_symlink() or bool(getattr(current.lstat(), 'st_file_attributes', 0) & 0x400)
    if root.name != 'standalone' or root.parent.name != 'packages' or not is_link:
        raise ValueError('独立安装位置不受支持，未替换原程序。')
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError('当前入口指向安装目录外，未替换。')
    expected_target = current.resolve()
    machine = {'amd64': 'x86_64', 'x86_64': 'x86_64', 'arm64': 'aarch64', 'aarch64': 'aarch64'}.get(platform.machine().lower())
    system = {'Windows': 'pc-windows-msvc', 'Linux': 'unknown-linux-musl', 'Darwin': 'apple-darwin'}.get(platform.system())
    if not machine or not system:
        raise ValueError('此平台没有已登记的独立发行格式。')
    triple = machine + '-' + system
    suffix = '.exe.zip' if os.name == 'nt' else '.tar.gz'
    name = 'codex-' + triple + suffix
    release = json.loads(_read('https://api.github.com/repos/openai/codex/releases/latest', 2_000_000))
    tag = release.get('tag_name', '')
    if not re.fullmatch(r'rust-v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', tag):
        raise ValueError('官方发行标签无效。')
    asset = next((item for item in release.get('assets', []) if item.get('name') == name), None)
    digest = (asset or {}).get('digest', '')
    expected_url = 'https://github.com/openai/codex/releases/download/' + tag + '/' + name
    if not asset or asset.get('browser_download_url') != expected_url or not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest):
        raise ValueError('官方资源缺失或没有可核对的 SHA-256；未安装。')
    data = _read(expected_url, MAX_DOWNLOAD)
    if hashlib.sha256(data).hexdigest().lower() != digest[7:].lower():
        raise ValueError('下载摘要不匹配；未安装。')
    folder = paths.data / 'maintenance/downloads' / uuid.uuid4().hex
    folder.mkdir(parents=True)
    archive = folder / name
    archive.write_bytes(data)
    # Select one regular binary; never extract paths, symlinks or archive scripts.
    binary_name = 'codex-' + triple + ('.exe' if os.name == 'nt' else '')
    if suffix == '.exe.zip':
        with zipfile.ZipFile(archive) as bundle:
            entries = [item for item in bundle.infolist() if Path(item.filename).name in (binary_name, 'codex.exe') and not item.is_dir()]
            if len(entries) != 1 or entries[0].file_size > MAX_DOWNLOAD:
                raise ValueError('发行包可执行文件不唯一或过大。')
            content = bundle.read(entries[0])
    else:
        with tarfile.open(archive) as bundle:
            entries = [item for item in bundle if Path(item.name).name in (binary_name, 'codex') and item.isfile()]
            if len(entries) != 1 or entries[0].size > MAX_DOWNLOAD:
                raise ValueError('发行包可执行文件不唯一或过大。')
            content = bundle.extractfile(entries[0]).read(MAX_DOWNLOAD + 1)
    target = root / 'releases' / (tag[6:] + '-' + triple + '-history-' + uuid.uuid4().hex[:8])
    assert target.absolute().is_relative_to(root)
    target.mkdir(parents=True)
    executable = target / 'bin' / ('codex.exe' if os.name == 'nt' else 'codex')
    executable.parent.mkdir()
    with executable.open('xb') as stream:
        stream.write(content)
    executable.chmod(0o755)
    from . import programs
    actual = programs.version(executable, home=folder)
    if actual['version'] != tag[6:]:
        raise ValueError('下载程序的版本校验失败；旧入口仍然保留。')
    from .locks import file_lock
    with file_lock(root / 'install.lock', wait=False) as locked:
        if not locked or current.resolve() != expected_target:
            raise ValueError('安装入口在下载期间变化或正在更新，请重新预览。')
        backup = _publish_pointer(root, target, folder)
    return dict(version=actual['version'], previous_pointer=str(backup), archive=str(archive))


def _publish_pointer(root, target, folder):
    current = root / 'current'
    stage = root / ('current-history-' + uuid.uuid4().hex[:8])
    backup = root / ('previous-history-' + uuid.uuid4().hex[:8])
    if os.name == 'nt':
        script = folder / 'junction.ps1'
        script.write_text('param([string]$Link,[string]$Target)\n$ErrorActionPreference="Stop"\n'
                          'New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null\n', encoding='utf-8')
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(script), str(stage), str(target)],
                       check=True, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        # Move only verified junction entries, never recurse through a release directory.
        current.rename(backup)
        try:
            stage.rename(current)
        except OSError:
            backup.rename(current)
            raise
    else:
        os.symlink(os.readlink(current), backup)
        os.symlink(target, stage)
        os.replace(stage, current)
    return backup
