#!/usr/bin/env python3
from __future__ import annotations
import datetime as dt, json, os, re, sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
INDEX=ROOT/'data/statmaker/domestic_enriched/index.json'
ODDS=ROOT/'odds/odds_api_io/domestic_odds.json'
CACHE=ROOT/'data/api_football/fixture_stats/greece/super-league-2/2025/fixture_stats.json'
ART=ROOT/'data/statmaker/domestic_enriched/greece_super_league_2_2025.json'
REPORT=ROOT/'reports/greece_super_league_2_sync.json'
BASE='https://v3.football.api-sports.io'; CODE='G2'; COUNTRY='Greece'; NAME='Super League 2'; DONE={'FT','AET','PEN'}

def read(p,d): return json.loads(p.read_text(encoding='utf-8-sig')) if p.is_file() else d
def write(p,x): p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def now(): return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
def num(x):
    try:return int(x)
    except:return None
def slug(x): return re.sub(r'[^a-z0-9]+','-',str(x or '').lower()).strip('-')
def items(x): return [r for r in x.get('response',[]) if isinstance(r,dict)]

class Client:
    def __init__(self,key,cap): self.key=key; self.cap=cap; self.used=0
    def get(self,ep,params):
        if self.used>=self.cap: raise RuntimeError('API request cap reached')
        self.used+=1; q=urlencode(params)
        req=Request(f'{BASE}/{ep}?{q}',headers={'x-apisports-key':self.key,'Accept':'application/json','User-Agent':'StatMaker G2 sync'})
        with urlopen(req,timeout=45) as r: x=json.loads(r.read().decode())
        if x.get('errors') not in (None,{},[], ''): raise RuntimeError(f'{ep}: {x.get("errors")}')
        return x

def resolve(rows):
    good=[]
    for r in rows:
        l=r.get('league') or {}; c=r.get('country') or {}
        n=re.sub(r'[^a-z0-9]+',' ',str(l.get('name','')).lower()).strip()
        if str(c.get('name','')).casefold()==COUNTRY.casefold() and n=='super league 2' and num(l.get('id')): good.append(r)
    if len(good)!=1: raise RuntimeError(f'exact Greece / Super League 2 resolution failed: {len(good)}')
    return good[0]

def seasons(row):
    ss=[s for s in row.get('seasons',[]) if isinstance(s,dict) and num(s.get('year'))]
    by={num(s['year']):s for s in ss}; hist=by.get(2025)
    target=by.get(2026) or max([s for s in ss if s.get('current') is True] or ss,key=lambda s:num(s['year']))
    if not hist: hist=max([s for s in ss if num(s['year'])<num(target['year'])],key=lambda s:num(s['year']))
    return hist,target

def status(r): return str(((r.get('fixture') or {}).get('status') or {}).get('short','')).upper()
def fid(r): return str((r.get('fixture') or {}).get('id',''))
def kickoff(r): return str((r.get('fixture') or {}).get('date',''))
def team(r,s): return ((r.get('teams') or {}).get(s) or {})
def value(v):
    if v is None:return None
    try:return float(str(v).replace('%',''))
    except:return None

def normalized(raw,home,away):
    data={}
    for r in raw:
        i=num((r.get('team') or {}).get('id'))
        data[i]={str(x.get('type','')).casefold():x.get('value') for x in r.get('statistics',[]) if isinstance(x,dict)}
    def g(i,k): return value(data.get(i,{}).get(k))
    return {'HC':g(home,'corner kicks'),'AC':g(away,'corner kicks'),'HY':g(home,'yellow cards'),'AY':g(away,'yellow cards'),'HR':g(home,'red cards'),'AR':g(away,'red cards'),'HS':g(home,'total shots'),'AS':g(away,'total shots'),'HST':g(home,'shots on goal'),'AST':g(away,'shots on goal')}

def history_row(r,old=None):
    h,a=team(r,'home'),team(r,'away'); goals=r.get('goals') or {}; half=(r.get('score') or {}).get('halftime') or {}
    x=dict(old or {}); x.update({'fixture_id':fid(r),'date_utc':kickoff(r),'date':kickoff(r)[:10],'status':status(r),'home_team_id':num(h.get('id')),'away_team_id':num(a.get('id')),'home_team':str(h.get('name','')),'away_team':str(a.get('name','')),'home_team_logo':h.get('logo'),'away_team_logo':a.get('logo'),'home_goals':num(goals.get('home')),'away_goals':num(goals.get('away')),'hthg':num(half.get('home')),'htag':num(half.get('away')),'source':'api-football'})
    return x

