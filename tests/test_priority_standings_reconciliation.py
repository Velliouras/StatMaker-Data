"""Delayed official tables must agree with verified finals without losing penalties."""
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/api_budget_runtime'))
from scripts.reconcile_priority_standings import reconcile, totals
from scripts.refresh_priority_matches import refresh, FOLDER

NOW = datetime(2026, 11, 1, 21, 5, tzinfo=timezone.utc)


def match(fid, goals=(2, 1), **changes):
    return dict(fixture_id=fid, date_utc=(NOW - timedelta(days=3-fid)).isoformat(),
                round='Regular Season - ' + str(fid), status='FT',
                home_team_id=10, away_team_id=11, home_team='Home', away_team='Away',
                home_goals=goals[0], away_goals=goals[1], **changes)


def document(first=(2, 1), second=(2, 0)):
    fixtures = [match(1, first), match(2, second)]
    rows = []
    for rank, team in enumerate((10, 11), 1):
        stats = totals(fixtures[:1], team)
        rows.append(dict(rank=rank, teamId=team, team='Home' if team == 10 else 'Away',
                         group='Super League', teamLogo='https://example.invalid/logo.png',
                         points=3*stats['wins']+stats['draws'], **stats))
    return dict(contract='statmaker-league-tables-v1', leagueCode='G1', season=2026,
                apiFootballLeagueId=197, fixtures=fixtures, standings=rows,
                singleStandingsGroup=True, standingsObservedAtUTC=(NOW-timedelta(hours=1)).isoformat())


def champions_league():
    teams = list(range(1, 37))
    fixtures = []
    for md in range(1, 9):
        for home, away in zip(teams[:18], reversed(teams[18:])):
            fixtures.append(dict(fixture_id=len(fixtures)+1, home_team_id=home, away_team_id=away,
                                 date_utc=(NOW + timedelta(days=md-7)).isoformat(),
                                 round=f'League Stage - {md}', status='NS'))
        teams = [teams[0], teams[-1], *teams[1:-1]]
    standings = [dict(rank=t, teamId=t, team=f'Team {t:02}', points=0, **totals([], t))
                 for t in range(1, 37)]
    return dict(leagueCode='CL', country='UEFA', apiFootballLeagueId=2, fixtures=fixtures,
                standings=standings, singleStandingsGroup=True)


