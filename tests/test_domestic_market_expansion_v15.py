import importlib.util
import json
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE = SCRIPTS / "domestic_market_expansion_v15.py"

spec = importlib.util.spec_from_file_location("domestic_market_expansion_v15", MODULE)
expansion = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = expansion
spec.loader.exec_module(expansion)


class FakeOdds:
    SUPPORTED_MARKETS = set()
    EMITTED_MARKET_COUNT_KEYS = []
    _statmaker_market_v15_installed = False

    @staticmethod
    def normalize_text(value, drop_suffixes=False):
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    @staticmethod
    def raw_market_name(market):
        return market.get("name", "")

    @staticmethod
    def outcome_rows(market):
        return market.get("odds", [])

    @staticmethod
    def row_name(row):
        return row.get("label") or row.get("name") or row.get("selection") or ""

    @staticmethod
    def to_float(value):
        try:
            number = float(value)
            return number if 1.01 <= number <= 1000 else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def row_price(cls, row):
        for key in ("odds", "price", "decimal", "value"):
            price = cls.to_float(row.get(key))
            if price is not None:
                return price
        return None

    @staticmethod
    def row_side_price(row, side):
        return FakeOdds.to_float(row.get(side))

    @staticmethod
    def row_line(row):
        for key in ("line", "point", "handicap", "hdp"):
            try:
                if row.get(key) is not None:
                    return float(row[key])
            except (TypeError, ValueError):
                return None
        label = FakeOdds.row_name(row)
        match = re.search(r"(?<!\d)(-?\d+(?:\.\d+)?)(?!\d)", label)
        return float(match.group(1)) if match else None

    @staticmethod
    def line_from_text(value):
        match = re.search(r"(?<!\d)(-?\d+(?:\.\d+)?)(?!\d)", str(value or ""))
        return float(match.group(1)) if match else None

    @staticmethod
    def team_from_market_or_row(market_name, row, home, away):
        text = FakeOdds.normalize_text(f"{market_name} {FakeOdds.row_name(row)}")
        if "home" in text or FakeOdds.normalize_text(home) in text:
            return home
        if "away" in text or FakeOdds.normalize_text(away) in text:
            return away
        return None

    @classmethod
    def add_market(cls, out, market, selection, odds, bookmaker, line=None, team=None):
        if odds is None or market not in cls.SUPPORTED_MARKETS:
            return
        row = {
            "market": market,
            "selection": selection,
            "odds": odds,
            "bookmaker": bookmaker,
            "confidence": "high",
            "exactBookmakerOdds": True,
        }
        if line is not None:
            row["line"] = line
        if team:
            row["team"] = team
        out.append(row)

    @staticmethod
    def record_raw_market(debug, raw, family):
        debug.setdefault("raw", []).append((raw, family))

    @staticmethod
    def record_skipped_market(debug, raw, reason, row="", family_override=None):
        debug.setdefault("skipped", []).append((raw, reason, family_override))

    @staticmethod
    def normalize_market(*args, **kwargs):
        return []

    @staticmethod
    def emitted_market_counts(feed):
        counts = {}
        for league in feed.get("leagues", []):
            for match in league.get("matches", []):
                for row in match.get("markets", []):
                    key = row.get("market")
                    counts[key] = counts.get(key, 0) + 1
        return counts


