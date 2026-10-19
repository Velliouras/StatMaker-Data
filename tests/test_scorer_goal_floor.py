"""Verified goal floors must not guess scores or double-count match events."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from reconcile_top_scorers_with_events import reconcile, person_matches


class GreekScorerGoalFloorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / "data/statmaker/schedule_scorers"
        self.folder.mkdir(parents=True)
        self.canonical = self.root / "data/statmaker/domestic_enriched"
        self.canonical.mkdir(parents=True)
        self.base = {
            "contract": "statmaker-verified-g1-scorer-baseline-v1",
            "leagueCode": "G1", "season": 2026,
            "providerVerified": True,
            "sourceRequest": "/players/topscorers?league=197&season=2026",
            "retrievedAtUTC": "2026-10-11T14:57:23+00:00",
            "players": [
                {"player": "A. Tetteh", "team": "Panathinaikos", "goals": 3},
                {"player": "K. Kodro", "team": "OFI", "goals": 2},
            ],
        }
        self.new = {
            "contract": "statmaker-schedule-scorers-v1",
            "leagueTopScorers": [{"leagueCode": "G1", "season": 2026,
                "verified": True,
                "players": [
                    {"player": "A. Tetteh", "team": "Panathinaikos", "goals": 3},
                    {"player": "K. Kodro", "team": "OFI", "goals": 2},
                    {"player": "Unmatched", "team": "Panathinaikos", "goals": 2},
                ]}],
        }
        self.games = {"competition": {"league_code": "G1"}, "matches": [
            {"fixture_id": 123, "status": "FT",
             "date_utc": "2026-10-18T14:30:00+00:00",
             "home_team": "Panathinaikos", "away_team": "OFI",
             "home_goals": 1, "away_goals": 1},
        ]}
        self.context = {
            "contract": "statmaker-domestic-match-context-v1",
            "goalEvents": [
                {"fixtureId": "123", "team": "Panathinaikos",
                 "player": "Andrews Tetteh", "ownGoal": False},
                {"fixtureId": "123", "team": "OFI",
                 "player": "Kenan Kodro", "ownGoal": False},
            ]
        }
        self.save()

    def save(self):
        for name, payload in [
            ("verified_g1_baseline.json", self.base),
            ("season_snapshot.json", self.new),
            ("domestic_match_context.json", self.context),
        ]:
            (self.folder / name).write_text(json.dumps(payload), encoding="utf-8")
        (self.canonical / "greece_super_league_2026.json").write_text(
            json.dumps(self.games), encoding="utf-8")

    def read_snapshot(self):
        return json.loads((self.folder / "season_snapshot.json").read_text())

    def test_abbreviations_match_only_same_surname_and_first_initial(self):
        self.assertTrue(person_matches("A. Tetteh", "Andrews Tetteh"))
        self.assertTrue(person_matches("K. Kodro", "Kenan Kodro"))
        self.assertFalse(person_matches("A. Tetteh", "Adrian Other"))
        self.assertFalse(person_matches("A. Tetteh", "Benedict Tetteh"))

    def test_verified_events_correct_stale_provider_totals(self):
        changed = reconcile(self.root)
        self.assertEqual({p["player"]: p["verifiedMinimum"] for p in changed},
                         {"A. Tetteh": 4, "K. Kodro": 3})
        rows = self.read_snapshot()["leagueTopScorers"][0]["players"]
        self.assertEqual({p["player"]: p["goals"] for p in rows},
                         {"A. Tetteh": 4, "K. Kodro": 3, "Unmatched": 2})

    def test_second_run_never_counts_same_goal_twice(self):
        reconcile(self.root)
        self.assertEqual(reconcile(self.root), [])
        rows = self.read_snapshot()["leagueTopScorers"][0]["players"]
        self.assertEqual([r["goals"] for r in rows], [4, 3, 2])

    def test_incomplete_event_timeline_is_rejected(self):
        self.context["goalEvents"] = self.context["goalEvents"][:1]
        self.save()
        self.assertEqual(reconcile(self.root), [])
        self.assertNotIn("verifiedGoalFloorAudit", self.read_snapshot())

    def test_prebaseline_match_is_not_added(self):
        self.games["matches"][0]["date_utc"] = "2026-10-10T18:00:00+00:00"
        self.save()
        self.assertEqual(reconcile(self.root), [])

    def test_own_goals_not_credited_to_player(self):
        self.context["goalEvents"][0]["ownGoal"] = True
        self.save()
        changes = reconcile(self.root)
        self.assertEqual([x["player"] for x in changes], ["K. Kodro"])

    def test_no_player_in_provider_leaders_is_fabricated(self):
        self.new["leagueTopScorers"][0]["players"].pop(0)
        self.save()
        changes = reconcile(self.root)
        self.assertEqual([x["player"] for x in changes], ["K. Kodro"])


if __name__ == "__main__":
    unittest.main()
