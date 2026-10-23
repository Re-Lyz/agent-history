"""Measure one owned conversation's exit sync among synthetic unrelated files.

Does not start an Agent, access user histories or make model requests.
Run with: python tests/benchmark_sync.py --files 400 --repeat 3
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_history as ah
import codex_import as ci
import single_conversation
import launch_backend as backend
import launch_jobs as jobs
from tests.test_trash import Fixture, rollout


def benchmark(files, repeat):
    fixture = Fixture()
    fixture.setUp()
    try:
        for index in range(files):
            path = fixture.a / 'sessions' / f'benchmark-{index}.jsonl'
            path.write_text(rollout(f'benchmark-{index}', 'Synthetic unrelated conversation', fixture.cwd),
                            encoding='utf-8')
        ah.collect(fixture.db)
        prepared = backend.prepare_launch(fixture.db, dict(ah.resolve(fixture.db, 's1')), 'a', fixture.cwd)
        jobs.update(prepared['job'], state='exited', exit_code=0)
        samples = []
        with patch.object(ci, 'first_meta', wraps=ci.first_meta) as headers:
            for _ in range(repeat):
                started = time.perf_counter()
                single_conversation.finish(fixture.db, prepared['job'])
                samples.append(time.perf_counter() - started)
        print(json.dumps(dict(synthetic=True, unrelated_files=files, samples_seconds=samples,
                              median_seconds=statistics.median(samples),
                              header_reads_per_sync=headers.call_count / repeat, model_requests=0)))
    finally:
        fixture.tearDown()
        fixture.doCleanups()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', type=int, default=400)
    parser.add_argument('--repeat', type=int, default=3)
    args = parser.parse_args()
    if args.files < 0 or args.repeat < 1:
        parser.error('files must be nonnegative and repeat must be positive')
    benchmark(args.files, args.repeat)
