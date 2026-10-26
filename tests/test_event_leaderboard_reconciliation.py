"""Missing top-20 players must be recovered only from complete verified evidence."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from reconcile_top_scorers_with_events import supplement_event_leaders


class EventLeaderboardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.games = [{"fixture_id": fid, "status": "FT", "date_utc": date,
                       "home_team": "Panathinaikos", "away_team": "Opponent",
                       "home_team_id": 617, "away_team_id": 999,
                       "home_goals": 1, "away_goals": 0}
                      for fid, date in [(500,"2026-08-31T16:30:00Z"),
                                        (501,"2026-10-18T14:30:00Z"),
                                        (502,"2026-10-25T16:00:00Z")]]
        self.entry = {"league_code":"G1", "lifecycle":"active", "stats_role":"current_target",
                      "api_football_league_id":197, "api_football_season":2026,
                      "output_path":"data/statmaker/domestic_enriched/g1.json"}
        self.fast = {"contract":"statmaker-league-tables-v1", "leagueCode":"G1", "season":2026,
                     "apiFootballLeagueId":197, "fixtures":self.games}
        self.write("data/statmaker/domestic_enriched/index.json", {"schema_version":3, "leagues":[self.entry]})
        self.write(self.entry["output_path"], {"competition":{"league_code":"G1"}, "matches":[]})
        self.save_games()
        self.doc = {"contract":"statmaker-schedule-scorers-v1", "leagueTopScorers":[{
            "leagueCode":"G1", "season":2026, "verified":True,
            "players":[{"player":"A. Tetteh", "team":"Panathinaikos", "goals":4, "assists":0}]}]}
        self.source = {"sourceRequest":"/players/topscorers?league=197&season=2026",
                       "leagueCode":"G1", "season":2026, "apiFootballLeagueId":197,
                       "response":[{"player":{"id":342918,"name":"A. Tetteh"}, "statistics":[{
                           "league":{"id":197,"season":2026}, "team":{"name":"Panathinaikos"}}]}]}
        self.save_source()
        for fid in (500,501,502):
            self.events(fid, name="C. Dessers" if fid==500 else "Cyriel Dessers")

    def write(self, name, value):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def save_games(self):
        self.write("data/statmaker/priority_refresh/G1.json", self.fast)

    def save_source(self):
        self.write("data/api_football/schedule_scorers/leaders/G1_2026.json", self.source)

    def events(self, fid, name="Cyriel Dessers", pid=37186, own=False, code="G1", team=617):
        self.write(f"data/api_football/schedule_scorers/events/fixture_{fid}.json", {
            "fixtureId":fid, "leagueCode":code, "sourceRequest":f"/fixtures/events?fixture={fid}",
            "retrievedAtUTC":"2026-10-26T08:00:00Z", "response":[{
                "type":"Goal", "detail":"Own Goal" if own else "Normal Goal",
                "player":{"id":pid,"name":name}, "team":{"id":team,"name":"Provider alias"},
                "time":{"elapsed":46,"extra":None}}]})

    def players(self):
        return self.doc["leagueTopScorers"][0]["players"]

    def test_omitted_player_merges_short_and_full_name_by_id_across_entire_season(self):
        changes = supplement_event_leaders(self.root, self.doc)
        row = next(r for r in self.players() if r.get("playerId")==37186)
        self.assertEqual(row["player"], "Cyriel Dessers")
        self.assertEqual(row["goals"], 3)
        self.assertIsNone(row["assists"])
        self.assertEqual(row["verifiedEventTotal"]["completedFixtures"], 3)
        self.assertEqual(len(changes), 1)

    def test_own_goals_are_excluded_from_player_totals(self):
        self.events(500, own=True)
        supplement_event_leaders(self.root, self.doc)
        self.assertEqual(self.players()[1]["goals"], 2)

    def test_incomplete_team_season_does_not_invent_a_total(self):
        (self.root/"data/api_football/schedule_scorers/events/fixture_502.json").unlink()
        self.assertEqual(supplement_event_leaders(self.root, self.doc), [])
        self.assertEqual(len(self.players()), 1)

    def test_same_initial_and_surname_with_different_ids_are_not_combined(self):
        self.events(502, name="Chris Dessers", pid=37187)
        supplement_event_leaders(self.root, self.doc)
        counts = {r.get("playerId"):r["goals"] for r in self.players() if r.get("eventDerived")}
        self.assertEqual(counts, {37186:2,37187:1})

    def test_provider_player_is_not_duplicated_or_reduced(self):
        row = {"player":"C. Dessers", "team":"Panathinaikos", "goals":5, "assists":2}
        self.doc["leagueTopScorers"][0]["players"] = [row]
        self.source["response"][0]["player"] = {"id":37186,"name":"C. Dessers"}
        self.save_source()
        supplement_event_leaders(self.root, self.doc)
        self.assertEqual(self.players(), [row])
        self.assertEqual(row["goals"], 5)

    def test_existing_lower_provider_total_is_raised_to_verified_event_floor(self):
        self.doc["leagueTopScorers"][0]["players"] = [{"player":"C. Dessers", "team":"Panathinaikos", "goals":1}]
        self.source["response"][0]["player"] = {"id":37186,"name":"C. Dessers"}
        self.save_source()
        supplement_event_leaders(self.root, self.doc)
        self.assertEqual(len(self.players()), 1)
        self.assertEqual(self.players()[0]["goals"], 3)

    def test_wrong_season_results_do_not_contribute(self):
        self.fast["season"] = 2025
        self.save_games()
        self.assertEqual(supplement_event_leaders(self.root, self.doc), [])

    def test_wrong_event_league_is_rejected(self):
        self.events(502, code="E0")
        self.assertEqual(supplement_event_leaders(self.root, self.doc), [])

    def test_wrong_scoring_side_is_rejected_even_when_total_goal_count_matches(self):
        self.games[2]["home_goals"], self.games[2]["away_goals"] = 0,1
        self.save_games()
        self.assertEqual(supplement_event_leaders(self.root, self.doc), [])

    def test_missing_player_id_does_not_join_names_by_guessing(self):
        self.events(502, pid=None)
        self.assertEqual(supplement_event_leaders(self.root, self.doc), [])

    def test_score_correction_rebuilds_derived_total_and_repeat_is_idempotent(self):
        supplement_event_leaders(self.root, self.doc)
        self.games[2]["home_goals"] = 0
        self.save_games()
        supplement_event_leaders(self.root, self.doc)
        self.assertEqual(self.players()[1]["goals"], 2)
        before = deepcopy(self.doc)
        self.assertEqual(supplement_event_leaders(self.root, self.doc), [])
        self.assertEqual(self.doc, before)


if __name__ == "__main__":
    unittest.main()
