"""Prior-outcome calibrated statistical/market probability pools.

Complete mutually exclusive partitions only. Prefer other bookmakers; a
same-book reference is explicitly labelled, never called independent evidence.
Weights minimize prior fixture-weighted log loss, never betting profit.
"""
from collections import Counter,defaultdict
import math
import re
import numpy as np
from scipy.optimize import brentq

MIN_FIXTURES=30
PENALTY=.002
SIDES={'RESULT':('HOME','DRAW','AWAY'),'TOTAL':('OVER','UNDER'),
       'TEAM':('OVER','UNDER'),'BTTS':('YES','NO')}


def book_identity(book):
    return re.sub(r'[^a-z0-9]','',re.sub(r'\([^)]*\)','',str(book)).casefold())


def partition(spec):
    metric,scope,side,team,line=spec
    if scope not in SIDES or side not in SIDES[scope]:return None
    return metric,scope,team,line


def references(rows):
    groups=defaultdict(lambda:defaultdict(list))
    for r in rows:
        key=partition(r['spec'])
        if key is None:continue
        packet=r['fixture'],r['sourceReceipt'],key
        groups[packet][r['bookmaker']].append(r)
    complete={};audit=Counter()
    for packet,books in groups.items():
        choices={}
        sides=SIDES[packet[2][1]]
        for book,items in books.items():
            byside=defaultdict(set)
            for r in items:byside[r['spec'][2]].add(float(r['odd']))
            if set(byside)!=set(sides) or any(len(v)!=1 for v in byside.values()):
                audit['incompleteOrConflictingPartitions']+=1;continue
            implied=np.asarray([1./next(iter(byside[s])) for s in sides])
            total=float(implied.sum())
            if not np.isfinite(implied).all() or not .95<=total<=1.25:
                audit['implausiblePartitionMargin']+=1;continue
            choices[book]=dict(zip(sides,(implied/total).tolist()))
        complete[packet]=choices
    result={}
    for i,r in enumerate(rows):
        metric,scope,side,team,line=r['spec']
        target=partition(r['spec'])
        if scope in ('DC','DNB'):target=metric,'RESULT','',None
        if target is None:continue
        choices=complete.get((r['fixture'],r['sourceReceipt'],target),{})
        others={b:p for b,p in choices.items() if book_identity(b)!=book_identity(r['bookmaker'])}
        selected=others or {b:p for b,p in choices.items() if b==r['bookmaker']}
        if not selected:continue
        sides=SIDES[target[1]]
        vector=np.median([[p[s] for s in sides] for p in selected.values()],axis=0)
        vector=vector/vector.sum();p=dict(zip(sides,vector.tolist()))
        if scope=='DC':
            q={'HOME_OR_DRAW':p['HOME']+p['DRAW'],'AWAY_OR_DRAW':p['AWAY']+p['DRAW'],
               'HOME_OR_AWAY':p['HOME']+p['AWAY']}[side]
        elif scope=='DNB':q=p[side]/(1-p['DRAW'])
        else:q=p[side]
        result[i]=dict(q=q,independent=bool(others),books=sorted(selected))
        audit['independentReference' if others else 'sameBookBenchmark']+=1
    return result,dict(audit)


def fit_weight(records,cutoff,week_weights=None):
    if any(r['kickoff']>=cutoff for r in records):raise ValueError('Calibration crosses prediction cutoff')
    # One fixture gets total weight one regardless of books, lines or sides.
    counts=Counter(r['fixture'] for r in records);n=len(counts)
    metadata=dict(fixtures=n,events=len(records),trainingBefore=cutoff,penalty=PENALTY,
        objective='Prior fixture-weighted binary log loss',roiUsed=False)
    if n<MIN_FIXTURES:return dict(metadata,weight=0.,fitted=False,reason='INSUFFICIENT_PRIOR_FIXTURES_USE_MARKET_REFERENCE')
    weights=np.asarray([(week_weights or {}).get(int(r['kickoff']//604800),1.)/counts[r['fixture']] for r in records]);weights/=weights.sum()
    # Pushes were excluded by the caller, so these are conditional win events.
    ps=np.clip([r['statConditional'] for r in records],1e-12,1-1e-12)
    pm=np.clip([r['marketConditional'] for r in records],1e-12,1-1e-12)
    y=np.asarray([r['y'] for r in records]);delta=ps-pm
    def gradient(w):
        p=np.clip(pm+w*delta,1e-12,1-1e-12)
        return float(weights@((p-y)*delta/(p*(1-p)))+2*PENALTY*w)
    w=0. if gradient(0.)>=0 else 1. if gradient(1.)<=0 else float(brentq(gradient,0.,1.))
    return dict(metadata,weight=w,fitted=True,reason='FITTED_PRIOR_LOG_LOSS',
        maxTrainingKickoff=max(r['kickoff'] for r in records))


def pooled_probability(stat_win,stat_push,market_conditional,weight):
    if not all(math.isfinite(v) for v in (stat_win,stat_push,market_conditional,weight)):
        raise ValueError('Nonfinite probability pool')
    if not 0<=weight<=1 or not 0<=market_conditional<=1 or not 0<=stat_push<1 or not 0<=stat_win<=1-stat_push+1e-12:
        raise ValueError('Invalid probability pool')
    conditional=stat_win/(1-stat_push)
    return float((weight*conditional+(1-weight)*market_conditional)*(1-stat_push))


def select_ev(options):
    """EV and probability rank first; favourite preference is a final tie break."""
    chosen=[];used=set()
    for r in sorted(options,key=lambda r:(-r['ev'],-r['p'],r.get('preference',1),str(r['spec']),r['bookmaker'])):
        metric,scope=r['spec'][:2]
        families={'RESULT'} if metric.startswith('GOALS') and scope in ('RESULT','DC','DNB','EXACT','MARGIN','HTFT') else {'GOALS'} if metric.startswith('GOALS') else {'CARDS'} if 'CARD' in metric else {metric}
        if scope=='RESULT_TOTAL':families={'RESULT','GOALS'}
        keys={(r['fixture'],f) for f in families}
        if keys&used:continue
        used|=keys;chosen.append(r)
    return chosen
