import base64
import gzip
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / 'scripts/ai_analyst/job_transfer.py'
spec = importlib.util.spec_from_file_location('job_transfer', MODULE)
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


class JobEvidenceTransferTest(unittest.TestCase):
    def test_round_trip_nested_unicode_evidence_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'; source.mkdir()
            (source/'reviews').mkdir()
            (source/'reviews/one.json').write_text('{"review":"Παναθηναϊκός"}')
            (source/'status.json').write_text('{"count":1}')
            before = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
            encoded, digest = transfer.pack(source)
            target = Path(tmp)/'target'
            self.assertEqual(2, transfer.unpack(encoded, digest, target))
            self.assertEqual(before, {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()})
            self.assertEqual((encoded, digest), transfer.pack(source))

    def test_rejects_changed_sha_and_oversized_job_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source'; source.mkdir(); (source/'status').write_text('ok')
            encoded, digest = transfer.pack(source)
            with self.assertRaisesRegex(ValueError, 'SHA'):
                transfer.unpack(encoded, '0'*64, Path(tmp)/'target')
            with self.assertRaisesRegex(ValueError, 'limit'):
                transfer.unpack('x'*(transfer.MAX_OUTPUT+1), digest, Path(tmp)/'target')

    def test_rejects_path_escape_before_writing_files(self):
        for name in ('../escape', '/absolute', 'reviews/../../escape', 'reviews\\escape'):
            raw = json.dumps({name: base64.b64encode(b'bad').decode()}).encode()
            compressed = gzip.compress(raw)
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, 'path'):
                    transfer.unpack(base64.b64encode(compressed).decode(), hashlib.sha256(compressed).hexdigest(), Path(tmp)/'target')

    def test_rejects_source_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source'; source.mkdir()
            (source/'other').symlink_to(Path(tmp)/'outside')
            with self.assertRaisesRegex(ValueError, 'symlink'): transfer.pack(source)


if __name__ == '__main__':
    unittest.main()
