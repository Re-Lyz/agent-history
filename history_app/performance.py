"""Bounded, numeric performance diagnostics; never accept paths or transcript text."""
from contextlib import contextmanager
import datetime
import json
import math
from pathlib import Path
import threading
import time
import uuid

PHASES = frozenset(('listing', 'reader', 'search', 'render', 'launch-preview',
                    'collection', 'sync', 'lock-wait', 'tool-detail'))
COUNTS = frozenset(('files', 'messages', 'bytes', 'failed', 'matches'))
_lock = threading.Lock()
_values = {}
_identity = uuid.uuid4().hex
_flushed = {}


def record(phase, seconds, **counts):
    if phase not in PHASES or not math.isfinite(seconds) or seconds < 0:
        return
    milliseconds = round(seconds * 1000, 3)
    with _lock:
        value = _values.setdefault(phase, dict(samples=0, total_ms=0, max_ms=0))
        value.update(samples=value['samples'] + 1, total_ms=round(value['total_ms'] + milliseconds, 3),
                     max_ms=max(value['max_ms'], milliseconds), last_ms=milliseconds,
                     updated=datetime.datetime.now(datetime.timezone.utc).isoformat())
        for key, count in counts.items():
            if key in COUNTS and isinstance(count, int) and count >= 0:
                value[key] = value.get(key, 0) + count


@contextmanager
def measure(phase, **counts):
    started = time.monotonic()
    try:
        yield
    finally:
        record(phase, time.monotonic() - started, **counts)


def snapshot():
    with _lock:
        return {phase: dict(value) for phase, value in _values.items()}


def flush(data, force=False):
    """Called by workers, never by Tk callbacks. Failure cannot fail an operation."""
    folder = Path(data) / 'performance'
    now = time.monotonic()
    with _lock:
        if not force and now - _flushed.get(folder, 0) < 1:
            return
        _flushed[folder] = now
    try:
        from setup_backend import atomic_json
        atomic_json(folder / (_identity + '.json'), snapshot())
    except (OSError, ValueError):
        pass


def summary(data):
    result = snapshot()
    folder = Path(data) / 'performance'
    try:
        files = sorted(folder.glob('*.json'), key=lambda path: path.stat().st_mtime, reverse=True)[:32]
    except OSError:
        files = []
    for path in files:
        if path.stem == _identity:
            continue
        try:
            if path.stat().st_size > 32768:
                continue
            values = json.loads(path.read_text(encoding='utf-8'))
            for phase, raw in values.items():
                if phase not in PHASES or not isinstance(raw, dict):
                    continue
                # Explicit field/type allowlist also protects diagnostic exports
                # if a metric file was replaced with arbitrary JSON.
                clean = {key: value for key, value in raw.items()
                         if key in COUNTS | {'samples', 'total_ms', 'max_ms', 'last_ms'}
                         and type(value) in (int, float) and math.isfinite(value) and value >= 0}
                value = result.setdefault(phase, dict(samples=0, total_ms=0, max_ms=0))
                for key in COUNTS | {'samples', 'total_ms'}:
                    value[key] = value.get(key, 0) + clean.get(key, 0)
                value['max_ms'] = max(value['max_ms'], clean.get('max_ms', 0))
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return result
