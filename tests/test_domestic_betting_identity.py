import copy
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import canonical_team_identity as canonical
import domestic_betting_identity as identity
import refresh_domestic_live_july_odds as pipeline
import refresh_primary_odds as primary


def indexes():
    teams = {'DNK': ['Brondby', 'Aarhus'], 'EGY': ['Asyut Petrol', 'El Gouna FC'],
             'T1': ['Beşiktaş', 'Başakşehir', 'Rizespor', 'Trabzonspor', 'Galatasaray', 'Fenerbahçe']}
    registry = [{'leagueCode': code, 'targetAppSeason': '2026-2027'} for code in teams]
    context = SimpleNamespace(ROSTER_PATH='roster',
        load_json=lambda path, default: {'leagues': [dict(leagueCode=code, appSeason='2026-2027', teams=names)
            for code, names in teams.items()]} if path == 'roster' else {},
        stats_fetch=SimpleNamespace(cache_path_for=lambda league: 'cache'))
    return canonical.domestic_indexes(SimpleNamespace(ALIASES_PATH='aliases'), context, registry)


def match(home, away, status='partial'):
    return dict(id='71925074', date='2026-10-27', kickoff='2026-10-27T12:00:00Z',
        homeTeam=home, awayTeam=away, providerHomeTeam=home, providerAwayTeam=away,
        teamMappingStatus=status, usableForStats=False, scheduleOnly=True,
        scheduleVerified=True, scheduleSource='odds-api-io-events',
        markets=[dict(market='1X2', selection='Home', odds=1.91, bookmaker='Unibet', exactBookmakerOdds=True)])


class DomesticBettingIdentityTest(unittest.TestCase):
    def test_five_failed_fixture_shapes_keep_exact_prices_and_provider_names(self):
        cases = [('DNK', 'Broendby IF', 'AGF Aarhus', 'Brondby', 'Aarhus'),
                 ('EGY', 'Asyut Petroleum', 'El Gouna FC', 'Asyut Petrol', 'El Gouna FC'),
                 ('T1', 'Beşiktaş', 'Başakşehir', 'Beşiktaş', 'Başakşehir'),
                 ('T1', 'Rizespor', 'Trabzonspor', 'Rizespor', 'Trabzonspor'),
                 ('T1', 'Galatasaray', 'Fenerbahçe', 'Galatasaray', 'Fenerbahçe')]
        for code, home, away, expected_home, expected_away in cases:
            with self.subTest(code=code, home=home):
                row = match(home, away, 'schedule_only_api_football' if code == 'T1' else 'partial')
                quotes = copy.deepcopy(row['markets'])
                feed = {'leagues': [dict(leagueCode=code, matches=[row])]}
                result = identity.reconcile_feed(feed, indexes())
                self.assertEqual(result['mappedFixtures'], 1)
                self.assertEqual((row['homeTeam'], row['awayTeam']), (expected_home, expected_away))
                self.assertEqual((row['providerHomeTeam'], row['providerAwayTeam']), (home, away))
                self.assertEqual(row['markets'], quotes)
                self.assertTrue(identity.betting_identity_ready(row))
                self.assertFalse(row['scheduleOnly'])
                self.assertEqual(identity.reconcile_feed(feed, indexes())['mappedFixtures'], 0)

    def test_team_selection_uses_repaired_canonical_name_without_changing_price_or_raw_label(self):
        row = match('Broendby IF', 'Aarhus')
        row['markets'] = [dict(market='TEAM_SHOTS', team='Broendby IF',
            selection='Broendby IF Shots Over 12.5', odds=1.93, providerMarketName='Home Total Shots')]
        identity.reconcile_feed({'leagues': [dict(leagueCode='DNK', matches=[row])]}, indexes())
        self.assertEqual(row['markets'][0], dict(market='TEAM_SHOTS', team='Brondby',
            selection='Brondby Shots Over 12.5', odds=1.93, providerMarketName='Home Total Shots'))

    def test_unknown_schedule_identity_retains_calendar_row_without_quotes(self):
        row = match('Unknown Denmark Club', 'Aarhus')
        row['scheduleOnly'] = False
        feed = {'leagues': [dict(leagueCode='DNK', matches=[row])]}
        result = identity.reconcile_feed(feed, indexes())
        self.assertEqual(result['suppressedFixtures'], 1)
        self.assertEqual(row['markets'], [])
        self.assertEqual((row['id'], row['kickoff']), ('71925074', '2026-10-27T12:00:00Z'))
        self.assertTrue(row['scheduleVerified'])
        self.assertTrue(row['scheduleOnly'])
        self.assertFalse(identity.betting_identity_ready(row))

    def test_ambiguous_identity_and_wrong_league_are_not_promoted(self):
        mapping = indexes()
        mapping['DNK'].add_alias('Broendby IF', 'Other Club', priority=canonical.VERIFIED_ALIAS_PRIORITY)
        for code in ('DNK', 'OTHER'):
            row = match('Broendby IF', 'Aarhus')
            identity.reconcile_feed({'leagues': [dict(leagueCode=code, matches=[row])]}, mapping)
            self.assertFalse(identity.betting_identity_ready(row))
            self.assertEqual(row['markets'], [])

    def test_unverified_row_still_fails_strict_validator_after_repair_attempt(self):
        row = match('Unknown Denmark Club', 'Aarhus')
        row['scheduleVerified'] = False
        feed = {'leagues': [dict(leagueCode='DNK', matches=[row])]}
        identity.reconcile_feed(feed, indexes())
        self.assertFalse(row['scheduleVerified'])
        with self.assertRaises(RuntimeError):
            pipeline.validate_feed(feed, [{'leagueCode': 'DNK'}], datetime(2026, 10, 26).date())

    def test_primary_cannot_attach_quotes_to_unresolved_schedule_even_with_fixture_id(self):
        row = match('Unknown Denmark Club', 'Aarhus')
        row['apiFootballFixtureId'] = '10'
        feed = {'leagues': [dict(leagueCode='DNK', matches=[row])]}
        index = SimpleNamespace(policy={}, resolve=lambda *args: self.fail('Unmapped fixture reached provider lookup'))
        with patch.object(primary, 'urlopen') as request:
            result = primary.refresh([feed], [{}], index, {}, 'key', datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertEqual(result['apiCalls'], 0)
        request.assert_not_called()
        self.assertEqual(row['markets'], [])

    def test_unibet_archive_cannot_restore_quotes_to_unresolved_schedule(self):
        row = match('Unknown Denmark Club', 'Aarhus')
        archive_row = copy.deepcopy(row)
        archive_row['providerMarkets'] = [dict(bookmaker='Unibet', exactProviderPayload=True,
            market=dict(name='ML', updatedAt='2026-10-26T11:00:00Z', odds=[]))]
        feed = {'leagues': [dict(leagueCode='DNK', matches=[row])]}
        archive = {'leagues': [dict(leagueCode='DNK', matches=[archive_row])]}
        self.assertEqual(primary.restore_archived_unibet(feed, archive, datetime(2026, 10, 26, 12, tzinfo=timezone.utc)), 0)
        self.assertEqual(row['markets'], [])
        self.assertEqual(archive_row['providerMarkets'][0]['market']['name'], 'ML')


if __name__ == '__main__':
    unittest.main()
