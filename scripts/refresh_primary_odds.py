"""API-Football exact prices first; free Unibet fills missing complete families."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import json
import math
import os
from pathlib import Path
import re
import time
import unicodedata
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from odds_market_integrity import _choose_bookmaker

CONTRACT = 'api-football-primary-unibet-fallback-v1'
BOOKMAKERS = ('Bet365',)
MAX_CACHED_HOURS = 24
# IDs and names must agree. Player props, Asian/handicap and unknown contracts stay out.
MARKETS = {
    1: ('Match Winner', '1X2', None),
    5: ('Goals Over/Under', 'MATCH_GOALS', None),
    6: ('Goals Over/Under First Half', 'FIRST_HALF_GOALS', None),
    8: ('Both Teams Score', 'BTTS', None),
    12: ('Double Chance', 'DOUBLE_CHANCE', None),
    16: ('Total - Home', 'TEAM_TOTAL_GOALS', 'Home'),
    17: ('Total - Away', 'TEAM_TOTAL_GOALS', 'Away'),
    20: ('Double Chance - First Half', 'HALF_TIME_DOUBLE_CHANCE', None),
    26: ('Goals Over/Under - Second Half', 'SECOND_HALF_GOALS', None),
    45: ('Corners Over Under', 'MATCH_CORNERS', None),
    57: ('Home Corners Over/Under', 'TEAM_CORNERS', 'Home'),
    58: ('Away Corners Over/Under', 'TEAM_CORNERS', 'Away'),
    80: ('Cards Over/Under', 'MATCH_CARDS', None),
    82: ('Home Team Total Cards', 'TEAM_CARDS', 'Home'),
    83: ('Away Team Total Cards', 'TEAM_CARDS', 'Away'),
    87: ('Total ShotOnGoal', 'MATCH_SHOTS_ON_TARGET', None),
    88: ('Home Total ShotOnGoal', 'TEAM_SHOTS_ON_TARGET', 'Home'),
    89: ('Away Total ShotOnGoal', 'TEAM_SHOTS_ON_TARGET', 'Away'),
    105: ('Home Team Total Goals(1st Half)', 'TEAM_FIRST_HALF_GOALS', 'Home'),
    106: ('Away Team Total Goals(1st Half)', 'TEAM_FIRST_HALF_GOALS', 'Away'),
    107: ('Home Team Total Goals(2nd Half)', 'TEAM_SECOND_HALF_GOALS', 'Home'),
    108: ('Away Team Total Goals(2nd Half)', 'TEAM_SECOND_HALF_GOALS', 'Away'),
    211: ('Total Shots', 'MATCH_SHOTS', None),
    220: ('Shots. Away Total', 'TEAM_SHOTS', 'Away'),
    221: ('Shots. Home Total', 'TEAM_SHOTS', 'Home'),
    243: ('Home Shots On Target', 'TEAM_SHOTS_ON_TARGET', 'Home'),
    244: ('Away Shots On Target', 'TEAM_SHOTS_ON_TARGET', 'Away'),
}
LABELS = {'GOALS': 'Goals', 'CORNERS': 'Corners', 'CARDS': 'Cards',
          'SHOTS': 'Shots', 'TARGET': 'Shots on Target'}


class ProviderStop(RuntimeError):
    pass


def utc(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def norm(value):
    text = unicodedata.normalize('NFKD', str(value or '')).casefold()
    return re.sub('[^a-z0-9]', '', ''.join(c for c in text if not unicodedata.combining(c)))


def load(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).is_file() else (default if default is not None else {})


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


def logo_id(value):
    match = re.fullmatch(r'https://media\.api-sports\.io/football/teams/(\d+)\.png', str(value or ''))
    return match.group(1) if match else None


class FixtureIndex:
    def __init__(self, root, policy, now):
        self.by_code = defaultdict(dict)
        self.policy = {r['code']:dict(r, leagueId=int(k)) for k,r in policy['leagueIds'].items()}
        for path in (root/'data/api_football/fixture_stats').glob('*/*/*/fixture_stats.json'):
            doc = load(path)
            meta = policy['leagueIds'].get(str(doc.get('league_id')))
            if doc.get('provider') != 'api-football' or not meta or str(doc.get('season')) != str(meta['season']):
                continue
            for row in doc.get('schedule_fixtures', []):
                source = row.get('source_league', {})
                if (str(source.get('id')) != str(doc['league_id']) or str(source.get('season')) != str(meta['season'])
                        or row.get('fixture_query_used') != 'league+season:'+str(meta['season'])):
                    continue
                self.add(meta['code'], dict(id=row.get('fixture_id'), kickoff=row.get('date'), status=row.get('status'),
                    home=row.get('home_team'), away=row.get('away_team'), homeId=row.get('home_team_id'), awayId=row.get('away_team_id')), now)
        for code in ('CL','EL','UECL'):
            meta = self.policy.get(code)
            if not meta:
                continue
            doc = load(root/f'data/api_football/league_tables/{code}_{meta["season"]}_fixtures.json')
            for row in doc.get('response', []):
                league, fixture, teams = row.get('league', {}), row.get('fixture', {}), row.get('teams', {})
                if league.get('id') != meta['leagueId'] or str(league.get('season')) != str(meta['season']):
                    continue
                self.add(code, dict(id=fixture.get('id'), kickoff=fixture.get('date'), status=fixture.get('status',{}).get('short'),
                    home=teams.get('home',{}).get('name'), away=teams.get('away',{}).get('name'),
                    homeId=teams.get('home',{}).get('id'), awayId=teams.get('away',{}).get('id')), now)

    def add(self, code, row, now):
        start = utc(row['kickoff'])
        if row.get('id') and row['status'] == 'NS' and start and now < start <= now+timedelta(days=14):
            row = {**row, 'leagueId':self.policy[code]['leagueId'], 'season':self.policy[code]['season']}
            old = self.by_code[code].get(str(row['id']))
            self.by_code[code][str(row['id'])] = row if old is None or old == row else {'invalid':True}

    def resolve(self, code, match):
        start = utc(match.get('kickoff'))
        if not start:
            return None
        home_ids, away_ids = logo_id(match.get('homeTeamLogo')), logo_id(match.get('awayTeamLogo'))
        homes = {norm(match.get(k)) for k in ('canonicalHomeTeam','homeTeam','providerHomeTeam')} - {''}
        aways = {norm(match.get(k)) for k in ('canonicalAwayTeam','awayTeam','providerAwayTeam')} - {''}
        found = []
        for row in self.by_code.get(code, {}).values():
            if row.get('invalid') or abs((utc(row['kickoff'])-start).total_seconds()) > 900:
                continue
            # Both numeric identities must agree when present; names alone cannot override a mismatch.
            if home_ids and away_ids:
                identity = str(row.get('homeId')) == home_ids and str(row.get('awayId')) == away_ids
            else:
                identity = norm(row['home']) in homes and norm(row['away']) in aways
            if identity:
                found.append(row)
        return found[0] if len(found) == 1 else None


def normalize_bet(bet, book, match, fixture, observed, updated):
    spec = MARKETS.get(bet.get('id'))
    if not spec or norm(bet.get('name')) != norm(spec[0]):
        return []
    name, family, side = spec
    home, away = match.get('homeTeam'), match.get('awayTeam')
    team = home if side == 'Home' else away if side == 'Away' else None
    result = []
    for item in bet.get('values', []):
        try:
            price = float(item.get('odd'))
        except (TypeError,ValueError):
            continue
        if not math.isfinite(price) or price <= 1:
            continue
        selection, line = str(item.get('value','')), None
        fixed = {'1X2':{'Home','Draw','Away'}, 'BTTS':{'Yes','No'},
                 'DOUBLE_CHANCE':{'Home/Draw','Home/Away','Draw/Away'},
                 'HALF_TIME_DOUBLE_CHANCE':{'Home/Draw','Home/Away','Draw/Away'}}
        if family in fixed:
            if selection not in fixed[family]:
                continue
            selection = {'Home/Draw':'1X','Home/Away':'12','Draw/Away':'X2'}.get(selection,selection)
        else:
            pair = re.fullmatch(r'(Over|Under)\s+(\d+(?:\.\d+)?)',selection)
            if not pair:
                continue
            direction, line = pair.group(1), float(pair.group(2))
            is_half = abs(line % 1 - .5) < 1e-9
            if not is_half and not ('CORNERS' in family and line.is_integer()):
                continue
            metric = 'Shots on Target' if 'TARGET' in family else 'Shots' if 'SHOTS' in family else 'Corners' if 'CORNERS' in family else 'Cards' if 'CARDS' in family else 'Goals'
            selection = f'{team} {metric} {direction} {line:g}' if team else direction if family in ('MATCH_GOALS','FIRST_HALF_GOALS','SECOND_HALF_GOALS') else f'{metric} {direction} {line:g}'
        quote = dict(market=family, selection=selection, odds=price, bookmaker=book,
            exactBookmakerOdds=True, confidence='high', oddsProvider='api-football',
            providerMarketName=name, providerMarketFamily=family, providerMarketId=bet['id'],
            apiFootballFixtureId=str(fixture['id']), oddsObservedAt=observed, oddsUpdatedAt=updated)
        if line is not None:quote['line']=line
        if team:quote['team']=team
        result.append(quote)
    return result


def group_key(quote):
    return quote.get('market'), quote.get('team') if str(quote.get('market','')).startswith('TEAM_') else None


def choose(primary, fallback):
    groups = defaultdict(lambda:defaultdict(list))
    for quote in primary:
        groups[group_key(quote)][quote['bookmaker']].append(quote)
    selected = {}
    for key, books in groups.items():
        family = key[0]
        validation = 'DOUBLE_CHANCE' if family == 'HALF_TIME_DOUBLE_CHANCE' else 'MATCH_GOALS' if family in ('SECOND_HALF_GOALS','TEAM_FIRST_HALF_GOALS','TEAM_SECOND_HALF_GOALS') else family
        _, quotes, _ = _choose_bookmaker(validation,books,BOOKMAKERS)
        if quotes:selected[key]=quotes
    by_fallback = defaultdict(lambda:defaultdict(list))
    for quote in fallback:
        if str(quote.get('bookmaker','')).casefold() == 'unibet' and quote.get('oddsProvider') != 'api-football':
            by_fallback[group_key(quote)]['Unibet'].append({**quote,'bookmaker':'Unibet','oddsProvider':'odds-api-io'})
    for key, books in by_fallback.items():
        if key in selected:continue
        family=key[0]
        validation='DOUBLE_CHANCE' if family=='HALF_TIME_DOUBLE_CHANCE' else 'MATCH_GOALS' if family in ('SECOND_HALF_GOALS','TEAM_FIRST_HALF_GOALS','TEAM_SECOND_HALF_GOALS') else family
        _, quotes, _ = _choose_bookmaker(validation,books,['Unibet'])
        if quotes:selected[key]=quotes
    return [q for key in sorted(selected,key=str) for q in selected[key]]


def cached_primary(match, previous, fixture, now):
    if not fixture:return []
    result=[]
    for source in (match, previous or {}):
        if utc(source.get('kickoff')) != utc(match.get('kickoff')):continue
        for quote in source.get('markets',[]):
            observed=utc(quote.get('oddsObservedAt'))
            if (quote.get('oddsProvider')=='api-football' and str(quote.get('apiFootballFixtureId'))==str(fixture['id'])
                    and observed and timedelta(0)<=now-observed<=timedelta(hours=MAX_CACHED_HOURS)):
                result.append(quote)
    return result


def refresh(feeds, previous_feeds, index, state, key, now, max_requests=96, opener=urlopen):
    report=dict(contract=CONTRACT,generatedAt=now.isoformat(),apiCalls=0,matchedFixtures=0,unmatchedFixtures=0,primaryQuotes=0,fallbackQuotes=0,errors=[])
    targets=defaultdict(list)
    for feed,previous in zip(feeds,previous_feeds):
        old_groups=previous.get('leagues') or [previous]
        old={(g.get('leagueCode') or previous.get('leagueCode'),str(m['id'])):m for g in old_groups for m in g.get('matches',[])}
        for league in feed.get('leagues') or [feed]:
            code=league.get('leagueCode') or feed.get('leagueCode')
            for match in league.get('matches',[]):
                start=utc(match.get('kickoff'))
                if not start or start<=now:continue
                fixture=index.resolve(code,match)
                primary=cached_primary(match,old.get((code,str(match.get('id')))),fixture,now)
                match['markets']=choose(primary,match.get('markets',[]))
                if fixture:
                    match['apiFootballFixtureId']=str(fixture['id'])
                    report['matchedFixtures']+=1
                    targets[(code,str(fixture['kickoff'])[:10])].append((match,fixture,primary))
                else:report['unmatchedFixtures']+=1
    groups=state.setdefault('groups',{})
    requests=[]
    for (code,day),matches in targets.items():
        stamp=utc(groups.get(code+'/'+day,{}).get('fetchedAt'))
        if not stamp or not timedelta(0)<=now-stamp<timedelta(hours=3):
            requests.append((code,day,matches))
    cursor=int(state.get('cursor',0))
    priority=[x for x in requests if index.policy[x[0]]['tier']=='primary']
    rest=sorted([x for x in requests if index.policy[x[0]]['tier']!='primary'],key=lambda x:(index.policy[x[0]]['tier']!='europe',x[1],x[0]))
    if rest:rest=rest[cursor%len(rest):]+rest[:cursor%len(rest)]
    processed=0
    try:
        for code,day,matches in sorted(priority,key=lambda x:(x[1],x[0]))+rest:
            if report['apiCalls']>=max_requests:break
            meta=index.policy[code];found={};complete=True;page=1
            while True:
                if report['apiCalls']>=max_requests:complete=False;break
                params=dict(league=meta['leagueId'],season=meta['season'],date=day,page=page)
                request=Request('https://v3.football.api-sports.io/odds?'+urlencode(params),headers={'x-apisports-key':key,'Accept-Encoding':'gzip'})
                report['apiCalls']+=1
                try:
                    with opener(request,timeout=35) as response:
                        raw=response.read()
                        if response.headers.get('Content-Encoding')=='gzip':raw=gzip.decompress(raw)
                        payload=json.loads(raw)
                    received_at=datetime.now(timezone.utc)
                    if payload.get('errors'):raise ValueError('Provider errors')
                    for row in payload.get('response',[]):
                        if row.get('league',{}).get('id')!=meta['leagueId'] or str(row.get('league',{}).get('season'))!=str(meta['season']):continue
                        found[str(row.get('fixture',{}).get('id'))]={**row,'_statmakerObservedAt':received_at.isoformat()}
                    total=int(payload.get('paging',{}).get('total',1))
                    if page>=total:break
                    page+=1
                except Exception as exc:
                    report['errors'].append(dict(code=code,date=day,errorType=type(exc).__name__,httpStatus=getattr(exc,'code',None)))
                    if isinstance(exc,HTTPError) and exc.code in (401,403,429):
                        raise ProviderStop(str(exc.code)) from None
                    complete=False;break
                finally:time.sleep(.25)
            if not complete:continue
            groups[code+'/'+day]=dict(fetchedAt=received_at.isoformat(),fixtures=len(found))
            processed+=1
            for match,fixture,old_quotes in matches:
                row=found.get(str(fixture['id']))
                if not row:
                    match['markets']=choose([],match.get('markets',[]))
                    continue
                if utc(row.get('fixture',{}).get('date')) != utc(fixture['kickoff']):continue
                if utc(row['_statmakerObservedAt'])>=utc(fixture['kickoff']):
                    match['markets']=[]
                    continue
                fresh=[]
                for book in row.get('bookmakers',[]):
                    if book.get('name') not in BOOKMAKERS:continue
                    for bet in book.get('bets',[]):
                        fresh.extend(normalize_bet(bet,book['name'],match,fixture,row['_statmakerObservedAt'],row.get('update')))
                # A successful response replaces prior quotes, including withdrawn markets.
                match['markets']=choose(fresh,match.get('markets',[]))
    except BaseException as exc:
        if type(exc).__name__!='BudgetStop' and not isinstance(exc,ProviderStop):raise
        report['errors'].append(dict(errorType=type(exc).__name__,cachedQuotesPreserved=True))
    if rest:state['cursor']=(cursor+max(1,processed-len(priority)))%len(rest)
    state['contract']=CONTRACT
    state['groups']={k:v for k,v in groups.items() if k.rsplit('/',1)[-1]>=now.date().isoformat()}
    for feed in feeds:
        feed['bookmakersRequested']=['Bet365','Unibet']
        feed['oddsProviderPolicy']=CONTRACT
        for league in feed.get('leagues') or [feed]:
            for match in league.get('matches',[]):
                for q in match.get('markets',[]):
                    if q.get('oddsProvider')=='api-football':report['primaryQuotes']+=1
                    elif q.get('bookmaker')=='Unibet':report['fallbackQuotes']+=1
    return report


def restore_archived_unibet(feed, archive, now):
    """Recover existing exact Unibet alternatives discarded by earlier bookmaker selection."""
    import update_domestic_odds_api_io as odds
    import domestic_market_expansion_v18
    from odds_market_integrity import install_parser_guard
    domestic_market_expansion_v18.install(odds)
    install_parser_guard(odds)
    by_event={(l.get('leagueCode'),str(m.get('id'))):m for l in archive.get('leagues',[]) for m in l.get('matches',[])}
    restored=0
    for league in feed.get('leagues',[]):
        for match in league.get('matches',[]):
            previous=by_event.get((league.get('leagueCode'),str(match.get('id'))))
            if not previous or utc(previous.get('kickoff')) != utc(match.get('kickoff')):continue
            if any(norm(previous.get(k)) != norm(match.get(k)) for k in ('homeTeam','awayTeam')):continue
            exact=[]
            for payload in previous.get('providerMarkets',[]):
                raw=payload.get('market',{});stamp=utc(raw.get('updatedAt'))
                if (payload.get('exactProviderPayload') is not True or payload.get('bookmaker')!='Unibet'
                        or not stamp or not timedelta(0)<=now-stamp<=timedelta(hours=72)):continue
                rows=odds.normalize_market(raw,'Unibet',match.get('homeTeam'),match.get('awayTeam'),{})
                exact.extend({**q,'oddsProvider':'odds-api-io','oddsUpdatedAt':stamp.isoformat()} for q in rows if not str(q.get('market','')).startswith('ASIAN'))
            # Current response wins over an archived version of the same family.
            current_keys={group_key(q) for q in match.get('markets',[]) if q.get('bookmaker')=='Unibet'}
            exact=[q for q in exact if group_key(q) not in current_keys]
            match.setdefault('markets',[]).extend(exact);restored+=len(exact)
    return restored


def main():
    args=argparse.ArgumentParser()
    args.add_argument('--feeds',nargs='+',required=True)
    args.add_argument('--previous-dir',type=Path)
    args.add_argument('--previous',type=Path)
    args.add_argument('--fixture-root',type=Path,default=Path('.'))
    args.add_argument('--policy',type=Path,default=Path('config/api_football_refresh_policy.json'))
    args.add_argument('--state',type=Path,default=Path('data/api_football/odds_refresh_state.json'))
    args.add_argument('--report',type=Path,default=Path('reports/primary_odds_refresh.json'))
    args.add_argument('--max-requests',type=int,default=96)
    args.add_argument('--offline',action='store_true')
    args.add_argument('--archive',type=Path)
    args=args.parse_args()
    now=datetime.now(timezone.utc);policy=load(args.policy);state=load(args.state)
    feeds=[load(path) for path in args.feeds]
    if args.archive:
        for feed in feeds:restore_archived_unibet(feed,load(args.archive),now)
    previous=[load(args.previous) if args.previous else load(args.previous_dir/Path(path).name) if args.previous_dir else {} for path in args.feeds]
    index=FixtureIndex(args.fixture_root,policy,now)
    key=os.getenv('API_FOOTBALL_KEY','')
    report=refresh(feeds,previous,index,state,key,now,max_requests=0 if args.offline or not key else max(0,min(120,args.max_requests)))
    report['offline']=args.offline or not bool(key)
    for path,feed in zip(args.feeds,feeds):write(path,feed)
    write(args.state,state);write(args.report,report)
    print(json.dumps(report))


if __name__=='__main__':main()