def refresh_history(client,league_id,year,fixtures,stat_cap):
    old={str(x.get('fixture_id')):x for x in read(CACHE,{}).get('fixtures',[]) if x.get('fixture_id')}
    rows={fid(r):history_row(r,old.get(fid(r))) for r in fixtures if status(r) in DONE and fid(r)}; fetched=0
    for k in sorted(rows,key=lambda z:(rows[z].get('date',''),z)):
        if any(v is not None for v in (rows[k].get('normalized_stats') or {}).values()):continue
        if fetched>=stat_cap:break
        try: raw=items(client.get('fixtures/statistics',{'fixture':k}))
        except Exception: continue
        rows[k]['raw_statistics']=raw; rows[k]['normalized_stats']=normalized(raw,rows[k]['home_team_id'],rows[k]['away_team_id']); fetched+=1
    out=sorted(rows.values(),key=lambda x:(x.get('date',''),x.get('fixture_id','')))
    write(CACHE,{'schema_version':1,'generated_at':now(),'source':'api-football','country':COUNTRY,'competition':NAME,'league_code':CODE,'api_football_league_id':league_id,'season':str(year),'fixtures':out})
    return out,fetched

def coverage(rows):
    anyc=core=0
    for r in rows:
        s=r.get('normalized_stats') or {}; groups=[s.get('HC') is not None and s.get('AC') is not None,s.get('HY') is not None and s.get('AY') is not None,s.get('HS') is not None and s.get('AS') is not None,s.get('HST') is not None and s.get('AST') is not None]
        anyc+=any(groups); core+=groups[0] and groups[1]
    return anyc,core,(core/len(rows) if rows else 0.0)

def publish_stats(league_id,hy,ty,rows,catalog):
    anyc,core,cov=coverage(rows); ts=next((s for s in catalog.get('seasons',[]) if num(s.get('year'))==ty),{})
    comp={'league_code':CODE,'country':COUNTRY,'league':NAME,'api_football_league_id':league_id,'api_football_season':str(hy),'app_season':f'{hy}-{hy+1}','target_api_football_season':str(ty),'target_app_season':f'{ty}-{ty+1}','target_season_start':ts.get('start'),'target_season_end':ts.get('end'),'stats_visible_without_odds':True,'betting_enabled':False}
    write(ART,{'schema_version':3,'generated_at':now(),'artifact_type':'statmaker_domestic_enriched_league','active_source':'api-football','csv_import':'inactive_archive_only','competition':comp,'data_contract':{'schedule_visibility':'official API-Football fixtures are published even without odds','betting_gate':'disabled pending quality review','synthetic_stats':False},'quality_summary':{'completed_fixtures':len(rows),'fixtures_with_any_stats':anyc,'fixtures_with_bb_core_stats':core,'bb_core_coverage':round(cov,4),'bb_ready_candidate':False},'matches':rows})
    idx=read(INDEX,{}); row={'league_code':CODE,'country':COUNTRY,'league':NAME,'app_season':f'{hy}-{hy+1}','api_football_season':str(hy),'api_football_league_id':league_id,'priority_group':'southern_europe','output_path':str(ART.relative_to(ROOT)),'cache_path':str(CACHE.relative_to(ROOT)),'completed_fixtures':len(rows),'fixtures_with_any_stats':anyc,'fixtures_with_bb_core_stats':core,'any_stats_coverage':round(anyc/len(rows),4) if rows else 0.0,'bb_core_coverage':round(cov,4),'bb_ready_candidate':False,'notes':'schedule and history enabled; betting disabled','target_api_football_season':str(ty),'target_app_season':f'{ty}-{ty+1}','target_season_start':ts.get('start'),'target_season_end':ts.get('end'),'lifecycle':'schedule_only','stats_visible_without_odds':True,'betting_enabled':False}
    ls=[x for x in idx.get('leagues',[]) if x.get('league_code')!=CODE]+[row]; ls.sort(key=lambda x:(str(x.get('country','')),str(x.get('league','')))); idx.update({'schema_version':max(3,num(idx.get('schema_version')) or 0),'generated_at':now(),'artifact_type':'statmaker_domestic_enriched_index','active_source':'api-football','csv_import':'inactive_archive_only','league_count':len(ls),'leagues':ls}); write(INDEX,idx); return row

def future(rows,today,horizon):
    end=today+dt.timedelta(days=horizon); out=[]
    for r in rows:
        try:d=dt.date.fromisoformat(kickoff(r)[:10])
        except:continue
        if today<=d<=end and status(r) not in DONE|{'CANC','ABD','AWD','WO'}:out.append(r)
    return sorted(out,key=lambda r:(kickoff(r),fid(r)))

