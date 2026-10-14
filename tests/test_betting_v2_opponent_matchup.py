import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from collections import defaultdict
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.opponent_matchup import Identities,OpponentMatchup,load_competitive_history,timestamp
from materialize_betting_v2_direct import split_goal_periods,stage_betting_v2_quotes
from merge_betting_v2_manifest import merged
from materialize_betting_v2_direct import sampled_distribution,scenario_distributions,evaluate
from betting_v2.monotone_count import fit_monotone_count

class MatchupContractTest(unittest.TestCase):
    def test_alias_country_and_diacritic_identity(self):
        ids=Identities()
        ids.add('Norway','Bodø/Glimt',['Bodo Glimt'])
        self.assertEqual(ids.resolve('Bodø/Glimt'),ids.resolve('Bodo Glimt'))
        ids.add('Greece','Olympiakos');ids.add('Cyprus','Olympiakos')
        self.assertIsNone(ids.resolve('Olympiakos'))
        self.assertNotEqual(ids.resolve('Olympiakos','Greece'),ids.resolve('Olympiakos','Cyprus'))

    def test_one_physical_match_no_friendlies_or_future(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            def write(path,obj):
                p=root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj))
            write('data/statmaker/domestic_enriched/index.json',dict(leagues=[dict(league_code='NOR',country='Norway',output_path='data/statmaker/domestic_enriched/nor.json')]))
            write('data/statmaker/domestic_enriched/nor.json',dict(matches=[dict(status='FT',home_team='Brann',away_team='Bodø/Glimt',date_utc='2026-10-01T17:00:00Z',home_goals=2,away_goals=1,normalized_stats={'HST':7})]))
            raw=dict(homeTeam='SK Brann',awayTeam='Bodo Glimt',date='2026-10-01',homeGoals=2,awayGoals=1,competition='Eliteserien')
            write('data/statmaker/uefa_support_history.json',dict(participants=[dict(country='Norway',leagueCode='NOR',canonicalTeam='Brann',aliases=['SK Brann'],
                matches=[raw,dict(raw,date='2026-10-02',competition='Friendlies Clubs'),dict(raw,date='2026-10-03',competition='UEFA Champions League Women'),dict(raw,date='2026-11-01')])]))
            rows,_=load_competitive_history(root,timestamp('2026-10-14T12:00:00Z'))
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['normalized_stats']['HST'],7)

    def test_extra_time_does_not_enter_fulltime_count(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);p=root/'data/statmaker/domestic_enriched/index.json';p.parent.mkdir(parents=True);p.write_text('{"leagues":[]}')
            p=root/'data/api_football/league_tables/CL_2026_fixtures.json';p.parent.mkdir(parents=True)
            p.write_text(json.dumps(dict(response=[dict(fixture=dict(date='2026-09-01T17:00:00Z',status=dict(short='AET')),teams=dict(home=dict(id=10,name='A'),away=dict(id=11,name='B')),
                goals=dict(home=3,away=2),score=dict(fulltime=dict(home=1,away=1),halftime=dict(home=0,away=1)))])))
            rows,_=load_competitive_history(root,timestamp('2026-10-14T12:00:00Z'))
            self.assertEqual((rows[0]['home_goals'],rows[0]['away_goals']),(1,1))

    def test_observed_xg_for_both_sides(self):
        m=OpponentMatchup.__new__(OpponentMatchup)
        m.history=defaultdict(lambda:defaultdict(list));m.groups={}
        m.elo=defaultdict(lambda:1500.0);m.xg=defaultdict(list,home=[(2.1,.6)],away=[(.9,1.7)])
        h=m.features('home','away','GOALS_FT',True);a=m.features('away','home','GOALS_FT',False)
        self.assertEqual((h['xg_for'],h['opponent_xg_against']),(2.1,1.7))
        self.assertEqual((a['xg_for'],a['opponent_xg_against']),(.9,.6))

    def test_halves_share_the_same_fulltime_realization(self):
        full=[(0,0),(1,4),(5,2),(3,1)]*2500
        first,second=split_goal_periods(full,.42,.46,'contract')
        self.assertEqual(len(first),10000)
        self.assertTrue(all((h+a,x+y)==ft for ft,(h,x),(a,y) in zip(full,first,second)))
        self.assertEqual((first,second),split_goal_periods(full,.42,.46,'contract'))

    def test_first_half_quote_cannot_be_fulltime(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);db=root/'databases/test.db';db.parent.mkdir()
            feeds=root/'files/app_ready_odds';feeds.mkdir(parents=True);exact=root/'exact';exact.mkdir()
            con=sqlite3.connect(db);con.row_factory=sqlite3.Row
            con.execute('CREATE TABLE prepared_fixture_matches (competition_id,snapshot_version,match_key,id,kickoff,home_team,away_team,canonical_home_team,canonical_away_team,provider_home_team,provider_away_team)')
            con.execute('INSERT INTO prepared_fixture_matches VALUES (?,?,?,?,?,?,?,?,?,?,?)',('conference_league','v','m','1','2026-10-15T18:00:00Z','Home','Away','Home','Away','Home','Away'))
            q=dict(market='DOUBLE_CHANCE',selection='X2',odds=2.625,bookmaker='Bet365',exactBookmakerOdds=True,providerMarketName='Double Chance HT',providerMarketFamily='HALF_TIME_DOUBLE_CHANCE')
            (feeds/'domestic.json').write_text('{"matches":[]}')
            for code in ('champions_league','europa_league','conference_league'):
                matches=[] if code!='conference_league' else [dict(id='1',kickoff='2026-10-15T18:00:00Z',markets=[q,dict(q,market='HALF_TIME_DOUBLE_CHANCE')])]
                (exact/(code+'_odds.json')).write_text(json.dumps(dict(matches=matches)))
            stage_betting_v2_quotes(con,db,exact)
            self.assertEqual([tuple(v) for v in con.execute('SELECT market,odd FROM prepared_betting_v2_quotes')],[('HALF_TIME_DOUBLE_CHANCE',2.625)])

    def test_new_stats_survive_betting_publish_and_stale_base_is_rejected(self):
        latest=dict(generatedAt='2026-10-14T19:00:00Z',contentVersion='stats-new',metadata={'stats':'new'},artifacts=[dict(id='app_ready_betting_bundle',sha256='base'),dict(id='stats',sha256='fresh')])
        source=dict(generatedAt='2026-10-14T18:59:00Z',metadata={'bettingV2DirectBaseSha256':'base'},artifacts=[dict(id='app_ready_betting_bundle',sha256='v2')])
        result=merged(source,latest)
        self.assertEqual(result['artifacts'][1],latest['artifacts'][1])
        self.assertEqual(result['metadata']['stats'],'new')
        self.assertEqual(result['generatedAt'],latest['generatedAt'])
        latest['artifacts'][0]['sha256']='new-base'
        with self.assertRaises(ValueError):merged(source,latest)


