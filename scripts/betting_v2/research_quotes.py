"""Archived-quote replay with a fixed model and pre-quote lagged features.

Historical publication times remain unverified. The 24-hour minimum feature
lag is explicit, and does not turn inspected periods into an untouched test.
"""
import argparse,gzip,hashlib,json,math,sys
from collections import defaultdict,Counter
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.opponent_matchup import timestamp
from betting_v2.index_quote_receipts import extracted_offers
from betting_v2.replay_corrected_prices import mapped_spec
from betting_v2.exact_forecast_adapter import prepare,forecast
from betting_v2.validated_walk_forward import distribution,zipped
from betting_v2.live_exact import restore
from betting_v2.joint_count import outcome
from betting_v2.recovery_walk_forward import write
MODES=('current_preference','probability_ranking','neutral_btts')
VARIANTS=('full','no_xg','no_goal_stack','oof_goal_stack')

def feature_cutoff(observed):return math.floor(observed/86400)*86400-86400

def one_fixture_receipt(quotes,latest=False):
    """A fixture decision must never mix bookmaker packets from different days."""
    chosen={}
    for q in quotes:
        rank=q['observed'],q['sourceReceipt'];previous=chosen.get(q['fixture'])
        if previous is None or (rank>previous if latest else rank<previous):chosen[q['fixture']]=rank
    return [q for q in quotes if q['sourceReceipt']==chosen[q['fixture']][1]]

def collect(history,out):
    bytime=defaultdict(list)
    for r in history:bytime[int(r['_date'])].append(r)
    def identities(s):
        key=direct.name(s);return {a[key] for a in direct.UEFA_ALIASES.values() if key in a}
    snapshots=defaultdict(dict);counts=Counter();hashes=[]
    for path in sorted((direct.ROOT/'reports/betting_v2/quote_receipts').glob('*.gz')):
        b=path.read_bytes();sha=hashlib.sha256(b).hexdigest();hashes.append((path.name,sha));counts['receipts']+=1
        try:doc=json.loads(gzip.decompress(b));offers,_=extracted_offers(doc)
        except (ValueError,KeyError,TypeError,OSError):counts['invalidReceipt']+=1;continue
        events={str(v['event'].get('id') or v['event'].get('eventId')):v['event'] for v in doc['boundedMarketSnapshot']}
        for offer in offers:
            ko=timestamp(offer['kickoffUTC']);obs=timestamp(offer['clientReceivedAtUTC'])
            if not timestamp('2026-10-01')<=ko<timestamp('2026-10-15'):continue
            counts['periodOffers']+=1
            if not 0<obs<ko:counts['notPrematch']+=1;continue
            cutoff=feature_cutoff(obs)
            if cutoff<timestamp('2026-10-01'):counts['beforeFrozenState']+=1;continue
            event=events[offer['providerEventId']];homes,aways=identities(event.get('home')),identities(event.get('away'))
            matches=[r for r in bytime[int(ko)] if r['_home'] in homes and r['_away'] in aways]
            if len(matches)!=1:counts['noUniqueExactJoin']+=1;continue
            r=matches[0];fixture=f"{r['_date']}:{r['_home']}:{r['_away']}";spec=mapped_spec(offer,event)
            if spec is None:counts['unsupportedSettlement']+=1;continue
            key=fixture,offer['bookmaker'],path.name
            item=dict(fixture=fixture,bookmaker=offer['bookmaker'],spec=spec,odd=offer['odd'],observed=obs,
                kickoff=ko,featureCutoff=cutoff,sourceReceipt=path.name,sourceSha256=sha,providerEventId=offer['providerEventId'])
            prev=snapshots[key].get(tuple(spec))
            if prev is not None and (prev.get('ambiguous') or prev['odd']!=item['odd']):item['ambiguous']=True
            snapshots[key][tuple(spec)]=item
    first={}
    for (fixture,book,receipt),items in snapshots.items():
        items=[r for r in items.values() if not r.get('ambiguous')]
        if not items:continue
        key=fixture,book;rank=min(r['observed'] for r in items),receipt
        if key not in first or rank<first[key][0]:first[key]=rank,items
    quotes=one_fixture_receipt([r for _,items in first.values() for r in items])
    zipped(out/'expanded_quotes.json.gz',quotes)
    write(out/'quote_inventory.json',dict(counters=dict(counts),uniqueQuotes=len(quotes),fixtures=len({q['fixture'] for q in quotes}),
        receiptSha256=hashlib.sha256(json.dumps(hashes).encode()).hexdigest(),providerCalls=0,publicationTimesVerified=False,
        rule='One earliest prematch receipt per fixture across all bookmakers; feature observations at least 24h before receipt'))
    return quotes

