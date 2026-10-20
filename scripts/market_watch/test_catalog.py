import unittest,copy
from .catalog import app_market,mapped_spec,settlement,families,selection_text
from .build import config
from .quotes import bind

class AllMarketContracts(unittest.TestCase):
    def test_all_37_app_markets_have_explicit_registry_and_model(self):
        cfg=config()['markets'];self.assertEqual(len(cfg),37)
        self.assertTrue(all(v['modelId'] and v['source'] for v in cfg.values()))
        self.assertTrue(all('ASIAN' not in k and 'HANDICAP' not in k for k in cfg))
        self.assertEqual(sum(v['bootstrapActive'] for v in cfg.values()),4)
        self.assertEqual(sum(not v['publisherConnected'] for v in cfg.values()),5)

    def test_result_period_and_team_markets_are_not_collapsed(self):
        cases=[(('GOALS_FT','RESULT','HOME','',None),'1X2'),(('GOALS_1H','RESULT','HOME','',None),'HALF_TIME_1X2'),
          (('GOALS_FT','TOTAL','OVER','',2.5),'MATCH_GOALS'),(('GOALS_FT','TEAM','OVER','HOME',1.5),'TEAM_GOALS'),
          (('GOALS_1H','TEAM','OVER','HOME',.5),'TEAM_FIRST_HALF_GOALS'),(('GOALS_2H','TOTAL','OVER','',1.5),'SECOND_HALF_GOALS'),
          (('GOALS_FT','BOTH_HALVES','YES','HOME',None),'TEAM_SCORE_BOTH_HALVES'),(('CORNERS','RESULT','HOME','',None),'MOST_CORNERS')]
        for spec,market in cases:self.assertEqual(app_market(spec),market)

    def test_period_settlement_uses_real_half_labels_and_retains_missing(self):
        r=dict(home_goals=2,away_goals=1,hthg=1,htag=0)
        self.assertEqual(settlement(r,['GOALS_1H','TOTAL','OVER','',1.5]),(0.,0.))
        self.assertEqual(settlement(r,['GOALS_2H','TOTAL','OVER','',1.5]),(1.,0.))
        self.assertEqual(settlement(r,['GOALS_FT','HTFT','HOME_HOME','',None]),(1.,0.))
        self.assertEqual(settlement(r,['GOALS_FT','BOTH_HALVES','YES','HOME',None]),(1.,0.))
        self.assertIsNone(settlement(dict(home_goals=2,away_goals=1),['GOALS_FT','HTFT','HOME_HOME','',None]))

    def test_dnb_push_result_dc_and_combination_are_different_contracts(self):
        r=dict(home_goals=1,away_goals=1)
        self.assertEqual(settlement(r,['GOALS_FT','DNB','HOME','',None]),(0.,1.))
        self.assertEqual(settlement(r,['GOALS_FT','RESULT','HOME','',None]),(0.,0.))
        self.assertEqual(settlement(r,['GOALS_FT','DC','HOME_OR_DRAW','',None]),(1.,0.))
        self.assertEqual(settlement(r,['GOALS_FT','BTTS_TOTAL','YES_UNDER','',2.5]),(1.,0.))
        self.assertEqual(settlement(r,['GOALS_FT','RESULT_TOTAL','HOME_UNDER','',2.5]),(0.,0.))

    def test_cards_never_use_generic_aggregate_as_bookmaker_settlement(self):
        row=dict(normalized_stats=dict(HY=2,AY=3,HR=1,AR=0))
        for metric in ('CARDS','YELLOW_CARDS','RED_CARDS'):
            self.assertIsNone(settlement(row,[metric,'TOTAL','OVER','',4.5]))
        offer=dict(providerMarketName='Bookings Totals',selection='OVER',line=4.5)
        self.assertEqual(app_market(mapped_spec(offer,{})),'MATCH_CARDS')

    def test_most_shots_spread_is_not_a_three_way_market(self):
        q=dict(providerMarketName='Most Shots on Target',selection='HOME',line=-.5)
        self.assertIsNone(mapped_spec(q,{}))
        q['line']=None;self.assertEqual(mapped_spec(q,{}),('SOT','RESULT','HOME','',None))
        self.assertIsNone(mapped_spec(dict(q,providerMarketName='Asian handicap'),{}))

    def test_exact_and_combinations_cannot_fall_back_to_plain_goal_totals(self):
        q=dict(providerMarketName='Both Teams To Score + Over/Under',selection='YES / OVER 2.5',line=None)
        self.assertEqual(mapped_spec(q,{}),('GOALS_FT','BTTS_TOTAL','YES_OVER','',2.5))
        self.assertIsNone(mapped_spec(dict(q,selection='nonsense'),{}))
        self.assertEqual(selection_text(['GOALS_FT','HTFT','DRAW_HOME','',None]),'X/1')
        self.assertEqual(families(['GOALS_FT','RESULT_TOTAL','HOME_OVER','',2.5]),{'RESULT','GOALS'})

    def test_reobservation_binds_exact_latest_provider_selection_and_price(self):
        q=dict(providerEventId='1',kickoff=100.,bookmaker='B',sourceMarket='ML',selection='HOME',providerSelectionId='10',line=None,odd=1.91)
        match=dict(providerHomeTeam='H',providerAwayTeam='A')
        r=dict(q,_providerHome='H',_providerAway='A')
        key=('1',100.,'B','ML');lookup={key:dict(observed=90.,rows=[r])}
        self.assertEqual(bind(q,match,lookup),90.)
        self.assertIsNone(bind(dict(q,odd=2.1),match,lookup))
        self.assertIsNone(bind(dict(q,providerSelectionId='11'),match,lookup))
        lookup[key]['rows'].append(dict(r,odd=2.1));self.assertIsNone(bind(q,match,lookup))
        self.assertIsNone(bind(q,dict(providerHomeTeam='A',providerAwayTeam='H'),lookup))

if __name__=='__main__':unittest.main()
