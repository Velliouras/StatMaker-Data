"""Zero-provider-call frozen future forecasts cannot learn any prospective result."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from freeze_prematch_shadow import (
    freeze, verified_targets, write_snapshot,
)

ASOF = datetime(2026, 10, 10, 16, tzinfo=timezone.utc)
KICK = datetime(2026, 10, 11, 20, tzinfo=timezone.utc)


def historical(*, xg=True, extra_future=False):
    rows = []
    start = datetime(2026, 9, 1, 15, tzinfo=timezone.utc)
    for i in range(26):
        rows.append({
            "group": "BRA|Serie A", "fixture_id": f"old-{i}",
            "date": start + timedelta(days=i),
            "home": "home fc" if i%2 == 0 else "away fc",
            "away": "away fc" if i%2 == 0 else "home fc",
            "hg": 1+(i%3), "ag": i%2,
            "stats": {"HxG": 1.45, "AxG": 1.05} if xg else {},
        })
    if extra_future:
        # Even if a future FT is present in cache, it MUST NOT affect
        # generation of a prediction as-of the earlier current date.
        rows.append({
            "group": "BRA|Serie A", "fixture_id": "future-known-score",
            "date": datetime(2026, 10, 10, 23, tzinfo=timezone.utc),
            "home": "home fc", "away": "away fc",
            "hg": 12, "ag": 0,
            "stats": {"HxG": 11., "AxG": 0.},
        })
    return rows


def schedule(kick=KICK):
    return {"leagues": [{
        "leagueCode": "BRA", "matches": [{
            "id": "odds-event-123",
            "kickoff": kick.isoformat(),
            "date": kick.date().isoformat(),
            "scheduleVerified": True,
            "teamMappingStatus": "matched",
            "canonicalHomeTeam": "Home FC",
            "canonicalAwayTeam": "Away FC",
            "providerHomeTeam": "Provider Home FC",
            "providerAwayTeam": "Provider Away FC",
            "scheduleSource": "odds-api-io-events",
        }],
    }]}


class FreezeShadowTests(unittest.TestCase):
    def test_genuine_xg_uses_same_walk_forward_feature_contract(self):
        result = freeze(Path("."), ASOF, previous_matches=historical(xg=True),
                        schedule=schedule())
        self.assertEqual(result["forecastCount"], 1)
        row = result["forecastData"][0]
        self.assertEqual(row["strategy"], "XG_PRIMARY")
        self.assertEqual(row["providerEventId"], "odds-event-123")
        self.assertIsNone(row["independentApiFootballFixtureId"])
        self.assertFalse(row["independentCrossProviderIdentityVerified"])
        self.assertNotIn("homeGoals", row)
        self.assertNotIn("awayGoals", row)
        self.assertNotIn("observed", row)
        self.assertEqual(result["certificationStatus"], "BLOCKED")
        self.assertFalse(row["certifiedStrong"])
        self.assertAlmostEqual(sum(row["probabilities"][k]
            for k in ("1X2_HOME","1X2_DRAW","1X2_AWAY")), 1, places=5)

    def test_elo_fallback_never_fills_in_fabricated_xg(self):
        result = freeze(Path("."), ASOF, previous_matches=historical(xg=False),
                        schedule=schedule())
        self.assertEqual(result["forecastCount"], 1)
        self.assertEqual(result["forecastData"][0]["strategy"],
                         "ELO_GOALS_FALLBACK_NO_XG")
        self.assertNotIn("observed", result["forecastData"][0])

    def test_strict_prior_date_features_ignore_future_score_leak(self):
        baseline = freeze(Path("."), ASOF, previous_matches=historical(xg=True),
                          schedule=schedule())
        leaked = freeze(Path("."), ASOF, previous_matches=historical(xg=True, extra_future=True),
                        schedule=schedule())
        self.assertEqual(baseline["forecastData"], leaked["forecastData"])

    def test_unverified_team_mapping_blocked_not_guessed(self):
        data = schedule()
        data["leagues"][0]["matches"][0]["teamMappingStatus"] = "partial"
        result = freeze(Path("."), ASOF, previous_matches=historical(),
                        schedule=data)
        self.assertEqual(result["forecastCount"], 0)
        self.assertEqual(result["rejected"]["UNVERIFIED_TEAM_MAPPING"], 1)

    def test_already_started_event_blocked(self):
        result = freeze(Path("."), ASOF, previous_matches=historical(),
                        schedule=schedule(ASOF - timedelta(seconds=1)))
        self.assertEqual(result["forecastCount"], 0)
        self.assertEqual(result["rejected"]["NOT_FUTURE_IN_WINDOW"], 1)

    def test_same_normalized_team_with_two_distinct_hist_names_blocked(self):
        rows = historical()
        rows.append({**rows[0], "fixture_id": "collision",
                     "home": "home--fc", "date": rows[0]["date"]+timedelta(hours=1)})
        result = freeze(Path("."), ASOF, previous_matches=rows,
                        schedule=schedule())
        self.assertEqual(result["forecastCount"], 0)
        self.assertEqual(result["rejected"]["AMBIGUOUS_OR_UNKNOWN_HISTORICAL_TEAM"], 1)

    def test_append_only_snapshot_is_never_replaced(self):
        result = freeze(Path("."), ASOF, previous_matches=historical(),
                        schedule=schedule())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_snapshot(root, result, now=ASOF)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["forecastCount"], 1)
            self.assertEqual(data["realStrongSelections"], 0)
            with self.assertRaises(FileExistsError):
                write_snapshot(root, result, now=ASOF)


if __name__ == "__main__":
    unittest.main()
