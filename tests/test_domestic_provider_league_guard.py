import sys
import unittest
from pathlib import Path

sys.path.insert(0, str((Path(__file__).resolve().parents[1] / 'scripts').resolve()))
import update_domestic_odds_api_io as odds


class DomesticProviderLeagueGuardTest(unittest.TestCase):
    def setUp(self):
        self.t1 = {
            'leagueCode': 'T1',
            'country': 'Turkey',
            'competition': 'Süper Lig',
            'providerLeagueSlug': None,
            'searchTerms': ['turkey super lig', 'turkish super lig', 'süper lig', 'super lig'],
        }

    def test_rejects_simulated_turkey_super_lig(self):
        providers = [{
            'name': 'Simulated Reality League - Turkey Super Lig SRL',
            'slug': 'simulated-reality-league-turkey-super-lig-srl',
            'eventsCount': 10,
        }]
        self.assertIsNone(odds.match_provider_league(self.t1, providers))

    def test_prefers_real_turkey_super_lig_over_simulated(self):
        simulated = {
            'name': 'Simulated Reality League - Turkey Super Lig SRL',
            'slug': 'simulated-reality-league-turkey-super-lig-srl',
            'eventsCount': 10,
        }
        real = {
            'name': 'Turkey Super Lig',
            'slug': 'turkey-super-lig',
            'eventsCount': 9,
        }
        matched = odds.match_provider_league(self.t1, [simulated, real])
        self.assertIsNotNone(matched)
        self.assertEqual('turkey-super-lig', matched['slug'])

    def test_stale_configured_slug_falls_back_to_strict_search_terms(self):
        configured = {
            'leagueCode': 'BRA',
            'country': 'Brazil',
            'competition': 'Serie A',
            'providerLeagueSlug': 'brazil-brasileiro-serie-a',
            'searchTerms': ['brazil serie a', 'brasil serie a', 'brasileirao', 'campeonato brasileiro serie a'],
        }
        providers = [
            {
                'name': 'Argentina - Serie A',
                'slug': 'argentina-serie-a',
                'eventsCount': 8,
            },
            {
                'name': 'Brazil - Serie A',
                'slug': 'brazil-serie-a',
                'eventsCount': 10,
            },
        ]
        matched = odds.match_provider_league(configured, providers)
        self.assertIsNotNone(matched)
        self.assertEqual('brazil-serie-a', matched['slug'])

    def test_brazil_country_guard_accepts_brasil_spelling(self):
        configured = {
            'leagueCode': 'BRA',
            'country': 'Brazil',
            'competition': 'Serie A',
            'searchTerms': ['brasil serie a'],
        }
        provider = {
            'name': 'Brasil - Serie A',
            'slug': 'brasil-serie-a',
            'eventsCount': 10,
        }
        self.assertTrue(odds.provider_country_matches(configured, provider))

    def test_stale_configured_slug_does_not_cross_country_on_fallback(self):
        configured = {
            'leagueCode': 'BRA',
            'country': 'Brazil',
            'competition': 'Serie A',
            'providerLeagueSlug': 'brazil-brasileiro-serie-a',
            'searchTerms': ['serie a'],
        }
        providers = [{
            'name': 'Argentina - Serie A',
            'slug': 'argentina-serie-a',
            'eventsCount': 8,
        }]
        self.assertIsNone(odds.match_provider_league(configured, providers))

    def test_j1_search_does_not_match_j3(self):
        configured = {
            'leagueCode': 'JPN',
            'country': 'Japan',
            'competition': 'J1 League',
            'providerLeagueSlug': None,
            'searchTerms': ['japan j1 league', 'j1 league', 'j league division 1'],
        }
        providers = [{
            'name': 'Japan - J3 League',
            'slug': 'japan-j3-league',
            'eventsCount': 100,
        }]
        self.assertIsNone(odds.match_provider_league(configured, providers))

    def test_j1_search_prefers_exact_division_token(self):
        configured = {
            'leagueCode': 'JPN',
            'country': 'Japan',
            'competition': 'J1 League',
            'providerLeagueSlug': None,
            'searchTerms': ['japan j1 league', 'j1 league', 'j league division 1'],
        }
        providers = [
            {'name': 'Japan - J3 League', 'slug': 'japan-j3-league', 'eventsCount': 100},
            {'name': 'Japan - J1 League', 'slug': 'japan-j1-league', 'eventsCount': 50},
        ]
        matched = odds.match_provider_league(configured, providers)
        self.assertIsNotNone(matched)
        self.assertEqual('japan-j1-league', matched['slug'])

    def test_rejects_configured_simulated_slug_too(self):
        configured = dict(self.t1)
        configured['providerLeagueSlug'] = 'simulated-reality-league-turkey-super-lig-srl'
        providers = [{
            'name': 'Simulated Reality League - Turkey Super Lig SRL',
            'slug': 'simulated-reality-league-turkey-super-lig-srl',
        }]
        self.assertIsNone(odds.match_provider_league(configured, providers))


if __name__ == '__main__':
    unittest.main()