class DomesticMarketExpansionV15Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        expansion.install(FakeOdds)

    def normalize(self, market):
        return FakeOdds.normalize_market(market, "Bet365", "Home FC", "Away FC", {})

    def test_draw_no_bet_provider_shape(self):
        rows = self.normalize({"name": "Draw No Bet", "odds": [{"home": "1.66", "away": "2.10"}]})
        self.assertEqual({"Home", "Away"}, {row["selection"] for row in rows})
        self.assertEqual({"Home FC", "Away FC"}, {row["team"] for row in rows})
        self.assertTrue(all(row["market"] == "DRAW_NO_BET" for row in rows))

    def test_half_time_result_provider_shapes(self):
        for name in ("Half Time Result", "ML HT"):
            rows = self.normalize({"name": name, "odds": [{"home": "2.20", "draw": "2.05", "away": "3.40"}]})
            self.assertEqual({"Home", "Draw", "Away"}, {row["selection"] for row in rows})
            self.assertTrue(all(row["market"] == "HALF_TIME_1X2" for row in rows))

    def test_second_half_totals(self):
        rows = self.normalize({"name": "Totals 2H", "odds": [{"hdp": 1.5, "over": "1.90", "under": "1.90"}]})
        self.assertEqual(2, len(rows))
        self.assertEqual({"SECOND_HALF_GOALS"}, {row["market"] for row in rows})
        self.assertEqual({1.5}, {row["line"] for row in rows})

    def test_european_handicap_preserves_provider_line(self):
        rows = self.normalize({
            "name": "European Handicap",
            "odds": [{"hdp": 2, "home": "1.11", "draw": "6.50", "away": "1.20"}],
        })
        self.assertEqual({"Home", "Draw", "Away"}, {row["selection"] for row in rows})
        self.assertTrue(all(row["line"] == 2 for row in rows))
        self.assertTrue(all(row["market"] == "EUROPEAN_HANDICAP" for row in rows))

    def test_correct_score_and_ht_ft_exact_labels(self):
        score = self.normalize({"name": "Correct Score", "odds": [{"label": "2-1", "odds": "9.50"}]})
        self.assertEqual("2-1", score[0]["selection"])
        self.assertEqual(9.5, score[0]["odds"])
        htft = self.normalize({"name": "Half Time / Full Time", "odds": [{"label": "1/1", "odds": "3.25"}]})
        self.assertEqual("1/1", htft[0]["selection"])
        self.assertEqual("HALF_TIME_FULL_TIME", htft[0]["market"])

    def test_corner_spread_and_most_shots_on_target(self):
        handicap = self.normalize({"name": "Corners Spread", "odds": [{"hdp": -0.5, "home": "1.85", "away": "1.95"}]})
        self.assertEqual({"Home", "Away"}, {row["selection"] for row in handicap})
        self.assertTrue(all(row["market"] == "CORNER_HANDICAP" for row in handicap))
        most = self.normalize({"name": "Most Shots on Target", "odds": [{"home": "1.80", "draw": "4.00", "away": "2.50"}]})
        self.assertEqual({"Home", "Draw", "Away"}, {row["selection"] for row in most})
        self.assertTrue(all(row["market"] == "MOST_SHOTS_ON_TARGET" for row in most))

    def test_exact_total_goals_becomes_parseable_goal_band(self):
        rows = self.normalize({
            "name": "Exact Total Goals",
            "odds": [
                {"label": "0", "odds": "10.0"},
                {"label": "2", "odds": "3.5"},
                {"label": "6+", "odds": "7.0"},
            ],
        })
        self.assertEqual({"0-0", "2-2", "6+"}, {row["selection"] for row in rows})
        self.assertTrue(all(row["market"] == "GOAL_BANDS" for row in rows))

    def test_unknown_market_never_creates_rows(self):
        rows = self.normalize({"name": "Player Assists", "odds": [{"label": "Player", "odds": "2.0"}]})
        self.assertEqual([], rows)

    def test_archive_rebuild_preserves_existing_and_adds_only_exact_rows(self):
        pipeline = types.ModuleType("domestic_live_july_pipeline")
        pipeline.now_utc = lambda: "2026-07-16T12:00:00Z"
        domestic = types.ModuleType("domestic_odds_expansion")
        domestic.install = lambda odds_module, pipeline_module: None
        sys.modules["domestic_live_july_pipeline"] = pipeline
        sys.modules["domestic_odds_expansion"] = domestic
        sys.modules["update_domestic_odds_api_io"] = FakeOdds
        sys.modules["domestic_market_expansion_v15"] = expansion

        rebuild_path = SCRIPTS / "rebuild_domestic_expanded_markets_from_archive.py"
        rebuild_spec = importlib.util.spec_from_file_location("rebuild_v15_test", rebuild_path)
        rebuild = importlib.util.module_from_spec(rebuild_spec)
        rebuild_spec.loader.exec_module(rebuild)

        feed = {
            "leagues": [{
                "leagueCode": "TST",
                "matches": [{
                    "id": "m1",
                    "date": "2026-07-20",
                    "homeTeam": "Home FC",
                    "awayTeam": "Away FC",
                    "usableForStats": True,
                    "markets": [{
                        "market": "1X2", "selection": "Home", "odds": 1.8,
                        "bookmaker": "Bet365", "exactBookmakerOdds": True,
                    }],
                }],
            }],
        }
        archive = {
            "leagues": [{
                "leagueCode": "TST",
                "matches": [{
                    "id": "m1",
                    "date": "2026-07-20",
                    "homeTeam": "Home FC",
                    "awayTeam": "Away FC",
                    "providerMarkets": [{
                        "bookmaker": "Bet365",
                        "exactProviderPayload": True,
                        "market": {"name": "Draw No Bet", "odds": [{"home": "1.60", "away": "2.20"}]},
                    }],
                }],
            }],
        }
        report = rebuild.rebuild_feed_markets(feed, archive, FakeOdds)
        markets = feed["leagues"][0]["matches"][0]["markets"]
        self.assertEqual(3, len(markets))
        self.assertEqual(2, report["expandedSelections"])
        self.assertFalse(report["syntheticOdds"])
        self.assertTrue(all(row.get("exactBookmakerOdds") is True for row in markets))


if __name__ == "__main__":
    unittest.main()
