import json
import sys
import unittest
from unittest import mock
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import materialize_prepared_league_simulations as simulation
import materialize_prepared_team_elo as elo


class LeagueSimulationPhasesTest(unittest.TestCase):
    def test_cached_current_score_only_leagues_keep_elo_and_simulation_coverage(self):
        for code in ("SP1", "SP2", "E1", "E2", "E3", "SC0", "SC1", "SC2", "SC3", "AUT", "N1", "P1"):
            with self.subTest(league=code):
                row = {"league_code": code, "stats_role": "historical_support", "lifecycle": "active",
                       "app_season": "2026-2027", "target_app_season": "2026-2027"}
                self.assertTrue(elo.is_simulation_current_entry(row))
                self.assertTrue(simulation.is_simulation_current_entry(row))
                old = dict(row, app_season="2025-2026")
                self.assertFalse(elo.is_simulation_current_entry(old))
                self.assertFalse(simulation.is_simulation_current_entry(old))
                inactive = dict(row, lifecycle="inactive")
                self.assertFalse(elo.is_simulation_current_entry(inactive))
                self.assertFalse(simulation.is_simulation_current_entry(inactive))

    def test_new_unverified_format_does_not_block_supported_leagues(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "fixtures.json").write_text("{}")
            base = {"stats_role":"current_target", "lifecycle":"active", "completed_fixtures":11,
                    "cache_path":"fixtures.json", "app_season":"2026-2027", "country":"Test"}
            with mock.patch.object(simulation, "ROOT", root):
                rows = simulation.current_entries({"leagues":[dict(base, league_code="E0"), dict(base, league_code="AUS")]})
            self.assertEqual(["E0"], [r["league_code"] for r in rows])

    def entry(self, code="FIN"):
        return {"league_code": code, "app_season": "2026",
                "target_season_start": "2026-04-01", "target_season_end": "2026-11-30"}

    def fixture(self, home, away, round_name, fixture_id=1):
        return {"fixture_id": fixture_id, "date": "2026-10-21T15:30:00+00:00",
                "status": "FT", "home_team": home, "away_team": away,
                "home_goals": 1, "away_goals": 1,
                "source_league": {"id": 244, "round": round_name}}

    def test_cross_division_relegation_tie_is_not_a_league_table_result(self):
        tie = self.fixture("PK-35", "FF Jaro", "Relegation", 1646596)
        self.assertEqual([], simulation.current_season_fixtures(self.entry(), {"fixtures": [tie]}))

    def test_return_leg_labelled_final_does_not_add_a_thirteenth_team(self):
        first = self.fixture("PK-35", "FF Jaro", "Relegation", 1646596)
        second = self.fixture("FF Jaro", "PK-35", "Final", 1646597)
        self.assertEqual([], simulation.current_season_fixtures(self.entry(), {"fixtures": [first, second]}))

    def test_other_non_table_playoff_labels_cannot_change_finnish_roster(self):
        for phase in ("Play-offs", "Semi-finals", "Final", "Relegation"):
            with self.subTest(phase=phase):
                tie = self.fixture("Outside Division", "FF Jaro", phase)
                self.assertEqual([], simulation.current_season_fixtures(self.entry(), {"fixtures": [tie]}))

    def test_relegation_group_and_championship_group_results_still_count(self):
        fixtures = [self.fixture("FF Jaro", "Lahti", "Relegation Group - 27", 1),
                    self.fixture("KuPS", "VPS", "Championship Group - 28", 2),
                    self.fixture("Inter Turku", "SJK", "Regular Season - 22", 3)]
        self.assertEqual(fixtures, simulation.current_season_fixtures(self.entry(), {"fixtures": fixtures}))

    def test_finnish_second_division_playoff_does_not_add_roster_outsider(self):
        regular = self.fixture("KäPa", "Club B", "Group Stage", 1)
        playoff = self.fixture("Rops", "KäPa", "Final", 1646600)
        self.assertEqual([regular], simulation.current_season_fixtures(
            self.entry("FIN2"), {"fixtures": [regular, playoff]}))

    def test_other_competitions_keep_their_existing_phase_rules(self):
        tie = self.fixture("Club A", "Club B", "Relegation")
        self.assertEqual([tie], simulation.current_season_fixtures(self.entry("OTHER"), {"fixtures": [tie]}))

    def test_full_finnish_league_keeps_twelve_teams_and_normalized_probabilities(self):
        teams = [f"Club {i}" for i in range(12)]
        fixtures = [self.fixture(home, away, "Regular Season - 1", i * 12 + j)
                    for i, home in enumerate(teams) for j, away in enumerate(teams) if home != away]
        fixtures.append(self.fixture("PK-35", teams[0], "Relegation", 1646596))
        fixtures.append(self.fixture(teams[0], "PK-35", "Final", 1646597))
        rows = simulation.current_season_fixtures(self.entry(), {"fixtures": fixtures})
        scope = simulation.EloScope(
            model_version=simulation.ELO_MODEL_VERSION, rating_scale=400.0, home_advantage=65.0,
            teams={simulation.norm(name): simulation.EloTeam(1500.0, 22) for name in teams})
        result = simulation.simulate_league(self.entry(), rows, 20, scope)
        self.assertIsNotNone(result)
        meta, outcomes = result
        self.assertEqual(12, meta["team_count"])
        self.assertEqual(132, meta["current_completed_matches"])
        self.assertEqual(set(teams), {row["team_name"] for row in outcomes})
        self.assertAlmostEqual(1.0, sum(row["title_probability"] for row in outcomes))
        for row in outcomes:
            self.assertAlmostEqual(1.0, sum(json.loads(row["position_probabilities_json"])))


if __name__ == "__main__":
    unittest.main()
