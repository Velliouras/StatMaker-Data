"""UEFA V2 source contract: exact bookmaker provenance, no invented prices."""
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from scripts.betting_v2.uefa_quoted_inventory import exact_uefa_quotes


class UefaQuotedInventoryTest(unittest.TestCase):
    def _payload(self):
        return {
            "schemaVersion": 1,
            "leagueCode": "CL",
            "season": "2026-2027",
            "generatedAt": "2026-10-12T14:00:00Z",
            "matches": [{
                "id": "real-odds-event-123", "date": "2026-10-15",
                "kickoff": "2026-10-15T20:00:00Z",
                "homeTeam": "Arsenal", "awayTeam": "Sporting CP",
                "markets": [
                    {"market": "1X2", "selection": "Home", "odds": 2.15,
                     "bookmaker": "Bet365", "exactBookmakerOdds": True},
                    {"market": "DOUBLE_CHANCE", "selection": "1X", "odds": 1.15,
                     "bookmaker": "Bet365", "exactBookmakerOdds": True},
                    {"market": "1X2", "selection": "Away", "odds": 2.85,
                     "bookmaker": "Unibet", "exactBookmakerOdds": False}
                ]
            }]
        }

    def test_exact_quoted_event_and_local_immutable_selection_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "champions_league_odds.json").write_text(json.dumps(self._payload()))
            now = datetime(2026, 10, 12, 16, tzinfo=timezone.utc)
            offers, reasons = exact_uefa_quotes(root, "generation-1", now)
            self.assertEqual(len(offers), 1)
            quoted = offers[0]
            self.assertEqual(quoted["fixtureId"], "real-odds-event-123")
            self.assertEqual(quoted["leagueCode"], "CL")
            self.assertEqual(quoted["marketModelKey"], "RESULT_1X2")
            self.assertTrue(quoted["selectionId"].startswith("uefa-local:"))
            self.assertTrue(quoted["externalQuoteSource"]["exactBookmakerOdds"])
            self.assertEqual(quoted["externalQuoteSource"]["bookmaker"], "Bet365")
            self.assertEqual(len(quoted["externalQuoteSource"]["sourceSha256"]), 64)
            self.assertGreaterEqual(reasons["CL_OUTSIDE_UAT_PRICE_BAND"], 1)
            self.assertGreaterEqual(reasons["CL_NOT_EXACT_BOOKMAKER"], 1)

    def test_source_is_rejected_if_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "champions_league_odds.json").write_text(json.dumps(self._payload()))
            now = datetime(2026, 10, 16, 16, tzinfo=timezone.utc)
            offers, reasons = exact_uefa_quotes(root, "generation-1", now)
            self.assertEqual(offers, [])
            self.assertEqual(reasons["CL_STALE_OR_INVALID_QUOTES"], 1)


if __name__ == "__main__":
    unittest.main()
