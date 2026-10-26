"""Bounded native transcript reads for previews; full validation stays separate."""
import json
import os
from collections import OrderedDict
from pathlib import Path
import threading

import agent_history as ah
import tool_details


class ReadCancelled(Exception):
    """An obsolete, read-only request stopped at a safe boundary."""


def checkpoint(cancel=None):
    if cancel and cancel():
        raise ReadCancelled()


class MessagePage(list):
    def __init__(self, messages=(), cursor=None):
        super().__init__(messages)
        self.cursor = cursor

    @property
    def has_more(self):
        return self.cursor is not None


def message_events(tool, path, record, stamp, *, offset=None, ordinal=None):
    descriptors = iter(tool_details.descriptors(tool, record))
    result = []
    for event in ah.PARSERS[tool](record):
        if event[0] != 'msg':
            continue
        message = dict(role=event[1], text=event[2], ts=event[3])
        if event[1] in ('tool_call', 'tool_output'):
            descriptor = next(descriptors, {})
            message.update(call_id=descriptor.get('call_id'), tool_name=descriptor.get('name', '工具详情'),
                           detail=dict(tool=tool, path=str(path), offset=offset, ordinal=ordinal,
                                       stamp=stamp, role=event[1], block=descriptor.get('block')))
        result.append(message)
    return result


def positioned_records(path, byte_limit=None, ordinal_limit=None, cancel=None):
    """Yield offsets of complete records, with the same prefix rules as native reads."""
    with Path(path).open('rb') as stream:
        size = min(os.fstat(stream.fileno()).st_size, byte_limit if byte_limit is not None else 2**63)
        ordinal = 0
        while stream.tell() < size and (ordinal_limit is None or ordinal < ordinal_limit):
            checkpoint(cancel)
            offset = stream.tell()
            line = stream.readline(size - offset)
            if not line.endswith(b'\n'):
                break
            yield offset, ordinal, json.loads(line)
            ordinal += 1


def reverse_lines(path, end, cancel=None):
    """Read complete JSONL lines backwards in bounded chunks, ignoring a partial tail."""
    with Path(path).open('rb') as stream:
        position, fragments = end, []
        skip_tail = True
        while position:
            checkpoint(cancel)
            start = max(0, position - 65536)
            stream.seek(start)
            chunk = stream.read(position - start)
            parts = chunk.split(b'\n')
            cursor = position
            for part in reversed(parts[1:]):
                cursor -= len(part)
                if skip_tail:
                    skip_tail = False
                else:
                    yield cursor, part + b''.join(reversed(fragments))
                fragments.clear()
                cursor -= 1
            fragments.append(parts[0])
            position = start
        if not skip_tail:
            yield 0, b''.join(reversed(fragments))


_prefixes = OrderedDict()
_prefix_lock = threading.Lock()


def prefix_end(path, stamp, end, ordinal_limit, cancel):
    key = (str(path), *stamp, end, ordinal_limit)
    with _prefix_lock:
        if key in _prefixes:
            _prefixes.move_to_end(key)
            return _prefixes[key]
    with Path(path).open('rb') as stream:
        ordinal = 0
        while stream.tell() < end and ordinal < ordinal_limit:
            checkpoint(cancel)
            line = stream.readline(end - stream.tell())
            if not line.endswith(b'\n'):
                break
            ordinal += 1
        bound = stream.tell() if ordinal == ordinal_limit else end
    with _prefix_lock:
        _prefixes[key] = bound
        while len(_prefixes) > 128:
            _prefixes.popitem(last=False)
    return bound


def file_message_page(row, limit=400, cursor=None, cancel=None, homes=None, provenance=False):
    """Read a tail page across bounded fork ancestors without parsing the whole log.

    A cursor identifies a chain entry, record byte offset and event boundary, so a
    multi-event Claude record can straddle pages without loss or duplication.
    """
    if limit < 1:
        raise ValueError('Message page size must be positive.')
    checkpoint(cancel)
    if row['tool'] == 'codex':
        home = (dict(ah.codex_homes()) if homes is None else homes).get(row['source'])
        if home is None:
            raise ValueError('来源连接已不存在。')
        chain = ah.codex_import.ancestry(home, row['path'])
    else:
        chain = [(Path(row['path']), None, None)]
    start = cursor[0] if cursor else len(chain) - 1
    newest = []
    next_cursor = None
    for index in range(start, -1, -1):
        path, byte_limit, ordinal_limit = chain[index]
        stat = Path(path).stat()
        stamp = (stat.st_size, stat.st_mtime_ns)
        end = min(stat.st_size, byte_limit if byte_limit is not None else stat.st_size)
        # An ordinal-only native marker needs one bounded prefix scan. Both
        # markers apply when present, exactly as in codex_import.records().
        if ordinal_limit is not None:
            end = prefix_end(path, stamp, end, ordinal_limit, cancel)
        if cursor and index == start:
            end = min(end, cursor[1]) if cursor[2] == 0 else min(end, cursor[3])
        for offset, line in reverse_lines(path, end, cancel):
            checkpoint(cancel)
            events = message_events(row['tool'], path, json.loads(line), stamp, offset=offset)
            if provenance:
                for event_index, event in enumerate(events):
                    event['locator'] = dict(path=str(path), offset=offset, event=event_index, stamp=stamp)
            if cursor and index == start and offset == cursor[1] and cursor[2]:
                events = events[:cursor[2]]
            for event_index in range(len(events) - 1, -1, -1):
                if len(newest) == limit:
                    next_cursor = (index, offset, event_index + 1, offset + len(line) + 1)
                    break
                newest.append(events[event_index])
            if next_cursor is not None:
                break
        if next_cursor is not None:
            break
    return MessagePage(reversed(newest), next_cursor)
