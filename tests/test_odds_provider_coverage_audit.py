import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import audit_odds_provider_coverage as audit


class CoverageAuditTest(unittest.TestCase):
    def test_player_and_half_time_shots_do_not_count_as_full_match(self):
        for name in ('Player Shots', 'Total Shots First Half', 'Shots Home Total HT'):
            self.assertIsNone(audit.api_family(name))
        self.assertEqual(audit.api_family('Total ShotOnGoal'), 'MATCH_SHOTS_ON_TARGET')
        self.assertEqual(audit.api_family('Shots. Home Total'), 'TEAM_SHOTS')

    def test_empty_invalid_and_unpriced_markets_do_not_prove_coverage(self):
        book = dict(name='Bet365', bets=[dict(name='Total Shots', values=[dict(odd=x)]) for x in ('NaN', 'Infinity', '1', None)])
        self.assertEqual(audit.summarize_book(book)['families'], [])
        book['bets'].append(dict(name='Total ShotOnGoal', values=[dict(odd='1.95')]))
        self.assertEqual(audit.summarize_book(book)['families'], ['MATCH_SHOTS_ON_TARGET'])

    def test_scope_identity_season_and_prematch_window_are_required(self):
        now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        good = dict(fixture=dict(id=1, date='2026-10-27T12:00:00Z', status=dict(short='NS')), league=dict(id=39, season=2026))
        for change in ({'league':dict(id=40,season=2026)}, {'league':dict(id=39,season=2025)},
                       {'fixture':dict(id=1,date='2026-10-26T10:00:00Z',status=dict(short='NS'))},
                       {'fixture':dict(id=1,date='2026-10-27T12:00:00Z',status=dict(short='FT'))}):
            self.assertEqual(audit.fixtures_in_window(dict(response=[{**good,**change}]),39,2026,now), [])
        self.assertEqual(len(audit.fixtures_in_window(dict(response=[good]),39,2026,now)),1)

    def test_all_bookmakers_count_once_per_fixture(self):
        report = dict(apiFootballScopes=[dict(fixtures=[dict(bookmakers=[
            dict(bookmaker='Bet365', families=['MATCH_GOALS'],pricedMarketNames=['Goals Over/Under']),
            dict(bookmaker='Other', families=['MATCH_GOALS','MATCH_SHOTS'],pricedMarketNames=['Total Shots'])])])], unibetFixtures=[])
        result = audit.summary(report)
        self.assertEqual(result['apiFootballAnyBookmaker']['MATCH_SHOTS'],1)
        self.assertEqual(result['apiFootballAnyBookmaker']['MATCH_GOALS'],1)
        self.assertEqual(result['apiFootballBet365']['MATCH_SHOTS'],0)

    def test_request_cap_stops_before_network_and_never_retries(self):
        report = dict(requests=dict(apiFootball=audit.APIF_LIMIT,unibet=0),errors=[])
        with patch.object(audit.time,'sleep'),patch.object(audit,'urlopen') as opener:
            client = audit.Client('private-key','private-key',report,opener=opener)
            with self.assertRaises(audit.StopAudit):client.get('apiFootball','odds')
            opener.assert_not_called()

    def test_invalid_or_past_cached_event_is_not_requested(self):
        now = datetime(2026,10,26,12,tzinfo=timezone.utc)
        feed = dict(leagueCode='CL', matches=[dict(id='1',kickoff='2026-10-26T13:00:00Z'),dict(id='2',kickoff='2026-10-26T11:00:00Z'),dict(id='3',kickoff='bad')])
        self.assertEqual([e['eventId'] for e in audit.feed_events(feed,now)],['1'])


if __name__ == '__main__':unittest.main()
