"""Route enabled market forecasts without altering their frozen contract."""
import hashlib,json
from .build import ROOT,OUT,read,write,allowed
from .policy import time
from .catalog import app_market,selection_text,settlement,families
from .quotes import current_quotes,bind

def spec_of(r):
    return r.get('quoteSpec',{}).get('spec') or [r['metric'].upper(),r['scope'],r['side'],r['teamSide'],r['line']]

def stage(actual,now,leagues,raw):
    from scripts.shots_uat.feed import bind_quote,iso,favourite
    from scripts.shots_uat.sources import Identities
    identity=Identities(ROOT,actual);path=OUT/'market_ledger.json';ledger=read(path,dict(schemaVersion=2,modelId='MARKET_WATCH_MODELS',rows=[]))
    issued=ledger['rows']+read(ROOT/'data/statmaker/uat/shots_v1/ledger.json',{}).get('rows',[]);used=set()
    for r in issued:used.update((str(r['apiFixtureId']),f) for f in families(spec_of(r)))
    from .recommend import candidates as current_candidates, POLICY
    from .build import config
    cfg=config()
    candidates=[q for q in read(OUT/'shadow_candidates.json',{}).get('rows',[])
                if not cfg['markets'][q['appMarket']].get('uatPilotEnabled')]
    candidates += current_candidates(actual,now,leagues,used)
    live=current_quotes(now) if candidates else {}
    for q in sorted(candidates,key=lambda q:(q.get('preference',1),-q['_monitorP'],-q.get('ev',0),str(q['spec']),q['bookmaker'])):
        metric,scope,side,team,line=q['spec'];market=app_market(q['spec']);model=q.get('_monitorModel','COUNT_COUPLED_V1')
        if q['featureCutoff']>q['observed'] or not allowed(market,now) or not now+900<q['kickoff']<now+7*86400:continue
        choices=[]
        for league in leagues:
            for m in league.get('matches',[]):
                if str(m.get('id'))!=str(q['providerEventId']) or abs(time(m['kickoff'])-q['kickoff'])>.001:continue
                bound,reason=identity.bind(league,m)
                if bound is not None and bound[1:3]==(q['_home'],q['_away']):choices.append((league,bound,reason))
        if len(choices)!=1:continue
        league,(m,h,a,api),reason=choices[0];keys={(str(api),f) for f in families(q['spec'])}
        if used&keys:continue
        pilot=q.get('_monitorPolicy')==POLICY
        if model=='ALL_MARKETS_JOINT_V1' or pilot:observed=bind(q,m,live)
        else:
            if line is None or line<0 or (line*2)%1:continue
            binding=bind_quote(m,dict(selection=side,line=line,odds=q['odd'],bookmaker=q['bookmaker'],providerMarketName=q['sourceMarket'],exactBookmakerOdds=True),raw)
            observed=binding[1] if binding else None
        if observed is None or not now-86400<=observed<=now:continue
        if team and (favourite(m,q['bookmaker'],raw,now)!=q.get('_favourite') or q.get('_favourite') not in ('HOME','AWAY')):continue
        p,push=q['_monitorP'],q['_monitorPush'];ev=p*q['odd']+push-1
        if p<.60 or not 1.8<q['odd']<3 or ev<=0:continue
        match={k:v for k,v in m.items() if k!='markets'};match.update({k:league[k] for k in ('leagueCode','country','competition','season')})
        group=f'api:{api}:'+','.join(sorted(families(q['spec'])))
        sid=hashlib.sha256(json.dumps([model,group,q['spec'],q['bookmaker'],q['odd']]).encode()).hexdigest()
        numeric_line=float(line) if isinstance(line,(int,float)) and scope in ('TOTAL','TEAM','BTTS_TOTAL','RESULT_TOTAL') else None
        row=dict(selectionId=sid,groupKey=group,fixtureKey=f'api:{api}:{q["kickoff"]:.0f}',fixtureId=str(m['id']),apiFixtureId=int(api),competitionId=league.get('competitionId','domestic'),
            homeTeam=m['homeTeam'],awayTeam=m['awayTeam'],kickoff=m['kickoff'],match=match,market=market,selection=selection_text(q['spec']),
            team=m['homeTeam'] if team=='HOME' else m['awayTeam'] if team=='AWAY' else None,teamSide=team,line=numeric_line,side=side,odd=q['odd'],bookmaker=q['bookmaker'],
            winProbability=p,pushProbability=push,indicativeEV=ev,model=model,policyId=POLICY if pilot else 'MARKET_WATCH_UAT_V1',probabilityModel='FROZEN_JOINT_POINT' if pilot else 'FROZEN_JOINT_CONSERVATIVE' if model=='ALL_MARKETS_JOINT_V1' else 'COUPLED_COUNT_CONSERVATIVE',
            metric=metric.lower(),scope=scope,featureCutoff=iso(q['featureCutoff']),quoteUpdatedAt=iso(observed),issuedAt=iso(now),publishedAt=None,publicationCommit=None,
            outcome='PENDING',settlementReturn=None,settledAt=None,identityBinding=reason,bookmakerBinding='EXACT_PROVIDER_QUOTE',certified=False,category='UAT_MARKET_EXPERIMENT',
            providerMarketName=q['sourceMarket'],monitorPaperSelectionId=q['_monitorId'],modelFingerprint=q.get('_modelFingerprint'),
            quoteObservationSha256=q.get('sourceSha256'),quoteObservationSemantics=q.get('quoteObservationSemantics','SHA_VERIFIED_CLIENT_RECEIPT'),
            favouriteSide=q.get('_favourite'),selectionPreference=q.get('preference',1),
            quoteSpec={k:q.get(k) for k in ('providerEventId','kickoff','bookmaker','sourceMarket','selection','providerSelectionId','line','odd','spec')})
        ledger['rows'].append(row);used.update(keys)
    write(path,ledger)

