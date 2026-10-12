"""Offline contract tests for verified, competition-specific UEFA V2 history."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.betting_v2.uefa_verified_history import verified_uefa_history, competition_code


class UefaVerifiedV2HistoryTest(unittest.TestCase):
    def test_verified_result_dedup_and_stats_not_invented(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data/statmaker"
            base.mkdir(parents=True)
            current = {
                "schemaVersion": 1,
                "competitions": [{
                    "competitionId": "champions_league",
                    "fixtures": [
                        {"fixtureId": 12345, "status": "FT",
                         "kickoff": "2026-09-08T19:00:00+00:00",
                         "homeTeam": "A", "awayTeam": "B",
                         "homeGoals": 2, "awayGoals": 1,
                         "statsFetched": True,
                         "normalizedStats": {"HC": 5, "AC": 3, "HxG": None}},
                        {"fixtureId": 999, "status": "NS",
                         "kickoff": "2026-09-10T19:00:00+00:00",
                         "homeTeam": "A", "awayTeam": "C"}
                    ]
                }]
            }
            support = {
                "schemaVersion": 1,
                "participants": [{"matches": [
                    {"competition": "UEFA Champions League", "date": "2026-09-08",
                     "homeTeam": "A", "awayTeam": "B", "homeGoals": 2,
                     "awayGoals": 1, "sourceLabel": "verified API-Football"},
                    {"competition": "UEFA Conference League", "date": "2026-08-20",
                     "homeTeam": "C", "awayTeam": "D", "homeGoals": 0,
                     "awayGoals": 0, "sourceLabel": "verified API-Football"},
                    {"competition": "Friendlies Clubs", "date": "2026-08-21",
                     "homeTeam": "A", "awayTeam": "Z", "homeGoals": 3,
                     "awayGoals": 0, "sourceLabel": "verified API-Football"}
                ]}]
            }
            (base / "uefa_current_stats.json").write_text(json.dumps(current))
            (base / "uefa_team_support_history.json").write_text(json.dumps(support))
            rows, reasons, groups = verified_uefa_history(Path(directory))
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["group"] for r in rows},
                             {"CL|UEFA Champions League", "UECL|UEFA Conference League"})
            cl = next(r for r in rows if r["group"].startswith("CL|"))
            self.assertEqual(cl["fixture_id"], "12345")
            self.assertEqual(cl["stats"], {"HC": 5, "AC": 3})
            self.assertEqual(reasons.get("DUPLICATE_VERIFIED_UEFA_MATCH"), 1)
            self.assertEqual(set(groups), {r["group"] for r in rows})

    def test_unverified_scores_and_competition_identity(self):
        self.assertEqual(competition_code("UEFA Europa Conference League"), "UECL")
        self.assertEqual(competition_code("UEFA Europa League"), "EL")
        self.assertEqual(competition_code("UEFA Champions League"), "CL")
        self.assertIsNone(competition_code("Friendlies Clubs"))


if __name__ == "__main__":
    unittest.main()