class StandingsReconciliationTest(unittest.TestCase):
    def test_champions_league_shared_ranks_and_away_goal_tiebreak(self):
        doc = champions_league()
        doc['fixtures'][0].update(status='FT', home_goals=2, away_goals=0)
        doc['fixtures'][1].update(status='FT', home_goals=2, away_goals=0)
        doc['fixtures'][2].update(status='FT', home_goals=0, away_goals=2)
        doc['fixtures'][3].update(status='2H', home_goals=5, away_goals=0)
        doc['fixtures'].append(dict(doc['fixtures'][0], fixture_id=999, round='Qualifying Round'))
        official = deepcopy(doc['standings'])
        self.assertTrue(reconcile(doc, NOW))
        self.assertEqual([(1, 34), (2, 1), (2, 2), (4, 4)],
                         [(r['rank'], r['teamId']) for r in doc['standings'][:4]])
        self.assertEqual([1, 2, 3], doc['standingsReconciliation']['appliedFixtureIds'])
        self.assertEqual(official, doc['officialStandings'])
        self.assertFalse(reconcile(doc, NOW+timedelta(minutes=5)))

    def test_champions_league_defers_partial_official_inclusion_and_final_matchday(self):
        for change in ('partial', 'matchday-8', 'incomplete-schedule'):
            doc = champions_league()
            doc['fixtures'][0].update(status='FT', home_goals=2, away_goals=0)
            if change == 'partial':
                doc['standings'][0].update(played=1, wins=1, goalsFor=2, points=3)
            elif change == 'matchday-8':
                doc['fixtures'][-1].update(status='FT', home_goals=1, away_goals=0)
            else:
                doc['fixtures'].pop()
            official = deepcopy(doc['standings'])
            self.assertFalse(reconcile(doc, NOW), change)
            self.assertEqual(official, doc['standings'], change)

    def test_adds_only_missing_final_and_keeps_verified_source_timestamp(self):
        doc = document()
        before = deepcopy(doc['standings'])
        observed = doc['standingsObservedAtUTC']
        self.assertTrue(reconcile(doc, NOW))
        self.assertEqual(before, doc['officialStandings'])
        self.assertEqual([(2, 6), (2, 0)], [(r['played'], r['points']) for r in doc['standings']])
        self.assertEqual([2], doc['standingsReconciliation']['appliedFixtureIds'])
        self.assertEqual(observed, doc['standingsObservedAtUTC'])
        self.assertEqual('official_plus_verified_finals', doc['standingsSource'])

    def test_repeated_runs_and_repeated_stale_provider_response_never_add_twice(self):
        doc = document()
        reconcile(doc, NOW)
        once = deepcopy(doc['standings'])
        for minute in (5, 15, 30, 60):
            if minute == 30:
                doc['standings'] = deepcopy(doc['officialStandings'])
            reconcile(doc, NOW+timedelta(minutes=minute))
            self.assertEqual(once, doc['standings'])

    def test_official_catchup_replaces_projection_without_another_three_points(self):
        doc = document()
        reconcile(doc, NOW)
        caught_up = deepcopy(doc['standings'])
        doc['officialStandings'] = deepcopy(caught_up)
        doc['standingsObservedAtUTC'] = NOW.isoformat()
        reconcile(doc, NOW+timedelta(minutes=30))
        self.assertEqual(caught_up, doc['standings'])
        self.assertEqual('official', doc['standingsSource'])

    def test_point_deductions_and_bonuses_survive_projection(self):
        for adjustment in (-3, -1, 2):
            doc = document()
            doc['standings'][0]['points'] += adjustment
            reconcile(doc, NOW)
            self.assertEqual(6+adjustment, doc['standings'][0]['points'])
            self.assertEqual(adjustment, doc['standings'][0]['points'] - 3*doc['standings'][0]['wins'])

    def test_partial_provider_update_can_include_a_final_for_only_one_team(self):
        doc = document()
        updated = totals(doc['fixtures'], 11)
        doc['standings'][1].update(updated)
        reconcile(doc, NOW)
        self.assertEqual([(2, 6), (2, 0)], [(r['played'], r['points']) for r in doc['standings']])

    def test_live_and_scheduled_scores_are_never_added(self):
        for status in ('1H', 'HT', '2H', 'NS', 'PST', 'CANC', 'ABD'):
            doc = document()
            doc['fixtures'][1]['status'] = status
            before = deepcopy(doc['standings'])
            reconcile(doc, NOW)
            self.assertEqual(before, doc['standings'])

    def test_missing_or_inconsistent_history_preserves_the_official_table(self):
        for change in ('missing', 'wrong-goals', 'future-final', 'duplicate', 'invalid-score', 'wrong-team'):
            doc = document()
            before = deepcopy(doc['standings'])
            if change == 'missing': doc['fixtures'].pop(0)
            elif change == 'wrong-goals': doc['fixtures'][0]['home_goals'] = 4
            elif change == 'future-final': doc['fixtures'][1]['date_utc'] = (NOW+timedelta(days=1)).isoformat()
            elif change == 'duplicate': doc['fixtures'].append(deepcopy(doc['fixtures'][1]))
            elif change == 'invalid-score': doc['fixtures'][1]['home_goals'] = None
            elif change == 'wrong-team': doc['fixtures'][1]['away_team_id'] = 99
            reconcile(doc, NOW)
            self.assertEqual(before, doc['standings'], change)
            self.assertEqual('awaiting_official', doc['standingsReconciliation']['status'], change)

    def test_invalid_official_rows_are_not_projected(self):
        for field, value in (('played', 2), ('rank', 0), ('wins', -1), ('teamId', 0), ('points', '3')):
            doc = document()
            doc['standings'][0][field] = value
            before = deepcopy(doc['standings'])
            reconcile(doc, NOW)
            self.assertEqual(before, doc['standings'])
            self.assertEqual('invalid_official_table', doc['standingsReconciliation']['reason'])

    def test_new_or_changed_tie_waits_for_official_sporting_order(self):
        for first, second in (((2, 1), (0, 1)), ((0, 0), (1, 1))):
            doc = document(first, second)
            before = deepcopy(doc['standings'])
            reconcile(doc, NOW)
            self.assertEqual(before, doc['standings'])
            self.assertEqual('new_tie_requires_official_sporting_order',
                             doc['standingsReconciliation']['reason'])

    def test_existing_unchanged_tie_retains_official_order(self):
        doc = document()
        for rank, team in ((3, 12), (4, 13)):
            doc['standings'].append(dict(rank=rank, teamId=team, team=str(team), points=-1,
                                         **dict.fromkeys(('played','wins','draws','losses','goalsFor','goalsAgainst'),0)))
        reconcile(doc, NOW)
        self.assertEqual([10, 11, 12, 13], [r['teamId'] for r in doc['standings']])

    def test_multiple_groups_uefa_and_split_seasons_remain_official(self):
        for change in ('group', 'UEFA', 'split', 'knockout'):
            doc = document()
            before = deepcopy(doc['standings'])
            if change == 'group': doc['singleStandingsGroup'] = False
            elif change == 'UEFA': doc['leagueCode'] = 'CL'
            elif change == 'split': doc['fixtures'][1]['round'] = 'Championship Round - 1'
            elif change == 'knockout': doc['fixtures'][1]['status'] = 'PEN'
            reconcile(doc, NOW)
            self.assertEqual(before, doc['standings'])
            self.assertEqual('awaiting_official', doc['standingsReconciliation']['status'])

    def test_can_reorder_teams_without_recomputing_official_tiebreaks(self):
        doc = document()
        # Home starts with a five-point deduction and overtakes Away after its win.
        doc['standings'][0].update(points=-2, rank=2)
        doc['standings'][1]['rank'] = 1
        reconcile(doc, NOW)
        self.assertEqual([(1,10,1),(2,11,0)], [(r['rank'],r['teamId'],r['points']) for r in doc['standings']])

    def test_repair_publishes_even_if_every_provider_bucket_is_already_fresh(self):
        class NoCalls:
            calls = 0
            def get(self, *args):
                raise AssertionError('Reconciliation must not spend an API request')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'config').mkdir()
            policy = dict(tiers={t:dict(resultsMinutes=5,standingsMinutes=30)
                                 for t in ('primary','europe','background')},
                          leagueIds={'197':dict(code='G1',season=2026,country='Greece',
                              competition='Super League',displaySeason='2026-2027',tier='primary')})
            (root/'config/api_football_refresh_policy.json').write_text(json.dumps(policy))
            path = root/FOLDER/'G1.json'
            path.parent.mkdir(parents=True)
            doc = document()
            for key in ('seasonReconciledAtUTC','resultsCheckedAtUTC','standingsCheckedAtUTC'):
                doc[key] = NOW.isoformat()
            path.write_text(json.dumps(doc))
            (path.parent/'index.json').write_text(json.dumps({'apiCalls':0}))
            with contextlib.redirect_stdout(io.StringIO()):
                refresh(root, NoCalls(), NOW)
                published = json.loads(path.read_text())
                refresh(root, NoCalls(), NOW)
            self.assertEqual(6, published['standings'][0]['points'])
            self.assertEqual(published, json.loads(path.read_text()))
            self.assertEqual(0, json.loads((path.parent/'index.json').read_text())['apiCalls'])


if __name__ == '__main__':
    unittest.main()
