import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import build_domestic_normalized_snapshot as snapshot
from scripts.restore_archived_stats import merge_archived_normalized


class HalfCornerSnapshotTest(unittest.TestCase):
    def test_provider_half_counts_are_a_separate_optional_version_two_trailer(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.json'
            target = Path(directory) / 'snapshot.bin'
            source.write_text(json.dumps({'fixtures': [{'fixture_id': 1, 'league_id': 197,
                'date': '2026-11-01', 'league_code': 'G1', 'home_team': 'Home', 'away_team': 'Away',
                'stats': {'HxG': 1.2, 'HC1H': 0, 'AC1H': 3}}]}))
            with patch.object(sys, 'argv', ['snapshot', str(source), str(target)]):
                snapshot.main()
            with target.open('rb') as handle:
                magic, version, count = struct.unpack('>iii', handle.read(12))
                self.assertEqual((magic, version), (0x534D4453, 2))
                # The old 28-field reader consumes every legacy row unchanged.
                for _ in range(count):
                    length = struct.unpack('>H', handle.read(2))[0]
                    handle.read(length)
                    numbers = []
                    for _ in range(28):
                        present = handle.read(1) == b'\1'
                        numbers.append(struct.unpack('>d', handle.read(8))[0] if present else None)
                    self.assertEqual(numbers[0], 1.2)
                self.assertEqual(snapshot.read_half_corner_tail(handle)['api_197_1'], (0.0, 3.0))

    def test_archive_restore_retains_period_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'snapshot.bin'
            with target.open('wb') as handle:
                handle.write(struct.pack('>iii', 0x534D4453, 2, 1))
                snapshot.write_utf(handle, 'current')
                handle.write(b'\0' * 28)
                snapshot.write_half_corner_tail(handle, {'current': (0, 2)})
            merge_archived_normalized(target, {'archive': (None,) * 28})
            with target.open('rb') as handle:
                self.assertEqual(struct.unpack('>iii', handle.read(12))[2], 2)
                for _ in range(2):
                    length = struct.unpack('>H', handle.read(2))[0]
                    handle.read(length)
                    self.assertEqual(handle.read(28), b'\0' * 28)
                self.assertEqual(snapshot.read_half_corner_tail(handle), {'current': (0, 2)})

    def test_absent_trailer_and_missing_values_remain_unknown(self):
        self.assertEqual(snapshot.read_half_corner_tail(io.BytesIO()), {})
        self.assertEqual(snapshot.half_corner_rows([{'fixture_id': 1, 'league_id': 197,
                         'stats': {'HC': 5, 'AC': 4}}]), {})

    def test_bad_trailer_is_rejected(self):
        with self.assertRaises(ValueError):
            snapshot.read_half_corner_tail(io.BytesIO(b'wrong data'))


if __name__ == '__main__':
    unittest.main()
