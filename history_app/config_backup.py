"""Immutable app-configuration snapshots; never restore transcripts or databases."""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from setup_backend import atomic_json
from .configuration import valid


def fingerprint(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('配置文件是符号链接，请先核对其位置。')
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, None
    return hashlib.sha256(data).hexdigest(), data


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            staged = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        staged.chmod(0o600)
        os.replace(staged, path)
    finally:
        if staged:
            staged.unlink(missing_ok=True)


def backup(paths):
    """Keep even malformed configuration bytes for diagnosis; snapshots are private."""
    identity = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:12]
    folder = paths.backups / identity
    folder.mkdir(parents=True, mode=0o700)
    manifest = dict(version=1, id=identity, created=dt.datetime.now(dt.timezone.utc).isoformat(),
                    shared=str(paths.shared.resolve()), data=str(paths.data.resolve()), files={})
    for name, path in paths.configuration().items():
        digest, data = fingerprint(path)
        manifest['files'][name] = dict(sha256=digest, size=len(data) if data is not None else 0)
        if data is not None:
            _write(folder / (name + '.bin'), data)
    atomic_json(folder / 'manifest.json', manifest)  # Published last: incomplete backups cannot be selected.
    (folder / 'manifest.json').chmod(0o600)
    return identity


def inventory(paths):
    values = []
    for item in paths.backups.glob('*/manifest.json'):
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            value = json.loads(item.read_text(encoding='utf-8'))
            if value['version'] == 1 and value['id'] == item.parent.name:
                values.append(dict(id=value['id'], created=value['created']))
    return sorted(values, key=lambda value: value['id'], reverse=True)


def _load(paths, identity):
    if not isinstance(identity, str) or not re.fullmatch(r'\d{8}-\d{6}-[a-f0-9]{12}', identity):
        raise ValueError('备份编号无效。')
    folder = paths.backups / identity
    if folder.is_symlink():
        raise ValueError('备份目录无效。')
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != 1 or manifest.get('id') != identity or \
            manifest.get('shared') != str(paths.shared.resolve()) or manifest.get('data') != str(paths.data.resolve()) or \
            set(manifest.get('files', {})) != set(paths.configuration()):
        raise ValueError('备份不属于当前配置目录，或备份清单无效。')
    payload = {}
    for name, item in manifest['files'].items():
        digest = item['sha256']
        if digest is None:
            if item['size'] != 0:
                raise ValueError('备份清单无效。')
            payload[name] = None
            continue
        actual, data = fingerprint(folder / (name + '.bin'))
        if actual != digest or data is None or len(data) != item['size']:
            raise ValueError('备份完整性校验失败，未修改配置。')
        try:
            value = json.loads(data.decode('utf-8-sig'))
            if not valid(name, value):
                raise ValueError()
        except (ValueError, UnicodeError) as error:
            raise ValueError('备份中含有无效配置，未恢复。') from error
        payload[name] = data
    return payload


def preview_restore(paths, identity):
    payload = _load(paths, identity)
    expected, changes = {}, []
    for name, path in paths.configuration().items():
        current, data = fingerprint(path)
        expected[name] = current
        if data != payload[name]:
            changes.append(dict(name=name, action='还原' if payload[name] is not None else '移出当前配置'))
    return dict(id=identity, expected=expected, changes=changes)


def restore(paths, preview):
    """Validate all bytes before writes; preserve a new snapshot before any change."""
    from .operations import idle_operation
    with idle_operation(paths):
        return _restore(paths, preview)


def _restore(paths, preview):
    payload = _load(paths, preview['id'])
    targets = paths.configuration()
    before = {name: fingerprint(path) for name, path in targets.items()}
    if {name: value[0] for name, value in before.items()} != preview['expected']:
        raise ValueError('配置在预览后发生变化，请重新预览。')
    recovery = backup(paths)
    applied = []
    try:
        for name, path in targets.items():
            if fingerprint(path)[0] != before[name][0]:
                raise ValueError('配置正在被其他进程修改，已停止恢复。')
            if payload[name] == before[name][1]:
                continue
            if payload[name] is None:
                path.unlink(missing_ok=True)  # Previous bytes remain in the immutable recovery snapshot.
            else:
                _write(path, payload[name])
            applied.append(name)
    except (OSError, ValueError):
        for name in reversed(applied):
            # Never overwrite a concurrent edit while rolling back our own writes.
            if fingerprint(targets[name])[1] == payload[name]:
                if before[name][1] is None:
                    targets[name].unlink(missing_ok=True)
                else:
                    _write(targets[name], before[name][1])
        raise
    return dict(restored=len(applied), recovery_backup=recovery)
