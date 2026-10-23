"""Compare warm listing, tail previews and tool reads on isolated synthetic data.

No user histories, Agent processes or model requests are used.
Run: python tests/benchmark_reading.py --files 1000 --messages 20000 --repeat 3
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_history as ah
import history_ui as ui
import launch_backend as backend
import tool_details
from tests.test_trash import Fixture, rollout


def measure(work, repeat):
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        work()
        samples.append(time.perf_counter() - started)
    return dict(median_seconds=statistics.median(samples), samples_seconds=samples)


def benchmark(files, messages, repeat):
    fixture = Fixture()
    fixture.setUp()
    try:
        for index in range(files):
            path = fixture.a / 'sessions' / f'benchmark-{index}.jsonl'
            path.write_text(rollout(f'benchmark-{index}', 'Synthetic list entry', fixture.cwd), encoding='utf-8')
        ah.collect(fixture.db)
        def old_listing():
            ui.list_rows(fixture.db, include_imports=True, include_internal=True, limit=None)
            ui.list_rows(fixture.db, limit=None)
        def paged_listing():
            ui.project_choices(fixture.db)
            ui.list_rows(fixture.db, indexed=True, logical=True, limit=500)
        result = dict(synthetic=True, files=files + 1, messages=messages, repeat=repeat, model_requests=0,
                      listing_before=measure(old_listing, repeat), listing_paged=measure(paged_listing, repeat))
        with fixture.path.open('w', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(type='session_meta', payload=dict(id='s1', cwd=fixture.cwd, source='cli'))) + '\n')
            for index in range(messages):
                stream.write(json.dumps(dict(type='response_item', payload=dict(type='message', role='user',
                    content=[dict(type='input_text', text=f'Synthetic message {index} ' + 'body ' * 20)]))) + '\n')
            stream.write(json.dumps(dict(type='response_item', payload=dict(type='function_call_output',
                call_id='benchmark-tool', output='Synthetic full output ' + 'z' * 50_000))) + '\n')
        row = dict(ah.resolve(fixture.db, 's1'))
        result['preview_full'] = measure(lambda: backend.file_messages(row), repeat)
        result['preview_tail_400'] = measure(lambda: backend.file_message_page(row), repeat)
        locator = backend.file_message_page(row)[-1]['detail']
        legacy = {key: value for key, value in locator.items() if key != 'offset'}
        # Only the old ordinal locator needs one scan to know its ordinal.
        legacy['ordinal'] = messages + 1
        result['tool_ordinal'] = measure(lambda: tool_details.read(legacy), repeat)
        result['tool_offset'] = measure(lambda: tool_details.read(locator), repeat)
        ah.require_collected(ah.collect(fixture.db, only_paths={fixture.path}))
        def append_index(incremental):
            with fixture.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(type='response_item', payload=dict(type='message', role='assistant',
                    content=[dict(type='output_text', text='Synthetic appended message')]))) + '\n')
            if not incremental:
                with fixture.db:
                    fixture.db.execute('DELETE FROM file_view_incremental WHERE path=?', (str(fixture.path),))
            ah.require_collected(ah.collect(fixture.db, only_paths={fixture.path}))
        result['append_rebuild'] = measure(lambda: append_index(False), repeat)
        result['append_incremental'] = measure(lambda: append_index(True), repeat)
        print(json.dumps(result))
    finally:
        fixture.tearDown()
        fixture.doCleanups()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', type=int, default=1000)
    parser.add_argument('--messages', type=int, default=20000)
    parser.add_argument('--repeat', type=int, default=3)
    args = parser.parse_args()
    if args.files < 0 or args.messages < 0 or args.repeat < 1:
        parser.error('Counts must be nonnegative; repeat must be positive.')
    benchmark(args.files, args.messages, args.repeat)
