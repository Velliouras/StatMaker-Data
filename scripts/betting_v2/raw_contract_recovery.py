"""Exact raw O/U recovery and conservative card-label readiness checks."""
from collections import Counter,defaultdict
import math,re
from betting_v2.research_git_archive import absolute_time,_price,PRICE_FIELDS
from betting_v2.research_quotes import feature_cutoff
from betting_v2.research_scope_expansion import foul_or_legacy_spec
from betting_v2.market_calibration import book_identity

CARD_NAMES={'number of cards in match':'','bookings totals':'','bookings totals home':'HOME','bookings totals away':'AWAY'}

def numeric_entries(row):
    """Read explicit directions, including casing variants; never invent a side."""
    result=[];label=str(row.get('name') or row.get('label') or row.get('selection') or '').strip()
    for field in PRICE_FIELDS:
        p=_price(row.get(field))
        if p is not None:
            if label:result.append((label,p,field))
            break
    for field,value in row.items():
        if str(field).casefold() in ('over','under') and (p:=_price(value)) is not None:result.append((str(field).upper(),p,field))
    return result

def recover_snapshot(doc,source,max_age=86400):
    observed=absolute_time(source['committedAtUTC']);generated=absolute_time(doc.get('generatedAt'))
    if observed is None or generated is None or generated>observed:raise ValueError('Invalid raw archive generation clock')
    rows=[];audit=Counter();keys=Counter();examples={}
    for league in doc.get('leagues',[]):
        for match in league.get('matches',[]):
            kickoff=absolute_time(match.get('kickoff'));event=dict(home=match.get('providerHomeTeam') or match.get('homeTeam'),away=match.get('providerAwayTeam') or match.get('awayTeam'))
            for entry in match.get('providerMarkets',[]):
                market=entry.get('market') or {};name=str(market.get('name') or entry.get('providerMarket') or '');lower=' '.join(name.casefold().split());kind='FOULS' if 'foul' in lower else 'CARD' if any(t in lower for t in ('card','booking')) else None
                if kind is None:continue
                audit[kind+'::rawMarkets']+=1
                for row in market.get('odds') or []:
                    if not isinstance(row,dict):continue
                    for field,value in row.items():keys[kind+'::'+str(field)+'::'+type(value).__name__]+=1
                    for label,price,field in numeric_entries(row):
                        directions=re.findall(r'\b(over|under)\b',label.casefold())
                        side=directions[0].upper() if len(directions)==1 else 'OTHER';audit[kind+'::rawNumericSide::'+side]+=1
                        ek=kind+'::'+side+'::'+field
                        if ek not in examples:examples[ek]=dict(bookmaker=entry.get('bookmaker'),market=name,selection=label,field=field,line=row.get('hdp',row.get('line')),odd=price)
                    # Clock and identity exclusions apply before any candidate offer.
                    if not kickoff or not str(match.get('id','')).isdigit() or match.get('teamMappingStatus')!='matched' or observed>=kickoff:
                        audit[kind+'::invalidIdentityOrPostKickoff']+=1;continue
                    updated=absolute_time(market.get('updatedAt'))
                    if entry.get('exactProviderPayload') is not True or not entry.get('bookmaker') or updated is None or updated>observed or observed-updated>max_age:
                        audit[kind+'::invalidPayloadOrMarketClock']+=1;continue
                    for selection,odd,field in numeric_entries(row):
                        line=row.get('hdp') if row.get('hdp') is not None else row.get('line');offer=dict(providerMarketName=name,selection=selection,line=line)
                        if kind=='FOULS':spec=foul_or_legacy_spec(offer,event)
                        else:
                            spec=None;tokens=re.findall(r'\b(over|under)\b',selection.casefold())
                            if book_identity(entry['bookmaker'])=='bet365' and lower in CARD_NAMES and len(tokens)==1:
                                try:
                                    value=float(line)
                                    if math.isfinite(value) and value>=0 and value*2==int(value*2):
                                        team=CARD_NAMES[lower];spec=('BET365_CARD_UNITS','TEAM' if team else 'TOTAL',tokens[0].upper(),team,value)
                                except (ValueError,TypeError,OverflowError):pass
                        if spec is None:audit[kind+'::unmappedExactContract']+=1;continue
                        rows.append(dict(spec=list(spec),odd=odd,bookmaker=entry['bookmaker'],observed=observed,kickoff=kickoff,featureCutoff=feature_cutoff(observed),providerEventId=str(match['id']),sourceMarket=name,sourceReceipt='git:'+source['sha'],sourceCommit=source['sha'],sourceSha256=source['sha256'],marketUpdatedAtUTC=market['updatedAt'],leagueCode=league.get('leagueCode'),canonicalHome=match.get('homeTeam'),canonicalAway=match.get('awayTeam'),priceField=field,exactApiFootballFixtureMatchVerified=False,executionEvidence=False))
    return rows,dict(audit),dict(keys),examples

