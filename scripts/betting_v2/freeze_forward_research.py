"""Freeze predictions for still-unplayed fixtures; no future outcome is read.

This creates a fixed first prospective batch, not a scheduled automation and
not evidence that an archived price is executable now.
"""
import argparse,gzip,hashlib,json,sqlite3,sys,zipfile
from collections import defaultdict,Counter
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.opponent_matchup import timestamp
from betting_v2.index_quote_receipts import extracted_offers
from betting_v2.replay_corrected_prices import mapped_spec
from betting_v2.research_quotes import QuoteModel,feature_cutoff,assess,select,one_fixture_receipt
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.validated_walk_forward import zipped
from betting_v2.recovery_walk_forward import write

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--baseline',type=Path,required=True)
    args=ap.parse_args();out=args.output;now=datetime.now(timezone.utc).timestamp()
    history=direct.load_actual_results(timestamp('2026-10-15'))['GLOBAL']
    # Same immutable real catalog used for the integration dry-run; only its
    # future fixture identities are read, never results after the training cutoff.
    manifest=json.loads((direct.ROOT/'data/statmaker/app_ready/update_manifest.json').read_text())
    bundle=next(v for v in manifest['artifacts'] if v['id']=='app_ready_betting_bundle')
    db=out/'forward_catalog.tmp'
    with zipfile.ZipFile(direct.ROOT/bundle['path']) as z:db.write_bytes(z.read('databases/statmaker_prepared_betting.db'))
    catalog=defaultdict(list)
    with sqlite3.connect(db) as con:
        con.row_factory=sqlite3.Row
        for r in con.execute('SELECT * FROM prepared_fixture_matches'):
            if direct.kickoff(r['kickoff'])>now:catalog[str(r['id'])].append(dict(r))
    db.unlink();snapshots=defaultdict(dict);counts=Counter()
    for path in sorted((direct.ROOT/'reports/betting_v2/quote_receipts').glob('*.gz')):
        payload=path.read_bytes();sha=hashlib.sha256(payload).hexdigest()
        try:doc=json.loads(gzip.decompress(payload));offers,_=extracted_offers(doc)
        except (ValueError,KeyError,TypeError,OSError):continue
        events={str(v['event'].get('id') or v['event'].get('eventId')):v['event'] for v in doc['boundedMarketSnapshot']}
        for offer in offers:
            ko=timestamp(offer['kickoffUTC']);obs=timestamp(offer['clientReceivedAtUTC'])
            if not 0<obs<=now<ko:continue
            choices=[r for r in catalog[offer['providerEventId']] if direct.kickoff(r['kickoff'])==ko]
            if len(choices)!=1:counts['unresolvedCatalogIdAndKickoff']+=1;continue
            r=choices[0];event=events[offer['providerEventId']]
            aliases=direct.UEFA_ALIASES.get(direct.league_code(r['league_code']),{})
            def identity(keys):
                return next((aliases[direct.name(r[k])] for k in keys if direct.name(r[k]) in aliases),None)
            h=identity(('canonical_home_team','home_team','provider_home_team'));a=identity(('canonical_away_team','away_team','provider_away_team'))
            if h is None or a is None:counts['unresolvedHistoricalTeam']+=1;continue
            if aliases.get(direct.name(event.get('home')))!=h or aliases.get(direct.name(event.get('away')))!=a:
                counts['unresolvedReceiptTeam']+=1;continue
            spec=mapped_spec(offer,event)
            if spec is None:continue
            fixture=f'{ko}:{h}:{a}';q=dict(fixture=fixture,bookmaker=offer['bookmaker'],spec=spec,odd=offer['odd'],
                observed=obs,kickoff=ko,featureCutoff=feature_cutoff(obs),sourceReceipt=path.name,sourceSha256=sha,
                providerEventId=offer['providerEventId'],_home=h,_away=a,quoteAgeHoursAtFreeze=(now-obs)/3600)
            key=fixture,q['bookmaker'],path.name;previous=snapshots[key].get(tuple(spec))
            if previous is not None and (previous.get('ambiguous') or previous['odd']!=q['odd']):q['ambiguous']=True
            snapshots[key][tuple(spec)]=q
    latest={}
    for (fixture,book,receipt),offers in snapshots.items():
        offers=[q for q in offers.values() if not q.get('ambiguous')]
        if not offers:continue
        key=fixture,book;rank=max(q['observed'] for q in offers),receipt
        if key not in latest or rank>latest[key][0]:latest[key]=rank,offers
    quotes=one_fixture_receipt([q for _,offers in latest.values() for q in offers],latest=True);triplets=defaultdict(dict)
    for q in quotes:
        if tuple(q['spec'][:2])==('GOALS_FT','RESULT'):triplets[q['fixture'],q['bookmaker'],q['sourceReceipt']][q['spec'][2]]=q['odd']
    frozen={};states={}
    for variant,directory in (('full',args.baseline),('oof_goal_stack',out)):
        statepath=directory/(variant+'_2026-10-01_pretest_state.json.gz');pp=directory/(variant+'_2026-10-01_parameters.json')
        profile=json.loads(pp.read_text());model=QuoteModel(variant,history,directory,profile);forecasts=[];options=[]
        for q in sorted(quotes,key=lambda q:(q['featureCutoff'],q['fixture'],str(q['spec']))):
            model.advance(q['featureCutoff']);pred=model.predict(q,q['spec'][0])
            if pred is None:continue
            row,prepared,full=pred;f=forecast(row,profile,q['spec'],prepared)
            forecasts.append(dict(q,winProbability=f['winProbability'],pushProbability=f['pushProbability'],
                homeMean=f['homeMean'],awayMean=f['awayMean'],status='UNSCORED'))
            prices=triplets[q['fixture'],q['bookmaker'],q['sourceReceipt']];fav=None
            if set(prices)=={'HOME','DRAW','AWAY'}:
                side=min(prices,key=prices.get)
                if side!='DRAW' and prices[side]<=1.8:fav=side
            pick=assess(q,pred,profile,fav,'current_preference')
            if pick is not None:options.append(pick)
        frozen[variant]=dict(forecasts=forecasts,selected=select(options));states[variant]=dict(
            stateSha256=hashlib.sha256(statepath.read_bytes()).hexdigest(),profileSha256=hashlib.sha256(pp.read_bytes()).hexdigest())
    stamp=datetime.fromtimestamp(now,timezone.utc).strftime('%Y%m%dT%H%M%SZ');target=out/('forward_frozen_'+stamp+'.json.gz')
    data=dict(frozenAtUTC=datetime.fromtimestamp(now,timezone.utc).isoformat(),firstProspectiveBatch=True,
        futureOutcomesRead=False,allKickoffsAfterFreeze=True,providerCalls=0,states=states,models=frozen,
        rules=dict(selection='current_preference, unchanged thresholds; one opinion per fixture/family',
            prices='latest eligible single archived receipt per fixture across all bookmakers',featureLag='at least 24h before quote',
            coefficients='frozen; no test-dependent refit or calibration',executionEvidence=False),counters=dict(counts))
    zipped(target,data)
    overview={v:dict(forecasts=len(d['forecasts']),fixtures=len({r['fixture'] for r in d['forecasts']}),selected=len(d['selected'])) for v,d in frozen.items()}
    write(out/'forward_freeze_inventory.json',dict(path=target.name,sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        frozenAtUTC=data['frozenAtUTC'],overview=overview,counters=dict(counts),status='UNSCORED',
        note='Immutable predictions for future fixtures; no autonomous future scoring job was created'))
    print('FORWARD_FROZEN',json.dumps(overview),flush=True)
if __name__=='__main__':main()