def settle_rows(rows,actual,now):
    from scripts.shots_uat.feed import iso
    from .results import exact_settlement
    result=[]
    for original in rows:
        r=dict(original);result.append(r)
        if r['outcome']!='PENDING' or not r.get('publishedAt') or now<time(r['kickoff'])+10800:continue
        evidence=exact_settlement(r,spec_of(r),actual,ROOT,now)
        if evidence is None:continue
        (y,void),source,clock=evidence
        r.update(outcome='VOID' if void else 'WON' if y else 'LOST',settlementReturn=1. if void else r['odd'] if y else 0.,
                 settledAt=iso(now),settlementSource=source,settlementEvidenceGeneratedAt=iso(clock) if clock else None)
    return result


def publish(actual,now,raw):
    from scripts.shots_uat.feed import verify_publication,bind_quote,iso
    path=OUT/'market_ledger.json';ledger=read(path,dict(schemaVersion=2,modelId='MARKET_WATCH_MODELS',rows=[]))
    if not path.exists():return [],[]
    ledger['rows']=verify_publication(ledger['rows'],path)
    ledger['rows']=settle_rows(ledger['rows'],actual,now)
    write(path,ledger);issued=[];live=[]
    lookup=current_quotes(now) if any(time(r['kickoff'])>now for r in ledger['rows']) else {}
    for original in ledger['rows']:
        if not original.get('publishedAt'):continue
        r=dict(original)
        if r['model']=='ALL_MARKETS_JOINT_V1' or r.get('probabilityModel')=='FROZEN_JOINT_POINT':observed=bind(r['quoteSpec'],r['match'],lookup)
        else:
            binding=bind_quote(r['match'],dict(selection=r['selection'],line=r['line'],odds=r['odd'],bookmaker=r['bookmaker'],providerMarketName=r['providerMarketName'],exactBookmakerOdds=True),raw)
            observed=binding[1] if binding else None
        r['latestQuoteObservedAt']=iso(observed) if observed is not None and now-86400<=observed<=now else None
        issued.append(r)
        if time(r['kickoff'])>now and r['latestQuoteObservedAt'] and allowed(r['market'],now):live.append(r)
    return issued,live
