import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from betting_v2.research_git_archive import (
    absolute_time, extract_snapshot, remove_conflicts, exact_join, settle_selected)
from betting_v2.research_quotes import feature_cutoff


class GitArchiveResearchTests(unittest.TestCase):
    def setUp(self):
        self.source = dict(sha='a' * 40, sha256='b' * 64, committedAtUTC='2026-10-03T12:00:00Z')
        self.market = dict(name='Corners Totals', updatedAt='2026-10-03T11:45:00Z',
            odds=[dict(hdp=9.5, over='2.05', under='1.80')])
        self.match = dict(id='123', kickoff='2026-10-03T14:00:00Z', teamMappingStatus='matched',
            homeTeam='Alpha', awayTeam='Beta', providerHomeTeam='Alpha FC', providerAwayTeam='Beta FC',
            providerMarkets=[dict(bookmaker='Bet365', exactProviderPayload=True, market=self.market)])
        self.doc = dict(generatedAt='2026-10-03T11:59:00Z',
            leagues=[dict(leagueCode='TEST', matches=[self.match])])

    def test_archive_commit_time_is_observation_not_provider_update(self):
        rows, _ = extract_snapshot(self.doc, self.source)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['observed'], absolute_time(self.source['committedAtUTC']))
        self.assertEqual(rows[0]['featureCutoff'], feature_cutoff(rows[0]['observed']))
        self.assertFalse(rows[0]['executionEvidence'])

    def test_old_market_timestamp_does_not_rescue_post_kickoff_archive(self):
        self.match['kickoff'] = self.source['committedAtUTC']
        rows, rejected = extract_snapshot(self.doc, self.source)
        self.assertEqual(rows, [])
        self.assertEqual(rejected['archivedAtOrAfterKickoff'], 1)

    def test_missing_future_and_stale_market_timestamps_are_rejected(self):
        for value in (None, '2026-10-03T12:01:00Z', '2026-10-02T11:59:59Z'):
            with self.subTest(value=value):
                self.market['updatedAt'] = value
                rows, rejected = extract_snapshot(self.doc, self.source)
                self.assertFalse(rows)
                self.assertEqual(rejected['missingFutureOrStaleMarketTimestamp'], 1)

    def test_future_generated_payload_fails_closed(self):
        self.doc['generatedAt'] = '2026-10-03T12:00:01Z'
        with self.assertRaises(ValueError): extract_snapshot(self.doc, self.source)
        self.assertIsNone(absolute_time('2026-10-03T12:00:00'))

    def test_non_exact_payload_and_unapproved_identity_are_rejected(self):
        self.match['providerMarkets'][0]['exactProviderPayload'] = False
        self.assertFalse(extract_snapshot(self.doc, self.source)[0])
        self.match['providerMarkets'][0]['exactProviderPayload'] = True
        self.match['teamMappingStatus'] = 'partial'
        self.assertFalse(extract_snapshot(self.doc, self.source)[0])

    def test_conflicting_duplicate_never_becomes_valid_again(self):
        row = extract_snapshot(self.doc, self.source)[0][0]
        rows, rejected = remove_conflicts([row, dict(row, odd=2.2), row])
        self.assertEqual(rows, [])
        self.assertEqual(rejected, 1)

    def test_exact_join_requires_unique_canonical_pair_and_kickoff(self):
        rows = extract_snapshot(self.doc, self.source)[0]
        actual = dict(_date=rows[0]['kickoff'], _home='alpha', _away='beta')
        period = dict(kickoffFromUTC='2026-10-01T00:00:00Z', kickoffBeforeUTC='2026-10-16T00:00:00Z', featureFromUTC='2026-10-01T00:00:00Z')
        self.assertEqual(len(exact_join(rows, [actual], period)[0]), 2)
        self.assertFalse(exact_join(rows, [actual, actual], period)[0])
        self.assertFalse(exact_join(rows, [dict(actual, _date=actual['_date']+60)], period)[0])

    def test_missing_actual_is_pending_and_integer_push_is_not_a_loss(self):
        pick = dict(fixture='f', spec=['CORNERS', 'TOTAL', 'OVER', '', 10.], odd=2.)
        scored, missing = settle_selected([pick], {})
        self.assertFalse(scored); self.assertEqual(missing, {'CORNERS': 1})
        scored, missing = settle_selected([pick], {'f': dict(normalized_stats=dict(HC=6, AC=4))})
        self.assertFalse(missing); self.assertEqual(scored[0]['actualPush'], 1.)
        self.assertEqual(scored[0]['y'], 0.)


if __name__ == '__main__': unittest.main()
