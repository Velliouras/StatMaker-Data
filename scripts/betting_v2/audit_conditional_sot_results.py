"""Independent payout, bound-input and chronology checks on saved SOT evidence."""
import argparse,math,sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_probability_recalibration import ROOT,PRIOR
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.reliable_counts import metric_values

def main(out):
    binding=read(out/'input_bindings.json')
    assert all(digest((ROOT/p).read_bytes())==h for p,h in binding['files'].items()),'Bound input mismatch'
    history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    frame=read(out/'training_features.json.gz');assert len({r['fixture'] for r in frame})==len(frame)
    for r in frame:
        assert r['featureCutoff']==int(r['kickoff']//86400)*86400-86400
        assert r['maxObservedKickoff'] is None or r['maxObservedKickoff']<r['featureCutoff']
        assert all(0<=s<=n and int(s)==s and int(n)==n for s,n in zip(r['sot'],r['shots']))
    banks=read(out/'parameter_banks.json')
    for day,bank in banks['fits'].items():
        for fit in [bank['point'],*bank['replicas']]:
            prior=[r for r in frame if r['kickoff']+86400<=fit['trainingBefore']]
            assert len(prior)==fit['fixtures'];assert fit['maxTrainingKickoff']==max(r['kickoff'] for r in prior)
            assert fit['maxTrainingKickoff']+86400<=fit['trainingBefore'] and not fit['roiUsed']
    result={}
    for packet in ('earliest','latest'):
        doc=read(out/f'priced_{packet}.json');checkpoint=read(out/f'checkpoint_{packet}.json.gz');audits={}
        for name,report in doc['reports'].items():
            rows=report['picks'];assert len({r['fixture'] for r in rows})==len(rows);profit=0.;wins=pushes=0
            for r in rows:
                metric,scope,side,team,line=r['spec'];assert metric=='SOT' and scope in ('TOTAL','TEAM') and side in ('OVER','UNDER')
                assert line>=0 and line*2==int(line*2);assert 1.8<r['odd']<3 and r['p']>=.6 and r['p']*r['odd']+r['push']>1
                assert r['p']>=0 and r['push']>=0 and r['p']+r['push']<=1+1e-12
                assert abs(r['ev']-(r['p']*r['odd']+r['push']-1))<1e-12
                h,a=metric_values(truth[r['fixture']],'SOT');count=h if team=='HOME' else a if team=='AWAY' else h+a
                actualpush=float(count==line);actualwin=float((count>line if side=='OVER' else count<line) and not actualpush)
                assert actualwin==r['y'] and actualpush==r['actualPush'];profit+=actualwin*r['odd']+actualpush-1;wins+=actualwin;pushes+=actualpush
                if name.endswith('_conservative'):
                    low=r['_lower'];assert r['p']<=low['p']+1e-12 and r['push']<=low['push']+1e-12
                    assert low['support'] is None or low['support']>=.6
            o=report['overall'];assert o['bets']==len(rows)
            if rows:assert abs(profit-o['profitUnits'])<1e-10 and abs(profit/len(rows)-o['roi'])<1e-12 and wins==o['wins'] and pushes==o['pushes']
            audits[name]=dict(bets=len(rows),profitUnits=profit,uniqueFixture=True,thresholdsAndPayoutsVerified=True)
        proper=checkpoint['proper']
        for fold,score in doc['probabilityScores'].items():
            rows=[r for r in proper if r['fold']==fold];fixtures={r['fixture'] for r in rows}
            assert len(rows)==score['all']['events'] and len(fixtures)==score['all']['fixtures']
            for kind in ('stat','market','conditional'):
                grouped={f:[] for f in fixtures}
                for r in rows:
                    win,push=r['probabilities'][kind];a=np.asarray([win,push,1-win-push]);assert np.isfinite(a).all() and min(a)>=-1e-9
                    target=np.asarray([r['y'],r['push'],1-r['y']-r['push']]);grouped[r['fixture']].append(-float(target@np.log(np.clip(a,1e-12,1))))
                ll=float(np.mean([np.mean(v) for v in grouped.values()]));assert abs(ll-score['all'][kind]['logLoss'])<1e-12
        result[packet]=dict(reports=audits,properScoreRowsVerified=len(proper),properScoreFixtureSetsVerified=True)
    write(out/'verification.json',dict(status='PASS',trainingFixtures=len(frame),monthlyFitsVerified=39,inputsVerified=len(binding['files']),results=result,providerCalls=0,llmCalls=0))
    print('VERIFIED',len(frame),'training rows, 39 fits, 12 decision reports and all 3-state proper scores')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.output)
