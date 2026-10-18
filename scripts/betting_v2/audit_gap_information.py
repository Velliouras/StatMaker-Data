"""Post-hoc DEVELOPMENT diagnostic: GAP vs a prior intercept-only correction.

This checks whether the added statistics contribute beyond changing overall
goal rates. It never chooses betting thresholds or promotes a new candidate.
"""
import argparse,sys
from collections import Counter,defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.gap_goal_rates import fit,predict,FEATURES
from betting_v2.research_probability_recalibration import PRIOR,OCTOBER,FOLDS
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import digest,write
from betting_v2.research_gap_goals import goal_partitions
from betting_v2.cross_market_inputs import valid_quote
from betting_v2.research_git_archive import absolute_time
from betting_v2.validated_walk_forward import distribution
from betting_v2.research_app_markets import outcome_extended

def intercept_record(r):return dict(r,x=[[1.]+[0.]*(len(FEATURES)-1)]*2)

def summarize(rows):
    if not rows:return dict(fixtures=0,events=0)
    n=Counter(r['fixture'] for r in rows);w=np.asarray([1/n[r['fixture']] for r in rows]);w/=w.sum()
    scores={k:float(w@np.asarray([r[k] for r in rows])) for k in ('stat','gap','bias')}
    paired=defaultdict(list)
    for r in rows:paired[r['fixture']].append(r['gap']-r['bias'])
    a=np.asarray([np.mean(paired[k]) for k in sorted(paired)]);rng=np.random.default_rng(18102026)
    boot=[float(a[rng.integers(len(a),size=len(a))].mean()) for _ in range(2000)] if len(a)>=30 else []
    return dict(fixtures=len(n),events=len(rows),logLoss=scores,gapMinusIntercept=dict(mean=float(a.mean()),fixture95=list(map(float,np.quantile(boot,[.025,.975]))) if boot else None))

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();out=args.output
    train=read(out/'goal_training_features.json.gz');profiles=read(out/'goal_profiles.json');full=read(out/'goal_rate_banks.json')['fits']
    fitted={d:fit([intercept_record(r) for r in train if r['kickoff']+86400<=absolute_time(d+'T00:00:00Z')],absolute_time(d+'T00:00:00Z')) for d in FOLDS}
    result={};sources={}
    for name in ['goal_training_features.json.gz','goal_profiles.json','goal_rate_banks.json','goal_features_earliest.json.gz','goal_features_latest.json.gz']:
        sources[name]=digest((out/name).read_bytes())
    for packet in ('earliest','latest'):
        features={r['fixture']:r for r in read(out/f'goal_features_{packet}.json.gz')}
        quotes=read(PRIOR/f'quotes_{packet}.json.gz')+read(OCTOBER/f'quotes_{packet}.json.gz');quotes=[q for q in quotes if valid_quote(q) is None]
        groups,_=goal_partitions(quotes);byfixture=defaultdict(list)
        for g in groups:byfixture[g['positive']['fixture']].append(g['positive'])
        rows=[]
        for fixture,offers in byfixture.items():
            r=features.get(fixture)
            if not r or r['actual'] is None or not fitted[r['fold']]['fitted']:continue
            template=dict(metric='GOALS_FT',regime=r['regime'])
            laws={kind:distribution(dict(template,means=mu),profiles[r['fold']]) for kind,mu in
                [('stat',r['baseMeanBank'][0]),('gap',predict(r,full[r['fold']]['point'])),('bias',predict(intercept_record(r),fitted[r['fold']]))]}
            for q in offers:
                win,push=outcome_extended(*r['actual'],q['spec']);target=np.array([win,push,1-win-push]);loss={}
                for kind,law in laws.items():
                    pw,pp=law.evaluate(q['spec']);loss[kind]=-float(target@np.log(np.clip([pw,pp,1-pw-pp],1e-12,1)))
                rows.append(dict(fixture=fixture,fold=r['fold'],scope=q['spec'][1],**loss))
        expected=read(out/f'priced_{packet}.json')['probabilityScores'];periods={}
        for d in FOLDS[1:]:
            score=summarize([r for r in rows if r['fold']==d])
            for kind in ('stat','gap'):
                if abs(score['logLoss'][kind]-expected[d]['all'][kind]['logLoss'])>1e-9:raise ValueError('GAP audit does not reproduce published probability scores')
            periods[d]=dict(all=score,byScope={s:summarize([r for r in rows if r['fold']==d and r['scope']==s]) for s in ('TOTAL','TEAM','BTTS')})
        result[packet]=periods;print('GAP_INFORMATION_AUDIT',packet,{d:v['all'] for d,v in periods.items()},flush=True)
    write(out/'incremental_information_audit.json',dict(results=result,interceptFits=fitted,inputSha256=sources,
        codeSha256=digest(Path(__file__).read_bytes()),posthocDevelopmentDiagnostic=True,roiUsed=False,publishedForecastScoresReproduced=True,
        purpose='Separate added GAP statistics from a prior global goal-rate correction; no new winner selection'))

if __name__=='__main__':main()
