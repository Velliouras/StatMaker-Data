"""Pure probability and schema regression tests; never substitute for real-data run."""
import importlib.util
import math
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from collections import Counter
import sys
import types
import unittest
from pathlib import Path

stub_names={
    'audit_data':{'read_fixtures':lambda p:([],{},{}),'number':lambda x:float(x) if x is not None else None},
    'fixture_lookup':{'canonical_name':lambda x:str(x).casefold()},
    'freeze_prematch_shadow':{'XG_PARAMS':object(),'ELO_PARAMS':object(),'PARAMS_VERSION':'test'},
    'walk_forward_goals':{'make_rows':lambda x:([],[]),'model_lambdas':lambda *x:(1.5,1.2)},
    'walk_forward_elo':{'make_elo_rows':lambda x:([],[]),'lambda_rates':lambda *x:(1.5,1.2)},
    'scripts':{},'scripts.betting_v2':{},
    'scripts.betting_v2.production_sources':{'full_inventory':lambda *x:([],{}),'market_identity':lambda x:x},
    'scripts.betting_v2.production_contract':{'Blocked':ValueError},
}
for n,attrs in stub_names.items():
    m=types.ModuleType(n)
    for k,v in attrs.items():setattr(m,k,v)
    sys.modules[n]=m
spec=importlib.util.spec_from_file_location('live_v2_test',Path(__file__).with_name('run_real_prod_diagnostic.py'))
engine=importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

class MarketProbabilities(unittest.TestCase):
    def t(self,key,side,h,a,line=None):
        return engine.probability({'marketModelKey':key,'side':side,'line':line},h,a)
    def test_1x2_coherence(self):
        assert abs(sum(self.t('RESULT_1X2',s,1.6,1.1)[0] for s in ('HOME','DRAW','AWAY'))-1)<1e-9
    def test_double_chance(self):
        one=self.t('RESULT_1X2','HOME',1.6,1.1)[0]
        draw=self.t('RESULT_1X2','DRAW',1.6,1.1)[0]
        self.assertAlmostEqual(self.t('RESULT_DOUBLE_CHANCE','HOME_OR_DRAW',1.6,1.1)[0],one+draw)
    def test_dnb_push(self):
        self.assertAlmostEqual(self.t('RESULT_DNB','HOME',1.6,1.1)[1],self.t('RESULT_1X2','DRAW',1.6,1.1)[0])
    def test_btts(self):
        self.assertAlmostEqual(self.t('BTTS','YES',1.6,1.1)[0],(1-math.exp(-1.6))*(1-math.exp(-1.1)))
    def test_total_over_and_under(self):
        over=self.t('FULL_TIME_MATCH_TOTAL','OVER',1.6,1.1,2.5)[0]
        under=self.t('FULL_TIME_MATCH_TOTAL','UNDER',1.6,1.1,2.5)[0]
        self.assertAlmostEqual(over+under,1)
    def test_integer_line_has_push(self):
        win,push=self.t('FULL_TIME_MATCH_TOTAL','OVER',1.6,1.1,2.0)
        self.assertGreater(push,0)
        self.assertLess(win+push,1)
    def test_unsupported(self):
        self.assertIsNone(self.t('UNKNOWN','OVER',1.6,1.1,2.5))
    def test_no_quarter_lines(self):
        self.assertIsNone(self.t('FULL_TIME_MATCH_TOTAL','OVER',1.6,1.1,2.25))
    def test_full_inventory_path_without_certificates(self):
        now=datetime(2026,10,11,14,tzinfo=timezone.utc)
        source={
           'competitionId':'domestic','fixtureId':'real-fixture-1',
           'leagueCode':'E0','homeTeam':'Home City','awayTeam':'Away United',
           'canonicalHomeTeam':'Home City','canonicalAwayTeam':'Away United',
           'providerHomeTeam':'Home City','providerAwayTeam':'Away United',
           'kickoff':(now+timedelta(days=1)).isoformat(),
           'market':'1X2','selection':'1','odd':2.1,'line':None,
           'team':None,'family':'GOALS_FT','marketModelKey':'RESULT_1X2',
           'side':'HOME','selectionId':'immutable-raw-id'
        }
        history=[{'group':'E0|Premier League','home':'home city','away':'away united',
                  'date':now-timedelta(days=3),'stats':{'HxG':2.0,'AxG':.5},
                  'fixture_id':'prior-1','hg':3,'ag':0}]
        groups={'E0|Premier League':{}}
        with patch.object(engine,'full_inventory',return_value=([source],Counter())), \
             patch.object(engine,'read_fixtures',return_value=(history,Counter(),groups)), \
             patch.object(engine,'make_rows',return_value=([types.SimpleNamespace(fixture='real-fixture-1')],{})), \
             patch.object(engine,'make_elo_rows',return_value=([],{})), \
             patch.object(engine,'model_lambdas',return_value=(3.1,.45)):
            report=engine.evaluate(Path('.'),Path('fake.db'),'gen-real',now)
        self.assertEqual(report['rawQuotedRows'],1)
        self.assertEqual(report['candidateCount'],1)
        self.assertGreater(report['candidates'][0]['indicativeEV'],0)
        self.assertFalse(report['candidates'][0]['certified'])

if __name__=='__main__':unittest.main()