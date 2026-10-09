"""A fixture can carry one Strong recommendation per market family, not one total."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import materialize_canonical_recommendation_ledger as producer
import reconcile_canonical_ledger_identity as identity


def candidate(family, selection, generation=100, match_key="2026-10-09|Home FC|Away FC"):
    return {
        "competitionId": "domestic",
        "localDate": "2026-10-09",
        "matchKey": match_key,
        "homeTeam": "Home FC",
        "awayTeam": "Away FC",
        "homeNames": ["Home FC"],
        "awayNames": ["Away FC"],
        "marketFamily": family,
        "market": family.upper().replace(" ", "_"),
        "selection": selection,
        "generationBuiltAtMs": generation,
    }


class CanonicalDailyMultiMarketTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            candidate("Team Corners", "Over 8.5"),
            candidate("Team Shots on Target", "Under 6.5"),
            candidate("Match Goals", "Under 3.5"),
            candidate("Team Corners", "Over 9.5", 200),
        ]

    def test_producer_preserves_three_markets_on_one_fixture(self):
        result = producer.merge(self.rows)
        self.assertEqual(len(result), 3)
        self.assertEqual({r["marketFamily"] for r in result},
                         {"Team Corners", "Team Shots on Target", "Match Goals"})
        self.assertEqual(next(r["selection"] for r in result
                              if r["marketFamily"] == "Team Corners"), "Over 9.5")

    def test_identity_repair_preserves_three_markets_on_one_fixture(self):
        result = identity.deduplicate_market_families(self.rows)
        self.assertEqual(len(result), 3)
        self.assertEqual({r["marketFamily"] for r in result},
                         {"Team Corners", "Team Shots on Target", "Match Goals"})

    def test_different_fixtures_remain_independent(self):
        rows = self.rows + [candidate("Match Goals", "Over 1.5",
                                      match_key="2026-10-09|Another FC|Other FC")]
        self.assertEqual(len(producer.merge(rows)), 4)
        self.assertEqual(len(identity.deduplicate_market_families(rows)), 4)

    def test_missing_fixture_identity_is_ignored_by_producer(self):
        invalid = candidate("Match Goals", "Over 2.5")
        invalid["awayNames"] = ["Home FC"]
        invalid["awayTeam"] = "Home FC"
        self.assertEqual(len(producer.merge(self.rows + [invalid])), 3)


if __name__ == "__main__":
    unittest.main()
