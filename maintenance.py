"""Compact immutable snapshots and remove old, reproducible projections.

Preview by default. Apply under the collector lock; native histories, logical
revisions, import mappings, trash, and native backups are never removed.
"""
import argparse
import contextlib
import json
from pathlib import Path
import time

import agent_history as ah
import conversation_store as store


def compact(db, *, apply=False, now=None):
    now = time.time() if now is None else now
    revisions = db.execute('SELECT * FROM conversation_revisions ORDER BY created,rowid').fetchall()
    legacy = [item for item in revisions if Path(item['snapshot']).suffix == '.json']
    projections = [path for path in (ah.DATA / 'handoffs' / 'projections').glob('*.jsonl')
                   if now - path.stat().st_mtime > 7 * 86400]
    before = sum(Path(item['snapshot']).stat().st_size for item in revisions)
    report = dict(applied=apply, legacy_snapshots=len(legacy), converted=0,
                  snapshot_bytes_before=before, snapshot_bytes_after=before,
                  old_projections=len(projections), projection_bytes=sum(p.stat().st_size for p in projections))
    if not apply:
        return report
    store.read_snapshot.cache_clear()
    for item in legacy:
        old = Path(item['snapshot'])
        value = store.revision(db, item['id'])
        previous = store.revision(db, item['parent']) if item['parent'] else None
        new = old.with_suffix('.json.gz')
        store.write_snapshot(new, value, previous)
        store.read_snapshot.cache_clear()
        restored = store.read_snapshot(str(new))
        # Verify all semantic fields before publishing the new storage path.
        if {k: v for k, v in restored.items() if k != 'depth'} != \
                {k: v for k, v in value.items() if k != 'depth'}:
            raise ValueError('Snapshot verification failed: ' + item['id'])
        with db:
            db.execute('UPDATE conversation_revisions SET snapshot=? WHERE id=?', (str(new), item['id']))
        old.unlink()
        report['converted'] += 1
    for path in projections:
        path.unlink()
    report['snapshot_bytes_after'] = sum(Path(row[0]).stat().st_size
                                        for row in db.execute('SELECT snapshot FROM conversation_revisions'))
    store.read_snapshot.cache_clear()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply the previewed maintenance.')
    parser.add_argument('--data-dir', type=Path, help='Override the local runtime data directory.')
    args = parser.parse_args()
    if args.data_dir:
        ah.DATA = args.data_dir.resolve()
    with ah.collect_lock(wait=True) as locked:
        if not locked:
            raise TimeoutError('History collector is busy.')
        with contextlib.closing(ah.connect()) as db:
            print(json.dumps(compact(db, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
