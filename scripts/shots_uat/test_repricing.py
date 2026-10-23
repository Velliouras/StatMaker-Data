import copy
import unittest

from .model import eligible_quote
from .repricing import price_updates, timestamp, validate_price_updates


class CurrentPriceTest(unittest.TestCase):
    def setUp(self):
        self.now = timestamp('2026-10-23T08:37:00Z')
        self.row = dict(selectionId='s1', apiFixtureId=1565507, bookmaker='Bet365',
                        market='MATCH_GOALS', selection='Under 3.5', team=None, line=3.5,
                        kickoff='2026-10-23T10:00:00Z', publishedAt='2026-10-22T00:11:16Z',
                        quoteUpdatedAt='2026-10-22T00:04:48Z', odd=1.875,
                        winProbability=.7407184168146499, pushProbability=0., outcome='PENDING',
                        model='ALL_MARKETS_JOINT_V1',
                        match=dict(id='72409744', kickoff='2026-10-23T10:00:00Z',
                                   providerHomeTeam='FC Dynamo Kyiv', providerAwayTeam='FK Kudrivka'))
        self.row['quoteSpec'] = dict(providerEventId='72409744', bookmaker='Bet365',
                                     kickoff=timestamp(self.row['kickoff']), sourceMarket='Totals',
                                     selection='UNDER', providerSelectionId='', line=3.5)
        self.key = ('72409744', timestamp(self.row['kickoff']), 'Bet365', 'Totals')
        self.lookup = {self.key: dict(observed=self.now-60, rows=[dict(
            selection='UNDER', providerSelectionId='', line=3.5, odd=1.825,
            _providerHome='FC Dynamo Kyiv', _providerAway='FK Kudrivka')])}

    def updates(self, row=None):
        return price_updates([row or self.row], {}, self.lookup, self.now, lambda *_: True, eligible_quote)

    def payload(self, updates):
        return dict(generatedAt='2026-10-23T08:37:00Z', issued=[self.row], candidates=[],
                    priceUpdates=updates, priceUpdateCount=len(updates))

    def test_dynamo_price_change_preserves_issue_and_recalculates_ev(self):
        before = copy.deepcopy(self.row)
        update, = self.updates()
        self.assertEqual(1.825, update['odd'])
        self.assertAlmostEqual(self.row['winProbability'] * 1.825 - 1, update['indicativeEV'])
        self.assertEqual(before, self.row)
        self.assertEqual([update], validate_price_updates(self.payload([update])))

    def test_price_below_or_on_floor_or_on_ceiling_and_nonfinite_are_not_returned(self):
        for odd in [1.727, 1.8, 3., float('nan'), float('inf')]:
            with self.subTest(odd=odd):
                self.lookup[self.key]['rows'][0]['odd'] = odd
                self.assertEqual([], self.updates())

    def test_unchanged_quote_is_left_to_legacy_candidates(self):
        self.lookup[self.key]['rows'][0]['odd'] = self.row['odd']
        self.assertEqual([], self.updates())

    def test_stale_future_or_pre_publication_observation_is_rejected(self):
        for clock in [self.now-86401, self.now+1, timestamp(self.row['publishedAt'])-1]:
            self.lookup[self.key]['observed'] = clock
            self.assertEqual([], self.updates())

    def test_kickoff_settled_or_paused_market_is_not_reintroduced(self):
        self.assertEqual([], self.updates(dict(self.row, kickoff='2026-10-23T08:37:00Z')))
        self.assertEqual([], self.updates(dict(self.row, outcome='WON')))
        self.assertEqual([], price_updates([self.row], {}, self.lookup, self.now, lambda *_: False, eligible_quote))

    def test_below_probability_floor_is_not_readmitted(self):
        self.assertEqual([], self.updates(dict(self.row, winProbability=.59)))

    def test_other_line_side_provider_selection_or_team_does_not_bind(self):
        quote = self.lookup[self.key]['rows'][0]
        for field, value in [('line', 2.5), ('selection', 'OVER'), ('providerSelectionId', 'other'),
                             ('_providerHome', 'Other')]:
            with self.subTest(field=field):
                saved = quote[field]; quote[field] = value
                self.assertEqual([], self.updates()); quote[field] = saved

    def test_other_bookmaker_and_conflicting_prices_are_rejected(self):
        self.assertEqual([], self.updates(dict(self.row, bookmaker='Other')))
        self.lookup[self.key]['rows'].append(dict(self.lookup[self.key]['rows'][0], odd=1.9))
        self.assertEqual([], self.updates())

    def test_shots_and_count_markets_use_exact_latest_line_without_mutating_original(self):
        match = dict(self.row['match'])
        row = dict(self.row, model='SHOTS_SOT_CATBOOST_V1', market='TEAM_SHOTS_ON_TARGET',
                   selection='Home Over 4.5', line=4.5, scope='TEAM', teamSide='HOME')
        key = (match['id'], 'Bet365', 'Total Shots on Target Home')
        market = dict(_observedAt=self.now-60, _kickoff=match['kickoff'],
                      _home=match['providerHomeTeam'], _away=match['providerAwayTeam'],
                      odds=[dict(hdp=4.5, over='1.91', under='1.83')])
        before = copy.deepcopy(row)
        updates = price_updates([row], {key: [market]}, {}, self.now, lambda *_: True, eligible_quote)
        self.assertEqual(1.91, updates[0]['odd']); self.assertEqual(before, row)
        # A newer snapshot withdrawing the line must not revive an older price.
        newer = dict(market, _observedAt=self.now-30, odds=[dict(hdp=5.5, over='1.91')])
        self.assertEqual([], price_updates([row], {key: [market, newer]}, {}, self.now, lambda *_: True, eligible_quote))
        row.update(model='COUNT_COUPLED_V1', market='TEAM_CORNERS', providerMarketName='Corners Totals Home')
        self.assertEqual(1, len(price_updates([row], {(match['id'], 'Bet365', 'Corners Totals Home'): [market]}, {}, self.now, lambda *_: True, eligible_quote)))

    def test_validator_rejects_changed_identity_history_ev_and_duplicate(self):
        update, = self.updates()
        for field, value in [('line', 2.5), ('bookmaker', 'Other'), ('originalOdd', 2.),
                             ('providerEventId', 'wrong'), ('indicativeEV', .99), ('odd', 1.7),
                             ('observedAt', '2026-10-23T08:38:00Z')]:
            with self.subTest(field=field):
                bad = dict(update, **{field: value})
                with self.assertRaises(ValueError): validate_price_updates(self.payload([bad]))
        with self.assertRaises(ValueError): validate_price_updates(self.payload([update, update]))
        p = self.payload([update]); p['candidates'] = [self.row]
        with self.assertRaises(ValueError): validate_price_updates(p)


if __name__ == '__main__':
    unittest.main()