class IncreasingStrengthTest(unittest.TestCase):
    def test_increasing_elo_cannot_lower_expected_goals(self):
        import numpy as np
        from scipy.sparse import csr_matrix
        x=csr_matrix(np.asarray([[0.],[1.],[2.],[3.]]))
        # This data would make an unconstrained fit learn the inverted sign.
        fit=fit_monotone_count(x,np.asarray([6.,4.,2.,1.]),{'elo_gap':0},'GOALS_FT',.2)
        self.assertGreaterEqual(fit.coef_[0],0.)
        values=fit.predict(x)
        self.assertTrue(np.all(np.diff(values)>=-1e-12))

    def test_attack_and_opponent_xga_do_not_have_inverted_effects(self):
        import numpy as np
        from scipy.sparse import csr_matrix
        x=csr_matrix(np.asarray([[0.,0.],[1.,0.],[0.,1.],[1.,1.],[2.,2.]]))
        fit=fit_monotone_count(x,np.asarray([8.,6.,6.,4.,1.]),
            {'recent_for':0,'opponent_xg_against':1},'SOT',.2)
        self.assertTrue((fit.coef_>=0).all())
        low,high=fit.predict(csr_matrix([[0.,0.],[2.,2.]]))
        self.assertGreaterEqual(high,low-1e-12)

class ScenarioProbabilityTest(unittest.TestCase):
    def test_conditioning_does_not_replace_raw_market_probability(self):
        full=[(1,0),(3,0),(2,1),(0,2),(1,1)]
        spec=('GOALS_FT','DC','AWAY_OR_DRAW','',None)
        before=evaluate(sampled_distribution(full),spec)[0]
        home,away=scenario_distributions(full)
        self.assertEqual(before,.4)
        self.assertEqual(evaluate(home,spec)[0],0.)
        self.assertEqual(evaluate(away,spec)[0],1.)
        self.assertEqual(evaluate(sampled_distribution(full),spec)[0],before)

    def test_halftime_support_uses_fulltime_winner_indices(self):
        full=[(2,0),(1,2),(3,1),(0,0)]
        first=[(0,0),(1,0),(2,0),(0,0)]
        home,away=scenario_distributions(first,full)
        spec=('GOALS_1H','DC','AWAY_OR_DRAW','',None)
        self.assertEqual(home['runs'],2)
        self.assertEqual(away['runs'],1)
        self.assertEqual(evaluate(home,spec)[0],.5)
        self.assertEqual(evaluate(away,spec)[0],0.)

if __name__=='__main__':unittest.main()

