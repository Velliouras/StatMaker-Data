"""Prematch goal-price anchors for count models, excluding corner prices/labels."""
from collections import Counter, defaultdict
from datetime import datetime
import math
import numpy as np
from scipy.optimize import brentq
from scipy.stats import poisson, skellam
from betting_v2.market_calibration import book_identity

MAX_AGE=86400

def utc(value):
    if isinstance(value,(float,int)):return float(value)
    try:
        d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return d.timestamp() if d.tzinfo is not None else None
    except (ValueError,TypeError,OverflowError):return None

def valid_quote(q):
    times=[utc(q.get(k)) for k in ('marketUpdatedAtUTC','observed','kickoff','featureCutoff')]
    if any(t is None or not math.isfinite(t) for t in times):return 'missingClock'
    updated,observed,kickoff,cutoff=times
    if not updated<=observed<kickoff or observed-updated>MAX_AGE:return 'futureOrStaleMarket'
    if cutoff>observed-86400 or cutoff%86400:return 'invalidFeatureCutoff'
    try:
        if not math.isfinite(q['odd']) or q['odd']<=1:return 'invalidOdd'
    except (KeyError,TypeError):return 'invalidOdd'
    if not all(q.get(k) for k in ('fixture','providerEventId','sourceReceipt','bookmaker')):return 'missingIdentity'
    return None

def no_vig(rows,sides):
    by=defaultdict(list)
    for q in rows:by[q['spec'][2]].append(q)
    if set(by)!=set(sides) or any(len({q['odd'] for q in v})!=1 for v in by.values()):return None
    chosen=[min(by[s],key=lambda q:(utc(q['marketUpdatedAtUTC']),str(q['spec']))) for s in sides]
    implied=np.asarray([1/q['odd'] for q in chosen]);margin=float(implied.sum())
    if not .95<=margin<=1.25:return None
    return dict(zip(sides,(implied/margin).tolist())),chosen

def goal_rates(result,over,line):
    """Invert O/U and conditional HOME-v-AWAY probability; retain draw mismatch."""
    if not(math.isfinite(line) and line>0 and line*2==int(line*2)):raise ValueError('Only positive integer and half goal lines')
    if not 0<over<1 or set(result)!={'HOME','DRAW','AWAY'}:raise ValueError('Complete interior goal probabilities required')
    if any(not math.isfinite(v) or v<=0 for v in result.values()) or abs(sum(result.values())-1)>1e-8:raise ValueError('Invalid 1X2 vector')
    integer=int(line)==line
    def total_probability(mean):
        push=poisson.pmf(int(line),mean) if integer else 0.
        return float(poisson.sf(math.floor(line),mean)/(1-push))
    total=brentq(lambda t:total_probability(t)-over,.05,12.,xtol=1e-10)
    target=result['HOME']/(result['HOME']+result['AWAY'])
    def winner_share(s):
        h,a=total*s,total*(1-s);home=skellam.sf(0,h,a);away=skellam.cdf(-1,h,a)
        return float(home/(home+away))
    share=brentq(lambda s:winner_share(s)-target,.001,.999,xtol=1e-10)
    home,away=total*share,total*(1-share)
    return dict(home=home,away=away,total=total,drawResidual=float(result['DRAW']-skellam.pmf(0,home,away)))

def anchors(quotes):
    """One whole-receipt anchor per fixture; aliases contribute only once.

    Select a complete same-book 1X2 and the most balanced complete O/U line.
    Per real bookmaker prefer freshest complete variant, plain name on ties.
    Across real books take median log intensities. No outcome or corner price.
    """
    grouped=defaultdict(lambda:defaultdict(list));audit=Counter();clocks=defaultdict(set)
    for q in quotes:
        if q['spec'][0]!='GOALS_FT' or q['spec'][1] not in ('RESULT','TOTAL'):continue
        reason=valid_quote(q)
        if reason:audit[reason]+=1;continue
        clocks[q['fixture']].add((str(q['providerEventId']),q['sourceReceipt'],q['observed'],q['kickoff'],q['featureCutoff']))
        grouped[q['fixture']][q['bookmaker']].append(q)
    result={}
    for fixture,books in grouped.items():
        if len(clocks[fixture])!=1:audit['mixedFixtureReceiptOrIdentity']+=1;continue
        choices={}
        for book,rows in books.items():
            match=no_vig([q for q in rows if q['spec'][1]=='RESULT'],('HOME','DRAW','AWAY'))
            if match is None:audit['incompleteResult']+=1;continue
            totals=defaultdict(list)
            for q in rows:
                if q['spec'][1]=='TOTAL' and not q['spec'][3]:totals[q['spec'][4]].append(q)
            valid=[]
            for line,items in totals.items():
                if not isinstance(line,(float,int)) or line<=0 or line*2!=int(line*2):continue
                pair=no_vig(items,('OVER','UNDER'))
                if pair:valid.append((abs(pair[0]['OVER']-.5),abs(line-2.5),line,pair))
            if not valid:audit['incompleteTotal']+=1;continue
            _,_,line,pair=min(valid,key=lambda p:p[:3])
            try:rates=goal_rates(match[0],pair[0]['OVER'],float(line))
            except (ValueError,FloatingPointError):audit['noInteriorGoalSolution']+=1;continue
            used=match[1]+pair[1];times=[utc(q['marketUpdatedAtUTC']) for q in used]
            rates.update(bookmaker=book,line=line,marketUpdatedMin=min(times),marketUpdatedMax=max(times),
                         resultProbabilities=match[0],overProbability=pair[0]['OVER'])
            identity=book_identity(book);rank=(min(times),'(' not in book,book)
            if identity not in choices or rank>choices[identity][0]:choices[identity]=rank,rates
        if not choices:audit['fixturesWithoutAnchor']+=1;continue
        values=[value for _,value in sorted(choices.values(),key=lambda p:p[1]['bookmaker'])]
        home=float(np.exp(np.median(np.log([v['home'] for v in values]))));away=float(np.exp(np.median(np.log([v['away'] for v in values]))))
        event,receipt,observed,kickoff,cutoff=next(iter(clocks[fixture]))
        result[fixture]=dict(fixture=fixture,providerEventId=event,sourceReceipt=receipt,observed=observed,kickoff=kickoff,
            featureCutoff=cutoff,home=home,away=away,total=home+away,drawResidual=float(np.median([v['drawResidual'] for v in values])),
            books=values,realBookmakers=len(values),maxMarketAgeSeconds=max(observed-v['marketUpdatedMin'] for v in values),
            interpretation='Independent-Poisson rates implied by contemporaneous goal prices, not observed xG')
        audit['anchoredFixtures']+=1;audit['multiBookFixtures' if len(values)>1 else 'singleBookFixtures']+=1
    return result,dict(audit)
