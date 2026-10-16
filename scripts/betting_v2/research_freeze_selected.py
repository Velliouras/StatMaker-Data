"""Freeze the prior-window selection rule on still-future archived quotes.

This is a distinct later batch. The original prospective batch stays immutable.
Quotes are reused as archived inputs, without any claim of current execution.
"""
import argparse,gzip,hashlib,json,sys
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.opponent_matchup import timestamp
from betting_v2.research_quotes import QuoteModel,assess,select
from betting_v2.research_regularization import GROUPS,profile_mode,location,read_gz
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.validated_walk_forward import zipped
from betting_v2.recovery_walk_forward import write

QUOTE_FIELDS=('fixture','bookmaker','spec','odd','observed','kickoff','featureCutoff',
    'sourceReceipt','sourceSha256','providerEventId','_home','_away')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True);ap.add_argument('--source',type=Path,required=True)
    ap.add_argument('--expected-source-sha256',required=True);args=ap.parse_args()
    source=args.source.read_bytes();source_sha=hashlib.sha256(source).hexdigest()
    if source_sha!=args.expected_source_sha256:raise ValueError('Source batch hash mismatch')
    original=json.loads(gzip.decompress(source));now=datetime.now(timezone.utc).timestamp()
    selection_path=args.output/'regularization_choices.json';selection=json.loads(selection_path.read_text())
    if selection['status']!='COMPLETE':raise ValueError('Incomplete prior-window rule')
    fold=max((f for f in selection['folds'] if timestamp(f['testFrom'])<=now),key=lambda f:f['testFrom'])
    if fold['testFrom']!='2026-10-01':raise ValueError('This study uses the frozen October pretest states')
    history=direct.load_actual_results(timestamp('2026-10-15'))['GLOBAL']
    sha=hashlib.sha256(json.dumps(history,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if sha!=json.loads((args.baseline/'inventory.json').read_text())['dataSha256']:raise ValueError('Different research history')
    quotes=[{k:r[k] for k in QUOTE_FIELDS} for r in original['models']['full']['forecasts']
        if r['observed']<=now<r['kickoff']]
    for q in quotes:
        if not q['featureCutoff']<=q['observed']-86400:raise ValueError('Feature lag violated')
        q['quoteAgeHoursAtFreeze']=(now-q['observed'])/3600
    quotes.sort(key=lambda r:(r['featureCutoff'],r['fixture'],str(r['spec']),r['bookmaker']))
    triplets=defaultdict(dict)
    for q in quotes:
        if tuple(q['spec'][:2])==('GOALS_FT','RESULT'):
            triplets[q['fixture'],q['bookmaker'],q['sourceReceipt']][q['spec'][2]]=q['odd']
    model_choices={('full','joint')}|{(v['candidate'],v['mode']) for v in fold['choices'].values()}
    engines={};states={}
    for name,mode in sorted(model_choices):
        directory=location(args,name);pp=directory/(name+'_2026-10-01_parameters.json')
        sp=directory/(name+'_2026-10-01_pretest_state.json.gz');profile=profile_mode(json.loads(pp.read_text()),mode)
        engines[name,mode]=(QuoteModel(name,history,directory,profile),profile)
        states[name+'::'+mode]=dict(stateSha256=hashlib.sha256(sp.read_bytes()).hexdigest(),
            sourceProfileSha256=hashlib.sha256(pp.read_bytes()).hexdigest(),effectiveProfile=profile)
    models={name:dict(forecasts=[],options=[]) for name in ('full','prior_window_selected')}
    metric_group={m:g for g,metrics in GROUPS.items() for m in metrics}
    for q in quotes:
        for engine,_ in engines.values():engine.advance(q['featureCutoff'])
        goal_choice=fold['choices']['GOALS'];goal_engine=engines[goal_choice['candidate'],goal_choice['mode']][0]
        selected_full=goal_engine.predict(q,'GOALS_FT')
        for label in models:
            choice=dict(candidate='full',mode='joint') if label=='full' else fold['choices'][metric_group[q['spec'][0]]]
            engine,profile=engines[choice['candidate'],choice['mode']];pred=engine.predict(q,q['spec'][0])
            if pred is None:continue
            row,prepared,full=pred
            if label=='prior_window_selected':
                if selected_full is None:continue
                pred=row,prepared,selected_full[2]
            result=forecast(row,profile,q['spec'],prepared)
            models[label]['forecasts'].append(dict(q,winProbability=result['winProbability'],
                pushProbability=result['pushProbability'],homeMean=result['homeMean'],awayMean=result['awayMean'],
                selectedCandidate=choice['candidate'],calibrationMode=choice['mode'],status='UNSCORED'))
            prices=triplets[q['fixture'],q['bookmaker'],q['sourceReceipt']];fav=None
            if set(prices)=={'HOME','DRAW','AWAY'}:
                side=min(prices,key=prices.get)
                if side!='DRAW' and prices[side]<=1.8:fav=side
            pick=assess(q,pred,profile,fav,'current_preference')
            if pick is not None:models[label]['options'].append(pick)
    for data in models.values():data['selected']=select(data.pop('options'))
    original_index={(r['fixture'],r['bookmaker'],tuple(r['spec'])):r for r in original['models']['full']['forecasts']}
    max_difference=max((abs(r['winProbability']-original_index[r['fixture'],r['bookmaker'],tuple(r['spec'])]['winProbability'])
        for r in models['full']['forecasts']),default=0.)
    if max_difference>1e-12:raise ValueError('Full baseline failed exact round-trip')
    stamp=datetime.fromtimestamp(now,timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target=args.output/('selected_forward_frozen_'+stamp+'.json.gz')
    frozen=dict(frozenAtUTC=datetime.fromtimestamp(now,timezone.utc).isoformat(),models=models,states=states,
        selectionChoices=fold['choices'],selectionSha256=hashlib.sha256(selection_path.read_bytes()).hexdigest(),
        sourceBatchSha256=source_sha,firstProspectiveBatch=False,originalBatchModified=False,
        overlapsFirstProspectiveBatch=True,
        futureOutcomesRead=False,allKickoffsAfterFreeze=True,providerCalls=0,
        rules=dict(selection='current_preference, unchanged thresholds; one opinion per fixture/family',
            favoriteGoalDistribution='Selected coherent goal model shared across families',
            quotes='Same archived single-receipt quote inputs as first batch, filtered to still-future kickoffs',
            executionEvidence=False,featureLag='at least 24h before quote'),
        baselineRoundTripMaxDifference=max_difference)
    zipped(target,frozen)
    overview={name:dict(forecasts=len(data['forecasts']),fixtures=len({r['fixture'] for r in data['forecasts']}),
        selected=len(data['selected'])) for name,data in models.items()}
    write(args.output/'regularization_forward_inventory.json',dict(path=target.name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),frozenAtUTC=frozen['frozenAtUTC'],
        sourceBatchSha256=source_sha,overview=overview,choices=fold['choices'],status='UNSCORED',
        noAutonomousScoringJob=True,executionEvidence=False,baselineRoundTripMaxDifference=max_difference))
    print('SELECTED_FORWARD_FROZEN',json.dumps(overview),flush=True)

if __name__=='__main__':main()
