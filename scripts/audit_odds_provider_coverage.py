"""Bounded read-only comparison of paid API-Football and existing free Unibet."""
from __future__ import annotations

from collections import Counter
import argparse
from datetime import datetime, timedelta, timezone
import gzip
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import update_domestic_odds_api_io as parser

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'reports/odds_provider_coverage_audit.json'
APIF_LIMIT = 200
UNIBET_LIMIT = 12
FAMILIES = ('1X2', 'DOUBLE_CHANCE', 'BTTS', 'MATCH_GOALS', 'TEAM_TOTAL_GOALS',
            'MATCH_CORNERS', 'TEAM_CORNERS', 'MATCH_CARDS', 'TEAM_CARDS',
            'MATCH_SHOTS', 'TEAM_SHOTS', 'MATCH_SHOTS_ON_TARGET', 'TEAM_SHOTS_ON_TARGET')


class StopAudit(Exception):
    pass


def utc(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def valid_price(value):
    try:
        number = float(value)
        return math.isfinite(number) and number > 1
    except (TypeError, ValueError):
        return False


def api_family(name):
    """Exact known full-time contracts; unknown names remain visible for review."""
    name = re.sub(r'[^a-z0-9]', '', str(name).lower())
    groups = {
        '1X2': ('matchwinner',), 'DOUBLE_CHANCE': ('doublechance',),
        'BTTS': ('bothteamsscore',), 'MATCH_GOALS': ('goalsoverunder',),
        'TEAM_TOTAL_GOALS': ('totalhome', 'totalaway'),
        'MATCH_CORNERS': ('cornersoverunder', 'totalcorners'),
        'TEAM_CORNERS': ('homecornersoverunder', 'awaycornersoverunder'),
        'MATCH_CARDS': ('cardsoverunder',),
        'TEAM_CARDS': ('hometeamtotalcards', 'awayteamtotalcards', 'hometeamyellowcards', 'awayteamyellowcards'),
        'MATCH_SHOTS': ('totalshots',), 'TEAM_SHOTS': ('shotshometotal', 'shotsawaytotal'),
        'MATCH_SHOTS_ON_TARGET': ('totalshotongoal', 'totalshotsontarget'),
        'TEAM_SHOTS_ON_TARGET': ('hometotalshotongoal', 'awaytotalshotongoal', 'homeshotsontarget', 'awayshotsontarget'),
    }
    return next((family for family, names in groups.items() if name in names), None)


def summarize_book(book):
    markets = [m for m in book.get('bets', []) if isinstance(m, dict)]
    priced = [m for m in markets if any(valid_price(v.get('odd')) for v in m.get('values', []) if isinstance(v, dict))]
    names = sorted({str(m.get('name', '')) for m in priced})
    return dict(bookmaker=book.get('name'), bookmakerId=book.get('id'),
                families=sorted({api_family(n) for n in names if api_family(n)}),
                pricedMarketNames=names,
                countMarketExamples=[dict(name=m.get('name'), id=m.get('id'), values=m.get('values', [])[:3])
                                     for m in priced if any(word in str(m.get('name', '')).lower() for word in ('shot', 'card', 'corner'))])


class Client:
    def __init__(self, api_key, unibet_key, report, opener=urlopen):
        self.api_key, self.unibet_key = api_key, unibet_key
        self.report, self.opener = report, opener

    def get(self, provider, path, params=None):
        counter = self.report['requests'][provider]
        limit = APIF_LIMIT if provider == 'apiFootball' else UNIBET_LIMIT
        if counter >= limit:
            raise StopAudit(provider + ' explicit request limit reached')
        headers = {'Accept-Encoding': 'gzip', 'User-Agent': 'StatMaker-read-only-coverage-audit'}
        params = dict(params or {})
        if provider == 'apiFootball':
            base = 'https://v3.football.api-sports.io'
            headers['x-apisports-key'] = self.api_key
        else:
            base = 'https://api.odds-api.io/v3'
            params['apiKey'] = self.unibet_key
        self.report['requests'][provider] += 1
        try:
            request = Request(base + '/' + path.lstrip('/') + '?' + urlencode(params), headers=headers)
            with self.opener(request, timeout=35) as response:
                raw = response.read()
                if response.headers.get('Content-Encoding', '').lower() == 'gzip':
                    raw = gzip.decompress(raw)
                payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get('errors'):
                self.report['errors'].append(dict(provider=provider, path=path, reason='provider returned errors'))
                return None
            return payload
        except HTTPError as exc:
            self.report['errors'].append(dict(provider=provider, path=path, httpStatus=exc.code))
            if exc.code in (401, 403, 429):
                raise StopAudit(provider + ' HTTP ' + str(exc.code)) from None
            return None
        except Exception as exc:
            self.report['errors'].append(dict(provider=provider, path=path, exceptionType=type(exc).__name__))
            return None
        finally:
            time.sleep(.25)


def rows(payload):
    return [r for r in (payload or {}).get('response', []) if isinstance(r, dict)]


def fixtures_in_window(payload, league_id, season, now):
    result = []
    for row in rows(payload):
        fixture, league = row.get('fixture', {}), row.get('league', {})
        start = utc(fixture.get('date'))
        if (str(league.get('id')) != str(league_id) or str(league.get('season')) != str(season)
                or not start or not now < start <= now + timedelta(days=14)
                or fixture.get('status', {}).get('short') != 'NS'):
            continue
        result.append(row)
    return sorted(result, key=lambda r: r['fixture']['date'])


def probe_api(client, report, policy, now):
    catalogue = client.get('apiFootball', 'odds/bets')
    report['apiFootballCatalogue'] = rows(catalogue)
    ordered = sorted(policy['leagueIds'].items(), key=lambda item: (item[1]['tier'] != 'primary', item[1]['code']))
    for league_id, meta in ordered:
        scope = dict(leagueCode=meta['code'], leagueId=int(league_id), season=meta['season'], fixtures=[])
        report['apiFootballScopes'].append(scope)
        payload = client.get('apiFootball', 'fixtures', dict(league=league_id, season=meta['season'], next=8, timezone='UTC'))
        if payload is None:
            scope['status'] = 'REQUEST_ERROR'
            continue
        fixtures = fixtures_in_window(payload, league_id, meta['season'], now)
        scope['upcomingFixtureCount'] = len(fixtures)
        scope['status'] = 'NO_UPCOMING_FIXTURE' if not fixtures else 'TESTED'
        for fixture in fixtures[:3 if meta['tier'] == 'primary' else 1]:
            fid = fixture['fixture']['id']
            item = dict(fixtureId=fid, kickoff=fixture['fixture']['date'], teams=fixture.get('teams'), bookmakers=[])
            scope['fixtures'].append(item)
            odds = client.get('apiFootball', 'odds', dict(fixture=fid))
            item['requestSucceeded'] = odds is not None
            item['paging'] = (odds or {}).get('paging')
            for row in rows(odds):
                if row.get('fixture', {}).get('id') != fid:
                    report['errors'].append(dict(provider='apiFootball', reason='fixture identity mismatch', fixtureId=fid))
                    continue
                item['bookmakers'].extend(summarize_book(b) for b in row.get('bookmakers', []) if isinstance(b, dict))


def feed_events(feed, now):
    groups = feed.get('leagues') if isinstance(feed.get('leagues'), list) else [feed]
    result = []
    for group in groups:
        future = [m for m in group.get('matches', [])
                  if str(m.get('scheduleSource', '')).lower() != 'api-football'
                  and m.get('teamMappingStatus') != 'schedule_only_api_football'
                  and utc(m.get('kickoff')) and now < utc(m['kickoff']) <= now + timedelta(days=14)]
        for match in sorted(future, key=lambda m: m['kickoff'])[:2]:
            if str(match.get('id', '')).isdigit():
                result.append(dict(eventId=str(match['id']), leagueCode=group.get('leagueCode') or feed.get('leagueCode'), kickoff=match['kickoff']))
    return result


def probe_unibet(client, report, now):
    events = feed_events(json.loads((ROOT/'odds/odds_api_io/domestic_odds.json').read_text()), now)
    for name in ('champions_league', 'europa_league', 'conference_league'):
        try:
            url = 'https://raw.githubusercontent.com/Velliouras/StatMaker-Data/build/uefa-qualifier-feed-20260720/odds/odds_api_io/' + name + '_odds.json'
            with urlopen(url, timeout=30) as response:
                events.extend(feed_events(json.loads(response.read()), now))
        except Exception as exc:
            report['errors'].append(dict(provider='cachedUefaFeed', feed=name, exceptionType=type(exc).__name__))
    unique = {e['eventId']: e for e in events}
    selected = sorted(unique.values(), key=lambda e: (e.get('leagueCode') not in ('CL', 'EL', 'UECL'), e['kickoff']))[:120]
    report['unibetRequestedEvents'] = selected
    for offset in range(0, len(selected), 10):
        batch = selected[offset:offset+10]
        ids = {e['eventId']: e for e in batch}
        payload = client.get('unibet', 'odds/multi', dict(eventIds=','.join(ids), bookmakers='Unibet'))
        if payload is None:
            continue
        for row in payload if isinstance(payload, list) else [payload]:
            if str(row.get('id')) not in ids:
                continue
            for book, markets in parser.bookmaker_blocks(row):
                if book.lower() != 'unibet':
                    continue
                normalized = []
                for market in markets:
                    normalized.extend(parser.normalize_market(market, book, str(row.get('home', '')), str(row.get('away', '')), {}))
                report['unibetFixtures'].append(dict(**ids[str(row['id'])], home=row.get('home'), away=row.get('away'),
                    pricedFamilies=sorted({m['market'] for m in normalized}),
                    rawMarketNames=sorted({str(m.get('name', '')) for m in markets})))


def summary(report):
    api = [f for s in report['apiFootballScopes'] for f in s['fixtures']]
    counts = Counter(family for f in api for family in {x for b in f['bookmakers'] for x in b['families']})
    bet365 = Counter(family for f in api for family in {x for b in f['bookmakers'] if str(b['bookmaker']).lower() == 'bet365' for x in b['families']})
    unibet = Counter(family for f in report['unibetFixtures'] for family in f['pricedFamilies'])
    return dict(apiFootballScopes=len(report['apiFootballScopes']), apiFootballFixtures=len(api),
                apiFootballFixturesWithOdds=sum(any(b['pricedMarketNames'] for b in f['bookmakers']) for f in api),
                apiFootballAnyBookmaker={f:counts[f] for f in FAMILIES}, apiFootballBet365={f:bet365[f] for f in FAMILIES},
                unibetRequestedEvents=len(report.get('unibetRequestedEvents', [])), unibetReturnedFixtures=len(report['unibetFixtures']),
                unibetPricedFamilies={f:unibet[f] for f in FAMILIES})


def main():
    args = argparse.ArgumentParser()
    args.add_argument('--unibet-only', action='store_true')
    args = args.parse_args()
    now = datetime.now(timezone.utc)
    report = dict(schemaVersion=1, generatedAt=now.isoformat(), productionProviderChanged=False,
                  sampleOnly=True, requests=dict(apiFootball=0, unibet=0), requestLimits=dict(apiFootball=APIF_LIMIT, unibet=UNIBET_LIMIT),
                  apiFootballScopes=[], unibetFixtures=[], errors=[], completed={})
    if args.unibet_only:
        previous = json.loads(OUTPUT.read_text())
        if previous.get('schemaVersion') != 1 or previous.get('completed', {}).get('apiFootball') is not True:
            raise ValueError('A complete prior API-Football audit is required')
        report.update(apiFootballScopes=previous['apiFootballScopes'],
                      apiFootballCatalogue=previous.get('apiFootballCatalogue', []),
                      retainedApiFootballEvidence=dict(generatedAt=previous['generatedAt'], requests=previous['requests']['apiFootball']))
        report['completed']['apiFootball'] = True
    api_key, unibet_key = os.getenv('API_FOOTBALL_KEY', ''), os.getenv('ODDS_API_IO_KEY', '')
    client = Client(api_key, unibet_key, report)
    policy = json.loads((ROOT/'config/api_football_refresh_policy.json').read_text())
    report['configuredScopeCount'] = len(policy['leagueIds'])
    for name, key, task in [('apiFootball', api_key, lambda:probe_api(client, report, policy, now)),
                            ('unibet', unibet_key, lambda:probe_unibet(client, report, now))]:
        if args.unibet_only and name == 'apiFootball':
            continue
        try:
            if not key:
                raise StopAudit('credential unavailable')
            task()
            report['completed'][name] = True
        except BaseException as exc:
            if not isinstance(exc, StopAudit) and type(exc).__name__ != 'BudgetStop':
                raise
            report['completed'][name] = False
            report['errors'].append(dict(provider=name, reason=type(exc).__name__))
    report['summary'] = summary(report)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    for key in (api_key, unibet_key):
        if key:
            rendered = rendered.replace(key, '[REDACTED]')
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered+'\n')
    print(json.dumps(dict(summary=report['summary'], requests=report['requests'], completed=report['completed'], errors=report['errors']), ensure_ascii=False))


if __name__ == '__main__':
    main()
