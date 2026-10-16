"""Score an immutable prospective batch when completed-match labels arrive.

Reads saved probabilities only: no fitting, calibration or threshold changes.
Missing results remain unscored and never enter the win/loss denominator.
"""
import argparse,gzip,hashlib,json,sys
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.joint_count import outcome
from betting_v2.research_app_markets import outcome_extended,PERIOD_SCOPES
from betting_v2.research_quotes import stats
from betting_v2.recovery_walk_forward import summary,write

def settled(rows,actual,freeze_time):
    scored=[];missing=0
    for r in rows:
        if not r['observed']<=freeze_time<r['kickoff']:raise ValueError('Invalid prospective timing')
        truth=actual.get(r['fixture'])
        labels=direct.metric_values(truth,r['spec'][0]) if truth is not None else None
        if labels is None:missing+=1;continue
        first=direct.metric_values(truth,'GOALS_1H') if r['spec'][1] in PERIOD_SCOPES else None
        if r['spec'][1] in PERIOD_SCOPES and first is None:missing+=1;continue
        y,push=outcome_extended(*labels,r['spec'],first=first);scored.append(dict(r,y=y,actualPush=push))
    return scored,missing

def paired_comparisons(events,baseline='full'):
    result={};base=events.get(baseline,{})
    for variant,rows in events.items():
        if variant==baseline:continue
        paired=[]
        for key,c in rows.items():
            b=base.get(key)
            if b is None:continue
            if b['y']!=c['y']:raise ValueError('Paired label mismatch')
            paired.append(dict(fixture=c['fixture'],p=b['winProbability']/(1-b['pushProbability']),
                q=c['winProbability']/(1-c['pushProbability']),y=c['y']))
        result[variant]=summary(paired) if paired else None
    return result

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--frozen',type=Path,required=True)
    ap.add_argument('--expected-sha256',required=True);ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    payload=args.frozen.read_bytes()
    if hashlib.sha256(payload).hexdigest()!=args.expected_sha256:raise ValueError('Frozen batch hash mismatch')
    doc=json.loads(gzip.decompress(payload));freeze=datetime.fromisoformat(doc['frozenAtUTC']).timestamp()
    now=datetime.now(timezone.utc).timestamp();history=direct.load_actual_results(now)['GLOBAL']
    actual={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};models={};events={}
    for variant,data in doc['models'].items():
        picks,missing=settled(data['selected'],actual,freeze);rows,missing_rows=settled(data['forecasts'],actual,freeze)
        models[variant]=dict(selected=stats(picks),selectedUnscored=missing,forecastScored=len(rows),forecastUnscored=missing_rows,
            byTier={t:stats([r for r in picks if r['tier']==t]) for t in ('HIGH','CONFIRMED','DEVELOPING')})
        events[variant]={(r['fixture'],r['bookmaker'],tuple(r['spec'])):r for r in rows if not r['actualPush']}
    comparisons=paired_comparisons(events)
    write(args.output,dict(frozenSha256=args.expected_sha256,scoredAtUTC=datetime.now(timezone.utc).isoformat(),
        models=models,pairedScores=comparisons.get('oof_goal_stack'),pairedScoresByModel=comparisons,providerCalls=0,
        refits=0,calibrationUpdates=0,thresholdUpdates=0,executionEvidence=False))
    print(json.dumps(models),flush=True)
if __name__=='__main__':main()
