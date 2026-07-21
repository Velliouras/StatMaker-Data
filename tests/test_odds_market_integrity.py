import unittest

from scripts.odds_market_integrity import (
    dedupe_markets_by_bookmaker,
    sanitize_payload,
)


class OddsMarketIntegrityTest(unittest.TestCase):
    def test_rejects_inverted_unibet_curve_and_keeps_bet365(self):
        payload = {
            "bookmakersRequested": ["Bet365", "Unibet"],
            "matches": [{
                "id": "72177122",
                "homeTeam": "SK Sturm Graz",
                "awayTeam": "Heart of Midlothian FC",
                "markets": [
                    {"market": "MATCH_GOALS", "selection": "Over", "odds": 1.22, "bookmaker": "Bet365", "line": 1.5},
                    {"market": "MATCH_GOALS", "selection": "Over", "odds": 1.75, "bookmaker": "Bet365", "line": 2.5},
                    {"market": "MATCH_GOALS", "selection": "Over", "odds": 2.80, "bookmaker": "Bet365", "line": 3.5},
                    {"market": "MATCH_GOALS", "selection": "Under", "odds": 4.00, "bookmaker": "Bet365", "line": 1.5},
                    {"market": "MATCH_GOALS", "selection": "Under", "odds": 2.05, "bookmaker": "Bet365", "line": 2.5},
                    {"market": "MATCH_GOALS", "selection": "Under", "odds": 1.40, "bookmaker": "Bet365", "line": 3.5},
                    {"market": "MATCH_GOALS", "selection": "Over", "odds": 1.96, "bookmaker": "Unibet", "line": 1.5},
                    {"market": "MATCH_GOALS", "selection": "Over", "odds": 1.70, "bookmaker": "Unibet", "line": 2.5},
                    {"market": "MATCH_GOALS", "selection": "Over", "odds": 2.90, "bookmaker": "Unibet", "line": 3.5},
                ],
            }],
        }
        sanitized, report = sanitize_payload(payload)
        markets = sanitized["matches"][0]["markets"]
        self.assertTrue(markets)
        self.assertEqual({"Bet365"}, {item["bookmaker"] for item in markets})
        over_15 = next(item for item in markets if item["selection"] == "Over" and item["line"] == 1.5)
        self.assertEqual(1.22, over_15["odds"])
        group = report["matches"][0]["groups"][0]
        self.assertEqual("Bet365", group["bookmaker"])
        self.assertEqual("integrity", group["rejected"]["Unibet"])

    def test_parser_dedupe_preserves_both_bookmakers_for_same_line(self):
        rows = [
            {"market": "MATCH_GOALS", "selection": "Over", "odds": 1.22, "bookmaker": "Bet365", "line": 1.5},
            {"market": "MATCH_GOALS", "selection": "Over", "odds": 1.96, "bookmaker": "Unibet", "line": 1.5},
            {"market": "MATCH_GOALS", "selection": "Over", "odds": 1.25, "bookmaker": "Bet365", "line": 1.5},
        ]
        result = dedupe_markets_by_bookmaker(rows)
        self.assertEqual(2, len(result))
        by_book = {item["bookmaker"]: item["odds"] for item in result}
        self.assertEqual(1.22, by_book["Bet365"])
        self.assertEqual(1.96, by_book["Unibet"])

    def test_never_mixes_bookmakers_inside_1x2(self):
        payload = {
            "bookmakersRequested": ["Bet365", "Unibet"],
            "matches": [{
                "markets": [
                    {"market": "1X2", "selection": "Home", "odds": 2.0, "bookmaker": "Bet365", "team": "Home"},
                    {"market": "1X2", "selection": "Draw", "odds": 3.3, "bookmaker": "Bet365"},
                    {"market": "1X2", "selection": "Away", "odds": 4.0, "bookmaker": "Bet365", "team": "Away"},
                    {"market": "1X2", "selection": "Home", "odds": 2.1, "bookmaker": "Unibet", "team": "Home"},
                    {"market": "1X2", "selection": "Draw", "odds": 3.4, "bookmaker": "Unibet"},
                ],
            }],
        }
        sanitized, _ = sanitize_payload(payload)
        markets = sanitized["matches"][0]["markets"]
        self.assertEqual(3, len(markets))
        self.assertEqual({"Bet365"}, {item["bookmaker"] for item in markets})

    def test_drops_line_family_when_all_bookmakers_are_invalid(self):
        payload = {
            "matches": [{
                "markets": [
                    {"market": "MATCH_GOALS", "selection": "Over", "odds": 2.0, "bookmaker": "Bet365", "line": 1.5},
                    {"market": "MATCH_GOALS", "selection": "Over", "odds": 1.5, "bookmaker": "Bet365", "line": 2.5},
                    {"market": "MATCH_GOALS", "selection": "Under", "odds": 1.5, "bookmaker": "Unibet", "line": 1.5},
                    {"market": "MATCH_GOALS", "selection": "Under", "odds": 2.0, "bookmaker": "Unibet", "line": 2.5},
                ],
            }],
        }
        sanitized, report = sanitize_payload(payload)
        self.assertEqual([], sanitized["matches"][0]["markets"])
        self.assertEqual(1, report["groupsDropped"])

    def test_duplicate_same_bookmaker_uses_conservative_lower_price(self):
        payload = {
            "bookmakersRequested": ["Bet365"],
            "matches": [{
                "markets": [
                    {"market": "BTTS", "selection": "Yes", "odds": 1.95, "bookmaker": "Bet365"},
                    {"market": "BTTS", "selection": "Yes", "odds": 1.80, "bookmaker": "Bet365"},
                    {"market": "BTTS", "selection": "No", "odds": 1.90, "bookmaker": "Bet365"},
                ],
            }],
        }
        sanitized, _ = sanitize_payload(payload)
        yes = next(item for item in sanitized["matches"][0]["markets"] if item["selection"] == "Yes")
        self.assertEqual(1.80, yes["odds"])


if __name__ == "__main__":
    unittest.main()
