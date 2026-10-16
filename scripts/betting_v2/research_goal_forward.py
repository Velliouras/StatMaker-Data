"""Freeze coherent goal controls on still-future immutable archived quotes."""
import argparse,gzip,hashlib,json,sys
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.opponent_matchup import timestamp
from betting_v2.research_quotes import QuoteModel,assess,select
from betting_v2.research_regularization import GROUPS,location,profile_mode
from betting_v2.research_goal_factorized import ARMS,compose_row,final_profile
from betting_v2.research_freeze_selected import QUOTE_FIELDS
from betting_v2.exact_forecast_adapter import forecast,prepare
from betting_v2.validated_walk_forward import zipped
from betting_v2.recovery_walk_forward import write

class GoalComponents:
    def __init__(self,ft_engine,ft_profile,period_engine,offsets):
        self.ft_engine=ft_engine;self.ft_profile=ft_profile;self.period_engine=period_engine;self.offsets=offsets;self.cache={}
    def predict(self,quote,metric):
        key=quote['fixture'],quote['featureCutoff'],metric
        if key not in self.cache:
            full=self.ft_engine.predict(quote,'GOALS_FT')
            if full is None:self.cache[key]=None
            elif metric=='GOALS_FT':self.cache[key]=full
            else:
                half=self.period_engine.predict(quote,metric)
                if half is None:self.cache[key]=None
                else:
                    row=compose_row(dict(full[0],fixture=quote['fixture']),dict(half[0],fixture=quote['fixture']),self.offsets)
                    self.cache[key]=row,prepare(row,self.ft_profile),full[2]
        return self.cache[key]

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--source',type=Path,required=True);ap.add_argument('--expected-source-sha256',required=True);args=ap.parse_args()
    raw=args.source.read_bytes();source_sha=hashlib.sha256(raw).hexdigest()
    if source_sha!=args.expected_source_sha256:raise ValueError('Original batch hash changed')
    original=json.loads(gzip.decompress(raw));now=datetime.now(timezone.utc).timestamp()
    choices_path=args.output/'goal_factorized_choices.json';choices=json.loads(choices_path.read_text())
    if choices['status']!='COMPLETE':raise ValueError('Goal choices not frozen')
    fold=max((f for f in choices['folds'] if timestamp(f['testFrom'])<=now),key=lambda f:f['testFrom'])
    if fold['testFrom']!='2026-10-01':raise ValueError('Only October frozen states are available in this study')
    old_path=args.output/'regularization_choices.json';count_choices=json.loads(old_path.read_text())
    count_fold=next(f for f in count_choices['folds'] if f['testFrom']==fold['testFrom'])
    history=direct.load_actual_results(timestamp('2026-10-15'))['GLOBAL']
    data_sha=hashlib.sha256(json.dumps(history,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if data_sha!=json.loads((args.baseline/'inventory.json').read_text())['dataSha256']:raise ValueError('Different research history')
    quotes=[{k:r[k] for k in QUOTE_FIELDS} for r in original['models']['full']['forecasts'] if r['observed']<=now<r['kickoff']]
    for q in quotes:
        if not q['featureCutoff']<=q['observed']-86400:raise ValueError('Insufficient feature lag')
        q['quoteAgeHoursAtFreeze']=(now-q['observed'])/3600
    quotes.sort(key=lambda q:(q['featureCutoff'],q['fixture'],str(q['spec']),q['bookmaker']))
    engines={};states={}
    def engine(name,profile):
        profile_sha=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest();key=name,profile_sha
        if key not in engines:
            directory=location(args,name);engines[key]=QuoteModel(name,history,directory,profile)
            path=directory/(name+'_2026-10-01_pretest_state.json.gz')
            states[name+'::'+profile_sha]=dict(stateSha256=hashlib.sha256(path.read_bytes()).hexdigest(),effectiveProfile=profile)
        return engines[key]
    base_profile=json.loads((args.baseline/'full_2026-10-01_parameters.json').read_text());baseline_engine=engine('full',base_profile)
    components={};goal_profiles={}
    for arm in ARMS:
        choice=fold['primaryFT' if arm==ARMS[0] else 'experimentalFT']
        profile=final_profile(args,choice['candidate'],'2026-10-01',choice['mode']);goal_profiles[arm]=profile
        components[arm]=GoalComponents(engine(choice['candidate'],profile),profile,
            engine(fold['period']['candidate'],{}),fold['period']['finalCalibration']['offsets'])
    metric_groups={m:g for g,metrics in GROUPS.items() for m in metrics};count_engines={}
    for group,choice in count_fold['choices'].items():
        if group=='GOALS':continue
        path=location(args,choice['candidate'])/(choice['candidate']+'_2026-10-01_parameters.json')
        profile=profile_mode(json.loads(path.read_text()),choice['mode']);count_engines[group]=engine(choice['candidate'],profile),profile
    prices=defaultdict(dict)
    for q in quotes:
        if tuple(q['spec'][:2])==('GOALS_FT','RESULT'):prices[q['fixture'],q['bookmaker'],q['sourceReceipt']][q['spec'][2]]=q['odd']
    models={name:dict(forecasts=[],options=[]) for name in ('full',*ARMS)}
    for q in quotes:
        for e in engines.values():e.advance(q['featureCutoff'])
        triplet=prices[q['fixture'],q['bookmaker'],q['sourceReceipt']];fav=None
        if set(triplet)=={'HOME','DRAW','AWAY'}:
            side=min(triplet,key=triplet.get)
            if side!='DRAW' and triplet[side]<=1.8:fav=side
        metric=q['spec'][0]
        for name in models:
            if name=='full':prediction=baseline_engine.predict(q,metric);profile=base_profile
            elif metric.startswith('GOALS'):
                prediction=components[name].predict(q,metric);profile=goal_profiles[name]
            else:
                e,profile=count_engines[metric_groups[metric]];prediction=e.predict(q,metric)
                full=components[name].predict(q,'GOALS_FT')
                if prediction is not None and full is not None:prediction=prediction[0],prediction[1],full[2]
                else:prediction=None
            if prediction is None:continue
            row,prepared,_=prediction;result=forecast(row,profile,q['spec'],prepared)
            models[name]['forecasts'].append(dict(q,winProbability=result['winProbability'],pushProbability=result['pushProbability'],
                homeMean=result['homeMean'],awayMean=result['awayMean'],status='UNSCORED'))
            pick=assess(q,prediction,profile,fav,'current_preference')
            if pick is not None:models[name]['options'].append(pick)
    for d in models.values():d['selected']=select(d.pop('options'))
    old={(r['fixture'],r['bookmaker'],tuple(r['spec'])):r for r in original['models']['full']['forecasts']}
    delta=max(abs(r['winProbability']-old[r['fixture'],r['bookmaker'],tuple(r['spec'])]['winProbability']) for r in models['full']['forecasts'])
    if delta>1e-12:raise ValueError('Baseline failed probability round-trip')
    locked_delta=max(abs(r['winProbability']-old[r['fixture'],r['bookmaker'],tuple(r['spec'])]['winProbability'])
        for r in models[ARMS[0]]['forecasts'] if r['spec'][0]=='GOALS_FT')
    if locked_delta>1e-12:raise ValueError('Locked FT forecasts changed')
    stamp=datetime.fromtimestamp(now,timezone.utc).strftime('%Y%m%dT%H%M%SZ');target=args.output/('goal_factorized_forward_frozen_'+stamp+'.json.gz')
    frozen=dict(frozenAtUTC=datetime.fromtimestamp(now,timezone.utc).isoformat(),models=models,states=states,goalChoices=fold,
        sourceBatchSha256=source_sha,goalSelectionSha256=hashlib.sha256(choices_path.read_bytes()).hexdigest(),
        inheritedCountSelectionSha256=hashlib.sha256(old_path.read_bytes()).hexdigest(),
        primaryArm=ARMS[0],experimentalArm=ARMS[1],futureOutcomesRead=False,originalBatchesModified=False,
        overlapsEarlierBatches=True,allKickoffsAfterFreeze=True,providerCalls=0,
        rules=dict(prices='Same archived single-receipt quote inputs, filtered to still-future kickoffs',
            executionEvidence=False,featureLag='At least 24h before quote',selection='Unchanged current_preference thresholds',
            goalCoherence='One FT PMF and complementary calibrated thinning, no future actual goals required'),
        baselineMaxDifference=delta,lockedFTMaxDifference=locked_delta)
    zipped(target,frozen)
    overview={name:dict(forecasts=len(d['forecasts']),fixtures=len({r['fixture'] for r in d['forecasts']}),selected=len(d['selected']),
        tiers={t:sum(r['tier']==t for r in d['selected']) for t in ('HIGH','CONFIRMED','DEVELOPING')}) for name,d in models.items()}
    write(args.output/'goal_factorized_forward_inventory.json',dict(path=target.name,sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        frozenAtUTC=frozen['frozenAtUTC'],overview=overview,primaryArm=ARMS[0],experimentalArm=ARMS[1],
        sourceBatchSha256=source_sha,overlapsEarlierBatches=True,status='UNSCORED',executionEvidence=False,
        baselineMaxDifference=delta,lockedFTMaxDifference=locked_delta,noAutonomousScoringJob=True))
    print('GOAL_FORWARD_FROZEN',json.dumps(overview),flush=True)

if __name__=='__main__':main()