class QuoteModel:
    def __init__(self,variant,history,directory,profile):
        self.history=history;self.profile=profile
        state=json.loads(gzip.decompress((directory/(variant+'_2026-10-01_pretest_state.json.gz')).read_bytes()))
        self.model=restore(state,history,timestamp('2026-09-01'),direct.metric_values)
        self.model.feature_exclusions=frozenset(('xg',)) if variant=='no_xg' else frozenset()
        self.model.use_goal_strength=variant!='no_goal_stack'
        self.clock=timestamp('2026-10-01');self.cache={}
    def advance(self,cutoff):
        if cutoff<self.clock:raise ValueError('Feature clock moved backwards')
        if cutoff>self.clock:
            for r in self.history:
                if self.clock<=r['_date']<cutoff:self.model.observe(r,direct.metric_values)
            self.clock=cutoff;self.cache={}
    def predict(self,r,metric):
        h,a=r['_home'],r['_away'];key=h,a,metric
        if key not in self.cache:
            pred=self.model.predict(h,a,metric);full=self.model.predict(h,a,'GOALS_FT')
            if pred is None or full is None:self.cache[key]=None
            else:
                regime='XG' if len(self.model.xg[h])>=8 and len(self.model.xg[a])>=8 else 'ELO_NO_XG'
                row=dict(metric=metric,regime=regime,means=list(pred[:2]))
                if metric in ('GOALS_1H','GOALS_2H'):row.update(fullMeans=list(full[:2]),shares=[pred[i]/full[i] for i in (0,1)])
                full_dist=distribution(dict(metric='GOALS_FT',regime=regime,means=list(full[:2])),self.profile)
                self.cache[key]=row,prepare(row,self.profile),full_dist
        return self.cache[key]

def assess(q,pred,profile,fav,mode):
    row,prepared,full=pred;r=forecast(row,profile,q['spec'],prepared)
    p,push=r['winProbability'],r['pushProbability'];ev=p*q['odd']+push-1
    if not 1.8<q['odd']<3 or p<.6 or ev<=0:return None
    metric,scope,side,team,_=q['spec'];support=None;preference=1
    if fav:
        other='AWAY' if fav=='HOME' else 'HOME'
        if full.evaluate(('GOALS_FT','RESULT',fav,'',None))[0]<=full.evaluate(('GOALS_FT','RESULT',other,'',None))[0]:return None
        attack=metric.startswith('GOALS') or metric in ('CORNERS','SHOTS','SOT')
        if attack:
            h,a=r['homeMean'],r['awayMean']
            if (h<=a if fav=='HOME' else a<=h):return None
        support=r['scenarios'][fav]['winProbability']
        if attack and (support is None or support<.6):return None
        if mode!='probability_ranking':
            if attack and team:preference=0 if (team==fav and side=='OVER') or (team!=fav and side=='UNDER') else 2
            elif scope in ('RESULT','DC','DNB'):
                good=(fav,'HOME_OR_DRAW' if fav=='HOME' else 'AWAY_OR_DRAW','HOME_OR_AWAY');preference=0 if side in good else 2
            elif scope=='BTTS' and mode=='current_preference':preference=0 if side=='NO' else 2
    confidence=min(p,support) if support is not None else p
    tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING'
    return dict(q,p=p,push=push,ev=ev,confidence=confidence,preference=preference,tier=tier)

