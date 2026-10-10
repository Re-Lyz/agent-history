import csv
import json
from pathlib import Path
import tempfile
import unittest
import audit_history as audit


class AuditEvidenceTests(unittest.TestCase):
    def test_hash_distinguishes_prefix_from_different_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b, c = [root / name for name in ('short', 'long', 'different')]
            a.write_bytes(b'first\n')
            b.write_bytes(b'first\nsecond\n')
            c.write_bytes(b'other\n')
            self.assertNotEqual(audit.stable_hash(a), audit.stable_hash(b))
            self.assertEqual(audit.stable_hash(a), audit.stable_hash(b, a.stat().st_size))
            self.assertNotEqual(audit.stable_hash(c), audit.stable_hash(b, c.stat().st_size))
            self.assertIsNone(audit.stable_hash(root / 'missing'))

    def test_empty_excludes_inherited_history_images_and_tool_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollout.jsonl'
            meta = {'type': 'session_meta', 'payload': {'id': 'empty'}}
            path.write_text(json.dumps(meta) + '\n')
            self.assertTrue(audit.genuinely_empty(path, meta['payload']))
            self.assertFalse(audit.genuinely_empty(path, {'history_base': {'thread_id': 'parent'}}))
            self.assertFalse(audit.genuinely_empty(path, {'history_mode': 'paginated'}))
            self.assertTrue(audit.genuinely_empty(path, {'history_mode': 'paginated'}, native_verified_empty=True))
            path.write_text(json.dumps(meta) + '\n{"type":"response_item",')
            self.assertFalse(audit.genuinely_empty(path, meta['payload']))
            for payload in ({'type': 'message', 'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,x'}]},
                            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '<task>important request</task>'}]},
                            {'type': 'function_call_output', 'output': 'important result'}):
                path.write_text(json.dumps(meta) + '\n' + json.dumps({'type': 'response_item', 'payload': payload}) + '\n')
                self.assertFalse(audit.genuinely_empty(path, meta['payload']))

    def test_references_and_csv_keep_identifiers_and_neutralize_formulas(self):
        sid = '01a01e23-81a2-71a3-afc7-0ef8ed979bc9'
        self.assertEqual(list(audit.references({'source': {'parent_thread_id': sid}})), [sid])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'review.csv'
            audit.write_csv(path, [{'id': sid, 'title': '=HYPERLINK("bad")'}], ['id', 'title'])
            with path.open(encoding='utf-8-sig', newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row['id'], sid)
            self.assertTrue(row['title'].startswith("'="))


if __name__ == '__main__':
    unittest.main()
