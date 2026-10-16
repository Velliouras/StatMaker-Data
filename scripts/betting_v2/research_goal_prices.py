"""Historical priced evaluation after prospective goal choices are frozen.

All three arms use the same strict one-receipt-per-fixture quote sample. No
selection rule is adjusted from these returns. This is development evidence.
"""
import argparse,hashlib,json,sys
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.research_quotes import QuoteModel,assess,select,stats
from betting_v2.research_regularization import location,profile_mode,read_gz
from betting_v2.research_goal_factorized import ARMS,final_profile
from betting_v2.research_goal_forward import GoalComponents
from betting_v2.opponent_matchup import timestamp
from betting_v2.joint_count import outcome
from betting_v2.recovery_walk_forward import write

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    future=json.loads((args.output/'goal_factorized_forward_inventory.json').read_text())
    if datetime.fromisoformat(future['frozenAtUTC']).timestamp()>datetime.now(timezone.utc).timestamp():raise ValueError('Future clock invalid')
    choice_path=args.output/'goal_factorized_choices.json';choice_sha=hashlib.sha256(choice_path.read_bytes()).hexdigest()
    fold=next(f for f in json.loads(choice_path.read_text())['folds'] if f['testFrom']=='2026-10-01')
    counts=next(f for f in json.loads((args.output/'regularization_choices.json').read_text())['folds'] if f['testFrom']=='2026-10-01')
    history=direct.load_actual_results(timestamp('2026-10-15'))['GLOBAL']
    sha=hashlib.sha256(json.dumps(history,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if sha!=json.loads((args.baseline/'inventory.json').read_text())['dataSha256']:raise ValueError('Historical sample changed')
    actual={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    path=args.output/'expanded_quotes.json.gz';quotes=read_gz(path);receipts=defaultdict(set)
    for q in quotes:
        receipts[q['fixture']].add(q['sourceReceipt'])
        if not q['featureCutoff']<=q['observed']-86400 or not q['observed']<q['kickoff']:raise ValueError('Invalid retrospective timing')
    if any(len(v)!=1 for v in receipts.values()):raise ValueError('Mixed fixture receipts')
    engines={}
    def engine(name,profile):
        key=name,json.dumps(profile,sort_keys=True)
        if key not in engines:engines[key]=QuoteModel(name,history,location(args,name),profile)
        return engines[key]
    base_profile=json.loads((args.baseline/'full_2026-10-01_parameters.json').read_text());base_engine=engine('full',base_profile)
    components={};goal_profiles={};count_engines={}
    for arm in ARMS:
        choice=fold['primaryFT' if arm==ARMS[0] else 'experimentalFT'];profile=final_profile(args,choice['candidate'],'2026-10-01',choice['mode'])
        goal_profiles[arm]=profile;components[arm]=GoalComponents(engine(choice['candidate'],profile),profile,
            engine(fold['period']['candidate'],{}),fold['period']['finalCalibration']['offsets'])
    for metric,choice in counts['choices'].items():
        if metric=='GOALS':continue
        profile=profile_mode(json.loads((location(args,choice['candidate'])/(choice['candidate']+'_2026-10-01_parameters.json')).read_text()),choice['mode'])
        count_engines[metric]=engine(choice['candidate'],profile),profile
    prices=defaultdict(dict)
    for q in quotes:
        if tuple(q['spec'][:2])==('GOALS_FT','RESULT'):prices[q['fixture'],q['bookmaker'],q['sourceReceipt']][q['spec'][2]]=q['odd']
    options={name:[] for name in ('full',*ARMS)};missing={name:0 for name in options}
    for source in sorted(quotes,key=lambda q:(q['featureCutoff'],q['observed'],q['fixture'],str(q['spec']))):
        truth=actual[source['fixture']];q=dict(source,_home=truth['_home'],_away=truth['_away']);metric=q['spec'][0]
        for e in engines.values():e.advance(q['featureCutoff'])
        label=direct.metric_values(truth,metric)
        triplet=prices[q['fixture'],q['bookmaker'],q['sourceReceipt']];fav=None
        if set(triplet)=={'HOME','DRAW','AWAY'}:
            side=min(triplet,key=triplet.get)
            if side!='DRAW' and triplet[side]<=1.8:fav=side
        for name in options:
            if name=='full':prediction=base_engine.predict(q,metric);profile=base_profile
            elif metric.startswith('GOALS'):prediction=components[name].predict(q,metric);profile=goal_profiles[name]
            else:
                e,profile=count_engines[metric];prediction=e.predict(q,metric);full=components[name].predict(q,'GOALS_FT')
                prediction=(prediction[0],prediction[1],full[2]) if prediction is not None and full is not None else None
            if prediction is None or label is None:missing[name]+=1;continue
            pick=assess(q,prediction,profile,fav,'current_preference')
            if pick is not None:
                y,push=outcome(*label,q['spec']);options[name].append(dict(pick,y=y,actualPush=push))
    reports={}
    for name,rows in options.items():
        picks=select(rows);reports[name]=dict(overall=stats(picks),byTier={t:stats([r for r in picks if r['tier']==t]) for t in ('HIGH','CONFIRMED','DEVELOPING')},picks=picks)
        print('GOAL_PRICED',name,json.dumps(reports[name]['overall']),flush=True)
    previous=json.loads((args.output/'full_expanded_replay.json').read_text())['reports']['full::calibrated::current_preference']['overall']
    for key in ('bets','fixtures','wins','pushes'):
        if reports['full']['overall'][key]!=previous[key]:raise ValueError('Previous priced baseline failed round-trip: '+key)
    if abs(reports['full']['overall']['profitUnits']-previous['profitUnits'])>1e-10:raise ValueError('Baseline priced profit changed')
    if hashlib.sha256(choice_path.read_bytes()).hexdigest()!=choice_sha:raise ValueError('Model choices changed during replay')
    write(args.output/'goal_factorized_priced_replay.json',dict(reports=reports,quotes=len(quotes),fixtures=len(receipts),
        quoteSha256=hashlib.sha256(path.read_bytes()).hexdigest(),choiceSha256=choice_sha,
        futureBatchFrozenBeforeReplay=future['sha256'],futureFreezeUTC=future['frozenAtUTC'],
        scoredAtUTC=datetime.now(timezone.utc).isoformat(),missingPredictionOrLabels=missing,
        baselineRoundTripVerified=True,testDependentUpdates=0,providerCalls=0,untouchedHoldout=False,
        rule='Unchanged current_preference; same archived single-receipt inputs and at least 24h pre-quote feature lag',
        priceLimitation='Archived quotes; no execution/slippage evidence; small fixture-cluster samples'))

if __name__=='__main__':main()