def key(m): return ('id',str(m.get('id'))) if m.get('id') else ('f',slug(m.get('homeTeam')),slug(m.get('awayTeam')),str(m.get('date','')))
def schedule_row(r,hist):
    h,a=team(r,'home'),team(r,'away'); hn,an=str(h.get('name','')),str(a.get('name',''))
    return {'id':fid(r),'date':kickoff(r)[:10],'kickoff':kickoff(r),'providerHomeTeam':hn,'providerAwayTeam':an,'homeTeam':hn,'awayTeam':an,'canonicalHomeTeam':hn,'canonicalAwayTeam':an,'homeTeamLogo':h.get('logo'),'awayTeamLogo':a.get('logo'),'teamMappingStatus':'schedule_only_api_football','usableForStats':slug(hn) in hist or slug(an) in hist,'scheduleOnly':True,'scheduleSource':'api-football','markets':[]}
def publish_schedule(league_id,year,fixtures,hist):
    odds=read(ODDS,{'source':'odds-api-io','leagues':[]}); ls=[x for x in odds.get('leagues',[]) if isinstance(x,dict)]; league=next((x for x in ls if x.get('leagueCode')==CODE),None)
    if league is None: league={'leagueCode':CODE,'country':COUNTRY,'competition':NAME,'season':f'{year}-{year+1}','providerLeagueSlug':'api-football-schedule-only','matches':[]}; ls.append(league)
    keep=[dict(x) for x in league.get('matches',[]) if not (x.get('scheduleOnly') is True and not x.get('markets'))]; by={key(x):x for x in keep}
    for r in fixtures:
        fresh=schedule_row(r,hist); cur=by.get(key(fresh)); merged={**fresh,**(cur or {})}; merged['date']=fresh['date']; merged['kickoff']=fresh['kickoff']; merged['markets']=(cur or {}).get('markets',[]); merged['scheduleOnly']=not bool(merged['markets']); by[key(fresh)]=merged
    league.update({'country':COUNTRY,'competition':NAME,'season':f'{year}-{year+1}','matches':sorted(by.values(),key=lambda x:(str(x.get('kickoff','')),str(x.get('homeTeam',''))))}); ls.sort(key=lambda x:(str(x.get('country','')),str(x.get('competition','')))); odds['leagues']=ls; odds['generatedAt']=now(); odds.setdefault('debug',{})['greeceSuperLeague2Schedule']={'source':'api-football','leagueId':league_id,'targetSeason':year,'scheduleFixtureCount':len(fixtures),'bettingEnabled':False,'syntheticOdds':False}; write(ODDS,odds); return league

def main():
    token=os.getenv('API_FOOTBALL_KEY','').strip()
    if not token:return 2
    stat_cap=max(0,int(os.getenv('G2_STATS_REQUEST_CAP','45'))); horizon=max(1,int(os.getenv('G2_SCHEDULE_HORIZON_DAYS','120'))); c=Client(token,stat_cap+4)
    catalog=resolve(items(c.get('leagues',{'country':COUNTRY,'search':NAME}))); league_id=num((catalog.get('league') or {}).get('id')); hs,ts=seasons(catalog); hy,ty=num(hs['year']),num(ts['year'])
    rows,fetched=refresh_history(c,league_id,hy,items(c.get('fixtures',{'league':league_id,'season':hy})),stat_cap); idx=publish_stats(league_id,hy,ty,rows,catalog)
    upcoming=future(items(c.get('fixtures',{'league':league_id,'season':ty})),dt.datetime.now(dt.timezone.utc).date(),horizon); hist={slug(r.get(k)) for r in rows for k in ('home_team','away_team') if slug(r.get(k))}; league=publish_schedule(league_id,ty,upcoming,hist)
    report={'generatedAt':now(),'leagueCode':CODE,'apiFootballLeagueId':league_id,'historySeason':hy,'targetSeason':ty,'requestsUsed':c.used,'historicalFixtures':len(rows),'statisticsFetchedThisRun':fetched,'scheduleFixtureCount':len(upcoming),'publishedScheduleMatchCount':len(league.get('matches',[])),'indexRow':idx,'bettingEnabled':False,'syntheticStats':False,'syntheticOdds':False}; write(REPORT,report); print(json.dumps(report,ensure_ascii=False,indent=2)); return 0
if __name__=='__main__': raise SystemExit(main())
