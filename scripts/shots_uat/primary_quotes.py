"""Bind API-Football normalized snapshots without claiming raw HTTP receipts."""
from collections import defaultdict
from datetime import datetime
import hashlib
import json
import math
import re

CONTRACT='api-football-primary-unibet-fallback-v1'
NAMES={'1X2':'ML','BTTS':'Both Teams To Score','DOUBLE_CHANCE':'Double Chance',
       'MATCH_GOALS':'Totals','FIRST_HALF_GOALS':'Totals HT','SECOND_HALF_GOALS':'Totals 2H',
       'MATCH_SHOTS':'Total Shots','MATCH_SHOTS_ON_TARGET':'Total Shots on Target',
       'TEAM_SHOTS':'Total Shots','TEAM_SHOTS_ON_TARGET':'Total Shots on Target',
       'MATCH_CORNERS':'Corners Totals','TEAM_CORNERS':'Corners Totals',
       'TEAM_TOTAL_GOALS':'Total Goals','MATCH_CARDS':'Bookings Totals','TEAM_CARDS':'Bookings Totals'}


def stamp(value):
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError):return 0.


def packet(path,now):
    if not path.exists():return {},{}
    data=path.read_bytes();doc=json.loads(data)
    if doc.get('oddsProviderPolicy')!=CONTRACT:return {},{}
    sha=hashlib.sha256(data).hexdigest();raw=defaultdict(list);active={}
    for league in doc.get('leagues') or [doc]:
        for match in league.get('matches',[]):
            eid=str(match['id']);active[eid]=set()
            assembled={}
            for q in match.get('markets',[]):
                if q.get('bookmaker')=='Unibet' and q.get('oddsProvider')!='api-football':
                    active[eid].add(('Unibet',q.get('providerMarketName')))
                    active[eid].add(('Unibet','ML'))
                    continue
                observed=stamp(q.get('oddsObservedAt'))
                if (q.get('oddsProvider')!='api-football' or q.get('exactBookmakerOdds') is not True
                        or not q.get('apiFootballFixtureId') or str(q['apiFootballFixtureId'])!=str(match.get('apiFootballFixtureId'))
                        or not now-86400<=observed<=now or observed>=stamp(match.get('kickoff'))):continue
                family=q.get('market');name=NAMES.get(family)
                if not name:continue
                if family.startswith('TEAM_'):
                    team=q.get('team')
                    if team not in (match.get('homeTeam'),match.get('awayTeam')):continue
                    name+=' Home' if team==match.get('homeTeam') else ' Away'
                label=str(q.get('selection',''));direction=re.search(r'\b(over|under)\b',label,re.I)
                side=direction[1].lower() if direction else {'1X':'1X','12':'12','X2':'X2'}.get(label,label.lower())
                try:price=float(q['odds'])
                except (TypeError,ValueError,KeyError):continue
                if not math.isfinite(price) or price<=1:continue
                key=(eid,q['bookmaker'],name,observed)
                if key not in assembled:
                    assembled[key]=dict(name=name,odds=[],updatedAt=q.get('oddsUpdatedAt'),
                        _kickoff=match['kickoff'],_home=match.get('providerHomeTeam'),_away=match.get('providerAwayTeam'),
                        _observedAt=observed,_receiptSha256=sha,_evidence='API_FOOTBALL_NORMALIZED_SNAPSHOT',
                        _nativeProviderMarketName=q.get('providerMarketName'),_apiFootballFixtureId=str(q['apiFootballFixtureId']))
                odd={side:price}
                if q.get('line') is not None:odd['hdp']=q['line']
                assembled[key]['odds'].append(odd)
            for (eid,book,name,_),market in assembled.items():raw[eid,book,name].append(market)
    return raw,active


def merge(raw,path,now):
    extra,active=packet(path,now)
    if not active:return raw
    result=defaultdict(list,{k:v for k,v in raw.items() if k[0] not in active or (k[1],k[2]) in active[k[0]]})
    for key,markets in extra.items():result[key]=markets
    return result
