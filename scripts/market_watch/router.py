"""Publish enabled count markets from frozen paper forecasts, at an exact live price."""
import hashlib,json
from datetime import datetime,timezone
from .build import ROOT,OUT,read,write,allowed
from .policy import time

MODEL="COUNT_COUPLED_V1"
NAMES={('CORNERS','TOTAL'):'MATCH_CORNERS',('CORNERS','TEAM'):'TEAM_CORNERS',
       ('FOULS','TOTAL'):'MATCH_FOULS',('FOULS','TEAM'):'TEAM_FOULS'}


def stage(actual,now,leagues,raw):
    from scripts.shots_uat.feed import bind_quote,iso,favourite
    from scripts.shots_uat.sources import Identities
    identity=Identities(ROOT,actual);path=OUT/"market_ledger.json";ledger=read(path,dict(schemaVersion=1,modelId=MODEL,rows=[]))
    known={r['groupKey'] for r in ledger['rows']};candidates=read(OUT/"shadow_candidates.json",{}).get('rows',[])
    for q in candidates:
        metric,scope,side,team,line=q['spec']
        if line<0 or (line*2)%1 or q['featureCutoff']>q['observed']:continue
        if not allowed(metric,now) or not now+900<q['kickoff']<now+7*86400:continue
        choices=[]
        for league in leagues:
            for m in league.get('matches',[]):
                if str(m.get('id'))!=str(q['providerEventId']) or abs(time(m['kickoff'])-q['kickoff'])>.001:continue
                bound,reason=identity.bind(league,m)
                if bound is not None and bound[1:3]==(q['_home'],q['_away']):choices.append((league,bound,reason))
        if len(choices)!=1:continue
        league,(m,h,a,api),reason=choices[0];group=f'api:{api}:{metric.lower()}'
        if group in known:continue
        quote=dict(selection=side,line=line,odds=q['odd'],bookmaker=q['bookmaker'],
                   providerMarketName=q['sourceMarket'],exactBookmakerOdds=True)
        binding=bind_quote(m,quote,raw)
        if not binding or not now-86400<=binding[1]<=now:continue
        if scope=='TEAM' and (favourite(m,q['bookmaker'],raw,now)!=q.get('_favourite') or q.get('_favourite') not in ('HOME','AWAY')):continue
        p,push=q['_monitorP'],q['_monitorPush'];ev=p*q['odd']+push-1
        if p<.60 or not 1.8<q['odd']<3 or ev<=0:continue
        match={k:v for k,v in m.items() if k!='markets'};match.update({k:league[k] for k in ('leagueCode','country','competition','season')})
        sid=hashlib.sha256(json.dumps([MODEL,group,q['spec'],q['bookmaker'],q['odd']]).encode()).hexdigest()
        row=dict(selectionId=sid,groupKey=group,fixtureKey=f'api:{api}:{q["kickoff"]:.0f}',fixtureId=str(m['id']),apiFixtureId=int(api),
                 competitionId=league.get('competitionId','domestic'),homeTeam=m['homeTeam'],awayTeam=m['awayTeam'],kickoff=m['kickoff'],
                 match=match,market=NAMES[metric,scope],selection=f'{side.title()} {line:g}',team=m['homeTeam'] if team=='HOME' else m['awayTeam'] if team=='AWAY' else None,
                 teamSide=team,line=line,side=side,odd=q['odd'],bookmaker=q['bookmaker'],winProbability=p,pushProbability=push,indicativeEV=ev,
                 model=MODEL,policyId='MARKET_WATCH_UAT_V1',probabilityModel='COUPLED_COUNT_CONSERVATIVE',metric=metric.lower(),scope=scope,
                 featureCutoff=iso(q['featureCutoff']),quoteUpdatedAt=iso(binding[1]),issuedAt=iso(now),publishedAt=None,publicationCommit=None,
                 outcome='PENDING',settlementReturn=None,settledAt=None,identityBinding=reason,bookmakerBinding='EXACT_PROVIDER_QUOTE',
                 certified=False,category='UAT_MARKET_EXPERIMENT',providerMarketName=q['sourceMarket'],monitorPaperSelectionId=q['_monitorId'])
        ledger['rows'].append(row);known.add(group)
    write(path,ledger)


def publish(actual,now,raw):
    from scripts.shots_uat.feed import verify_publication,bind_quote,iso
    from materialize_betting_v2_direct import metric_values
    from betting_v2.research_app_markets import outcome_extended
    path=OUT/'market_ledger.json';ledger=read(path,dict(schemaVersion=1,modelId=MODEL,rows=[]))
    if not path.exists():return [],[]
    ledger['rows']=verify_publication(ledger['rows'],path)
    for r in ledger['rows']:
        if r['outcome']!='PENDING' or not r.get('publishedAt') or now<time(r['kickoff'])+10800:continue
        choices=[v for v in actual if str(v.get('fixture_id'))==str(r['apiFixtureId']) and abs(v['_date']-time(r['kickoff']))<=300]
        if len(choices)!=1:continue
        values=metric_values(choices[0],r['metric'].upper())
        if values is None:continue
        y,void=outcome_extended(*values,[r['metric'].upper(),r['scope'],r['side'],r['teamSide'],r['line']])
        r.update(outcome='VOID' if void else 'WON' if y else 'LOST',settlementReturn=1. if void else r['odd'] if y else 0.,settledAt=iso(now))
    write(path,ledger);issued=[];live=[]
    for original in ledger['rows']:
        if not original.get('publishedAt'):continue
        r=dict(original);quote=dict(selection=r['selection'],line=r['line'],odds=r['odd'],bookmaker=r['bookmaker'],
                                    providerMarketName=r['providerMarketName'],exactBookmakerOdds=True)
        binding=bind_quote(r['match'],quote,raw)
        r['latestQuoteObservedAt']=iso(binding[1]) if binding and now-86400<=binding[1]<=now else None
        issued.append(r)
        if time(r['kickoff'])>now and r['latestQuoteObservedAt'] and allowed(r['metric'].upper(),now):live.append(r)
    return issued,live