def select(options):
    best={}
    for r in options:
        metric,scope=r['spec'][:2]
        family='RESULT' if metric.startswith('GOALS') and scope in ('RESULT','DC','DNB') else 'GOALS' if metric.startswith('GOALS') else metric
        key=r['fixture'],family;rank=r['preference'],-r['confidence'],-r['p'],-r['ev'],str(r['spec']),r['bookmaker']
        if key not in best or rank<best[key][0]:best[key]=rank,r
    return [r for _,r in best.values()]

def stats(rows):
    if not rows:return dict(bets=0,fixtures=0)
    clusters=defaultdict(list)
    for r in rows:clusters[r['fixture']].append(r['y']*r['odd']+r['actualPush']-1)
    groups=list(clusters.values());profits=np.array([p for g in groups for p in g]);boot=[]
    if len(groups)>=30:
        rng=np.random.default_rng(15102026)
        for _ in range(2000):boot.append(np.mean([p for i in rng.integers(len(groups),size=len(groups)) for p in groups[i]]))
    return dict(bets=len(rows),fixtures=len(groups),wins=sum(r['y'] for r in rows),pushes=sum(r['actualPush'] for r in rows),
        profitUnits=float(profits.sum()),roi=float(profits.mean()),roi95=list(map(float,np.quantile(boot,[.025,.975]))) if boot else None,
        meanPredicted=float(np.mean([r['p'] for r in rows])),hitRate=float(np.mean([r['y'] for r in rows])))

def replay(variant,quotes,history,directory,out):
    actual={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};prices=defaultdict(dict)
    for q in quotes:
        if tuple(q['spec'][:2])==('GOALS_FT','RESULT'):prices[q['fixture'],q['bookmaker'],q['sourceReceipt']][q['spec'][2]]=q['odd']
    fitted=json.loads((directory/(variant+'_2026-10-01_parameters.json')).read_text());reports={}
    for calibration,profile in (('raw',{}),('calibrated',fitted)):
        model=QuoteModel(variant,history,directory,profile);options={m:[] for m in MODES}
        for q in sorted(quotes,key=lambda q:(q['featureCutoff'],q['observed'],q['fixture'],str(q['spec']))):
            model.advance(q['featureCutoff']);r=actual[q['fixture']];metric=q['spec'][0]
            pred=model.predict(r,metric);label=direct.metric_values(r,metric)
            if pred is None or label is None:continue
            triplet=prices[q['fixture'],q['bookmaker'],q['sourceReceipt']];fav=None
            if set(triplet)=={'HOME','DRAW','AWAY'}:
                side=min(triplet,key=triplet.get)
                if side!='DRAW' and triplet[side]<=1.8:fav=side
            for mode in MODES:
                pick=assess(q,pred,profile,fav,mode)
                if pick is not None:
                    y,push=outcome(*label,q['spec']);pick.update(y=y,actualPush=push);options[mode].append(pick)
        for mode,candidates in options.items():
            picks=select(candidates);name=variant+'::'+calibration+'::'+mode
            markets=sorted({r['spec'][0]+'::'+r['spec'][1] for r in picks})
            reports[name]=dict(overall=stats(picks),byTier={t:stats([r for r in picks if r['tier']==t]) for t in ('HIGH','CONFIRMED','DEVELOPING')},
                byMarket={m:stats([r for r in picks if r['spec'][0]+'::'+r['spec'][1]==m]) for m in markets},picks=picks)
            print('REPLAY',name,json.dumps(reports[name]['overall']),flush=True)
    write(out/(variant+'_expanded_replay.json'),dict(reports=reports,untouchedHoldout=False,providerCalls=0,
        publicationTimesVerified=False,policyChoicesPredeclared=list(MODES),promotionAllowed=False))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--baseline',type=Path,required=True)
    ap.add_argument('--variants',nargs='+',choices=VARIANTS,default=list(VARIANTS));args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    history=direct.load_actual_results(timestamp('2026-10-15'))['GLOBAL'];quotes=collect(history,args.output)
    print('QUOTES',len(quotes),'fixtures',len({q['fixture'] for q in quotes}),flush=True)
    for variant in args.variants:replay(variant,quotes,history,args.output if variant=='oof_goal_stack' else args.baseline,args.output)
if __name__=='__main__':main()
