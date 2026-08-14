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
