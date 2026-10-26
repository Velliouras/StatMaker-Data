import json, os, gzip
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from collections import Counter
import sys
sys.path.insert(0, 'scripts')
import update_domestic_odds_api_io as parser
now = datetime.now(timezone.utc)
feeds = [json.loads(Path('odds/odds_api_io/domestic_odds.json').read_text())]
for name in ('champions_league', 'europa_league', 'conference_league'):
    url = 'https://raw.githubusercontent.com/Velliouras/StatMaker-Data/build/uefa-qualifier-feed-20260720/odds/odds_api_io/' + name + '_odds.json'
    with urlopen(url, timeout=30) as response:
        feeds.append(json.loads(response.read()))
events = {}
for feed in feeds:
    for league in feed.get('leagues', []):
        future = [m for m in league.get('matches', []) if str(m.get('kickoff', '')) > now.isoformat().replace('+00:00', 'Z')]
        for match in sorted(future, key=lambda m:m.get('kickoff',''))[:2]:
            events[str(match['id'])] = league.get('leagueCode')
    for match in feed.get('matches', []):
        if str(match.get('kickoff', '')) > now.isoformat().replace('+00:00', 'Z'):
            events.setdefault(str(match['id']), feed.get('leagueCode', 'UEFA'))
events = dict(list(events.items())[:120])
report = dict(generatedAt=now.isoformat(), productionProviderChanged=False, requestedBookmakers=['Unibet'], eventsRequested=len(events), apiCalls=0, fixtures=[], errors=[])
ids = list(events)
key = os.environ['ODDS_API_IO_KEY']
for offset in range(0, len(ids), 10):
    chunk = ids[offset:offset+10]
    params = dict(apiKey=key, eventIds=','.join(chunk), bookmakers='Unibet')
    request = Request('https://api.odds-api.io/v3/odds/multi?' + urlencode(params), headers={'Accept-Encoding':'gzip'})
    report['apiCalls'] += 1
    try:
        with urlopen(request, timeout=45) as response:
            raw = response.read()
            if response.headers.get('Content-Encoding') == 'gzip': raw = gzip.decompress(raw)
            rows = json.loads(raw)
        if not isinstance(rows, list): rows = [rows] if isinstance(rows, dict) else []
        for row in rows:
            for book, markets in parser.bookmaker_blocks(row):
                if book.lower() != 'unibet': continue
                normalized = []
                for market in markets:
                    normalized.extend(parser.normalize_market(market, book, str(row.get('home','')), str(row.get('away','')), {}))
                report['fixtures'].append(dict(eventId=row.get('id'), leagueCode=events.get(str(row.get('id'))), home=row.get('home'), away=row.get('away'), rawMarketNames=sorted({m.get('name','') for m in markets}), normalizedFamilies=dict(Counter(m['market'] for m in normalized))))
    except Exception as exc:
        report['errors'].append(dict(batch=offset//10, exceptionType=type(exc).__name__, httpStatus=getattr(exc,'code',None)))
report['familyFixtureCounts'] = dict(Counter(family for row in report['fixtures'] for family in row['normalizedFamilies']))
Path('reports/unibet_free_coverage_audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='fixtures'}, ensure_ascii=False))