def card_readiness(response):
    """Return event-derived units, rejecting uncertain clock/appearance/completeness.

90+ card events are unresolved because these caches lack a final-whistle clock.
Second-yellow labels remain provisional until aggregate conventions are checked.
"""
    flags=set();fixture=response.get('fixture') or {};teams=response.get('teams') or {};team_ids=[(teams.get(s) or {}).get('id') for s in ('home','away')]
    if fixture.get('status',{}).get('short')!='FT':flags.add('notCompletedRegulationFT')
    if len(set(team_ids))!=2 or None in team_ids:flags.add('missingTeamIds')
    events=response.get('events');lineups=response.get('lineups');stats=response.get('statistics') or []
    if not isinstance(events,list):flags.add('missingEvents');events=[]
    if not isinstance(lineups,list):flags.add('missingLineups');lineups=[]
    starters={};bench={};coaches=set()
    for lineup in lineups:
        tid=(lineup.get('team') or {}).get('id');xi={(r.get('player') or {}).get('id') for r in lineup.get('startXI',[])};subs={(r.get('player') or {}).get('id') for r in lineup.get('substitutes',[])}
        if tid not in team_ids or len(xi)!=11 or None in xi or xi&subs:flags.add('incompleteOrInvalidXI')
        if tid in starters:flags.add('duplicateLineupTeam')
        starters[tid]=xi;bench[tid]=subs
        if (cid:=(lineup.get('coach') or {}).get('id')) is not None:coaches.add(cid)
    if set(starters)!=set(team_ids):flags.add('incompleteTwoTeamLineups')
    incoming=defaultdict(dict);outgoing=defaultdict(dict)
    for e in events:
        if str(e.get('type','')).casefold()!='subst':continue
        t=(e.get('time') or {}).get('elapsed');tid=(e.get('team') or {}).get('id');pid=(e.get('player') or {}).get('id');aid=(e.get('assist') or {}).get('id')
        if not isinstance(t,(int,float)) or isinstance(t,bool) or not 0<=t<=90 or pid is None or aid is None:flags.add('invalidSubstitutionEvidence');continue
        if pid in outgoing[tid] or aid in incoming[tid]:flags.add('duplicateSubstitutionPlayer')
        outgoing[tid][pid]=t;incoming[tid][aid]=t
    for team in response.get('players') or []:
        tid=(team.get('team') or {}).get('id')
        for item in team.get('players') or []:
            pid=(item.get('player') or {}).get('id');samples=item.get('statistics') or [];minutes=(samples[0].get('games') or {}).get('minutes') if samples else None
            if isinstance(minutes,(int,float)) and minutes>0 and pid not in starters.get(tid,set()) and pid not in incoming[tid]:flags.add('appearingBenchPlayerMissingSubstitution')
    raw=defaultdict(Counter);units=defaultdict(int);excluded=Counter();yellow_seen=set();red_seen=set();identities=set()
    for e in events:
        if e.get('type')!='Card':continue
        tid=(e.get('team') or {}).get('id');pid=(e.get('player') or {}).get('id');time=e.get('time') or {};minute=time.get('elapsed');detail=str(e.get('detail') or '').casefold();extra=time.get('extra')
        identity=(tid,pid,minute,extra,detail)
        if identity in identities:flags.add('duplicateCardEvent');continue
        identities.add(identity)
        if tid not in team_ids:flags.add('unknownCardTeam');continue
        second='second yellow' in detail
        if detail=='yellow card':raw[tid]['yellow']+=1
        elif detail=='red card':raw[tid]['red']+=1
        elif second:flags.add('secondYellowAggregateConventionUnverified');raw[tid]['secondYellow']+=1
        else:flags.add('unknownCardDetail');continue
        if not isinstance(minute,(int,float)) or isinstance(minute,bool) or not 0<=minute<90:flags.add('unverifiedCardPeriodOrFinalWhistle');continue
        if pid is None:flags.add('missingCardPlayerId');continue
        if pid in coaches and pid not in starters.get(tid,set())|bench.get(tid,set()) and pid not in incoming[tid]:flags.add('coachOrUnknownNonPlayer');continue
        if pid in outgoing[tid] and minute>=outgoing[tid][pid]:
            if minute==outgoing[tid][pid]:flags.add('cardAtSubstitutionMinute')
            else:excluded['afterFinalAppearance']+=1
            continue
        if pid not in starters.get(tid,set()) and pid not in incoming[tid]:
            if pid in bench.get(tid,set()):excluded['unusedSubstitute']+=1
            else:flags.add('unknownCardPlayerAppearance')
            continue
        pair=tid,pid
        if second:
            if pair not in red_seen:units[tid]+=2;red_seen.add(pair)
        elif detail=='yellow card':
            if pair in yellow_seen:flags.add('repeatedOrdinaryYellowWithoutDismissalDetail')
            else:units[tid]+=1;yellow_seen.add(pair)
        elif pair not in red_seen:units[tid]+=2;red_seen.add(pair)
    # Independent aggregate fields are a completeness check, never the target.
    aggregates={}
    for s in stats:
        tid=(s.get('team') or {}).get('id');values={v.get('type'):v.get('value') for v in s.get('statistics',[])};aggregates[tid]=values
    for tid in team_ids:
        for typ,key in [('Yellow Cards','yellow'),('Red Cards','red')]:
            value=aggregates.get(tid,{}).get(typ)
            if not isinstance(value,(int,float)) or isinstance(value,bool) or value<0 or int(value)!=value:flags.add('missingAggregateCardCompleteness')
            elif value!=raw[tid][key]:flags.add('eventAggregateCardMismatch')
    return dict(ready=not flags,flags=sorted(flags),provisionalUnits=[units[t] for t in team_ids],rawEventCounts={str(t):dict(raw[t]) for t in team_ids},excluded=dict(excluded),referee=fixture.get('referee'),finalWhistleClockVerified=False,
        definition='Bet365 1 yellow / 2 red; second yellows ignored; appearance and regulation checks; uncertain rows blocked')
