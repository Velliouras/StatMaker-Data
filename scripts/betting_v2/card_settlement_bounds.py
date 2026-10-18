"""Conservative event-derived card count intervals; no betting selections."""
from collections import Counter
from betting_v2.raw_contract_recovery import card_readiness

ALLOW_INTERVAL={'unverifiedCardPeriodOrFinalWhistle','cardAtSubstitutionMinute'}

def count_bounds(response):
    r=card_readiness(response);flags=set(r['flags'])
    if not flags<=ALLOW_INTERVAL:return dict(usable=False,blockedFlags=sorted(flags-ALLOW_INTERVAL),readiness=r)
    teamids=[response['teams'][s]['id'] for s in ('home','away')];outgoing={}
    for e in response['events']:
        if str(e.get('type','')).casefold()=='subst':outgoing[(e['team']['id'],e['player']['id'])]=e['time']['elapsed']
    uncertain=Counter()
    for e in response['events']:
        if e.get('type')!='Card':continue
        minute=(e.get('time') or {}).get('elapsed');tid=(e.get('team') or {}).get('id');pid=(e.get('player') or {}).get('id')
        if not isinstance(minute,(int,float)) or isinstance(minute,bool) or not 0<=minute<90 or outgoing.get((tid,pid))==minute:uncertain[tid]+=1
    lower=list(r['provisionalUnits']);upper=[v+2*uncertain[t] for v,t in zip(lower,teamids)]
    # Two is a deliberately broad maximum extra contribution for each uncertain
    # card event, including a possible second-yellow dismissal. Bounds may be loose.
    return dict(usable=True,lower=lower,upper=upper,exact=lower==upper,uncertainEvents=[uncertain[t] for t in teamids],readiness=r,
        limitation='Conditional on complete cached card-event and lineup evidence; these are count bounds, not a bookmaker payout certificate.')

def settlement_bounds(spec,odd,bounds):
    if not bounds.get('usable'):return dict(graded=False,reason='UNRESOLVED_EVENT_OR_APPEARANCE_COVERAGE')
    metric,scope,side,team,line=spec
    if metric!='BET365_CARD_UNITS' or scope not in ('TOTAL','TEAM') or side not in ('OVER','UNDER') or (scope=='TEAM' and team not in ('HOME','AWAY')):raise ValueError('Unverified card contract')
    if line is None or line<0 or line*2!=int(line*2) or odd<=1:raise ValueError('Invalid card price/line')
    idx=0 if team=='HOME' else 1 if team=='AWAY' else None
    lo=bounds['lower'][idx] if idx is not None else sum(bounds['lower']);hi=bounds['upper'][idx] if idx is not None else sum(bounds['upper']);states=set()
    for count in range(int(lo),int(hi)+1):states.add('PUSH' if count==line else 'WIN' if (count>line if side=='OVER' else count<line) else 'LOSS')
    profits={'WIN':odd-1,'PUSH':0.,'LOSS':-1.}
    return dict(graded=True,countLower=lo,countUpper=hi,states=sorted(states),unambiguous=len(states)==1,profitLower=min(profits[s] for s in states),profitUpper=max(profits[s] for s in states))
