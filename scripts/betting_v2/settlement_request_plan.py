"""Offline, capped request plans for documented provider card settlement names.

This module has no network client and makes zero provider calls.
"""
from collections import defaultdict,Counter
import hashlib,json,math

SUPPORTED={'Bookings Totals':('TOTAL',''),'Bookings Totals Home':('TEAM','HOME'),'Bookings Totals Away':('TEAM','AWAY')}
RESULTS={'won','lost','void','half_won','half_lost','pending','ungradeable'}

def build(quotes):
    groups=defaultdict(list);excluded=Counter()
    for q in quotes:
        if q['spec'][0]!='BET365_CARD_UNITS':continue
        market=q['sourceMarket'];event=str(q['providerEventId']);line=q['spec'][4];side=q['spec'][2]
        if market not in SUPPORTED:excluded['notDocumentedSettlementMarket::'+market]+=1;continue
        scope,team=SUPPORTED[market]
        if q['spec'][1]!=scope or q['spec'][3]!=team or side not in ('OVER','UNDER') or not event.isdigit() or line is None or not math.isfinite(line) or line<0 or line*2!=int(line*2):raise ValueError('Invalid exact settlement contract')
        groups[event,market,float(line)].append(q)
    selections=[];bindings={}
    for key,rows in sorted(groups.items()):
        if len({(q['fixture'],q['kickoff']) for q in rows})!=1:raise ValueError('Provider settlement key maps to multiple fixtures')
        # Grade one actually observed side. Opposite side is its settlement
        # complement, including a whole-line push, not an invented odds price.
        chosen=min(rows,key=lambda q:(q['spec'][2]!='OVER',q['observed'],q['sourceReceipt']))
        identity=dict(eventId=int(key[0]),market=key[1],hdp=key[2],side=chosen['spec'][2].lower())
        ref='card-'+hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:24]
        selections.append(dict(ref=ref,**identity));bindings[ref]=dict(fixture=chosen['fixture'],kickoff=chosen['kickoff'],observed=chosen['observed'],sourceReceipt=chosen['sourceReceipt'],sourceSha256=chosen['sourceSha256'],spec=chosen['spec'],sourceMarket=chosen['sourceMarket'],bookmaker=chosen['bookmaker'],observedOfferRows=len(rows))
    batches=[dict(selections=selections[i:i+50]) for i in range(0,len(selections),50)]
    if any(len(json.dumps(b).encode())>64000 for b in batches):raise ValueError('Provider batch body exceeds documented 64KB bound')
    return dict(selections=selections,batches=batches,bindings=bindings,summary=dict(uniqueSettlementSelections=len(selections),events=len({s['eventId'] for s in selections}),requestsAtMost50=len(batches),excluded=dict(excluded),providerCallsMade=0,llmCalls=0,executed=False,
        limitations=['Exact documented Bookings names only; Number of Cards In Match is preserved but not silently renamed.',
            'Provider grading uses common practice and is not a bookmaker-specific payout receipt.',
            'Initial request count excludes any later retry of pending/non-final grades.',
            'No new provider calls are authorized or made by this offline planner.']))

def validate_cached_grades(plan,responses):
    expected={s['ref']:s for s in plan['selections']};seen={};coverage=Counter();usable={}
    for response in responses:
        if not isinstance(response.get('grades'),list):raise ValueError('Missing provider grade array')
        for g in response['grades']:
            ref=g.get('ref');result=g.get('result')
            if ref not in expected or result not in RESULTS:raise ValueError('Unknown grade ref/result')
            if ref in seen and seen[ref]!=g:raise ValueError('Conflicting cached grades require explicit later-version reconciliation')
            seen[ref]=g
    for ref in expected:
        g=seen.get(ref)
        if g is None:coverage['missing']+=1;continue
        if g.get('final') is not True:coverage['notFinal']+=1;continue
        if g['result'] in ('pending','ungradeable'):coverage[g['result']]+=1;continue
        if g['result']=='void' and g.get('reason')!='push':coverage['voidNonPush']+=1;continue
        if g['result'] in ('half_won','half_lost'):raise ValueError('Half result for an integer/half-line card contract')
        state={'won':'WIN','lost':'LOSS','void':'PUSH'}[g['result']];usable[ref]=dict(state=state,oppositeState={'WIN':'LOSS','LOSS':'WIN','PUSH':'PUSH'}[state]);coverage['finalUsable']+=1
    return dict(coverage=dict(coverage),usable=usable,providerCalls=0,source='Validated cached provider responses, not independently certified bookmaker payout')
