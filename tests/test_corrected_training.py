import os,sys,time,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.opponent_matchup import timestamp,OpponentMatchup
from materialize_betting_v2_direct import metric_values,kickoff,quote_market
from betting_v2.joint_count import JointCount,goal_periods,outcome

class CorrectedTrainingTest(unittest.TestCase):
    def test_timezone_invariance(self):
        old=os.environ.get('TZ')
        try:
            values=[]
            for zone in ('UTC','Asia/Bangkok','America/New_York'):
                os.environ['TZ']=zone;time.tzset()
                values.append((timestamp('2026-10-01'),kickoff('2026-10-01')))
            self.assertEqual(len(set(values)),1)
            self.assertEqual(values[0][0],timestamp('2026-10-01T00:00:00Z'))
        finally:
            if old is None:os.environ.pop('TZ',None)
            else:os.environ['TZ']=old
            time.tzset()
    def test_invalid_labels(self):
        row=dict(home_goals=1,away_goals=1,hthg=2,htag=0,normalized_stats={'HC':2.5,'AC':3})
        for metric in ('GOALS_1H','GOALS_2H','CORNERS'):self.assertIsNone(metric_values(row,metric))
        row['hthg']=.5;self.assertIsNone(metric_values(row,'GOALS_2H'))
    def test_same_day_no_lookahead(self):
        rows=[dict(_date=timestamp(day),_home='a',_away=b,_home_group='L',_away_group='L',home_goals=h,away_goals=0)
            for day,b,h in [('2026-01-01T10:00:00Z','b',1),('2026-01-01T18:00:00Z','c',3),('2026-01-02T18:00:00Z','b',2)]]
        def fake(x,y,columns,metric,alpha):return SimpleNamespace(coef_=np.zeros(len(columns)),intercept_=0.)
        with patch('betting_v2.opponent_matchup.fit_monotone_count',fake):m=OpponentMatchup(rows,timestamp('2026-01-03'),metric_values)
        features=[r[0] for r in m.train['GOALS_FT']]
        self.assertEqual([features[i]['recent_for'] for i in (0,2,4)],[0.,0.,2.])
    def test_exact_market_coherence(self):
        j=JointCount.from_means(2.1,.8,alpha=.2,draw_tilt=.3,btts_tilt=-.2)
        def spec(scope,side,line=None,team=''):return ('GOALS_FT',scope,side,team,line)
        h,d,a=[j.evaluate(spec('RESULT',s))[0] for s in ('HOME','DRAW','AWAY')]
        self.assertAlmostEqual(h+d+a,1)
        self.assertAlmostEqual(j.evaluate(spec('DC','HOME_OR_DRAW'))[0],h+d)
        np.testing.assert_allclose(j.evaluate(spec('DNB','HOME')),(h,d))
        over,push=j.evaluate(spec('TOTAL','OVER',2));under,push2=j.evaluate(spec('TOTAL','UNDER',2))
        self.assertAlmostEqual(over+under+push,1);self.assertEqual(push,push2)
        self.assertEqual(outcome(1,1,spec('TOTAL','OVER',2)),(0,1))
        periods,scenarios=goal_periods(j,.42,.47)
        np.testing.assert_allclose(np.array(periods['GOALS_1H'].means)+periods['GOALS_2H'].means,j.means)
        self.assertAlmostEqual(scenarios['GOALS_1H','HOME'][1],h)
        self.assertGreater(scenarios['GOALS_1H','HOME'][0].evaluate(spec('RESULT','AWAY'))[0],0)
        for s in (spec('TOTAL','OVER',2.25),spec('BTTS','MAYBE'),spec('TEAM','OVER',1.5)):
            with self.assertRaises(ValueError):j.evaluate(s)

if __name__=='__main__':unittest.main()
