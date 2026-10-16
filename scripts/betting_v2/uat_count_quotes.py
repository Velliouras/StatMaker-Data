"""Add saved, exact-identity foul prices to a separate UAT quote table.

The legacy quote table is copied verbatim and never mutated. New prices must
have a named bookmaker, a prematch receipt within 24 hours, and an exact
provider event ID + kickoff match to an already approved prepared fixture.
"""
from collections import Counter,defaultdict
import gzip
import json
from pathlib import Path
from .research_scope_expansion import foul_or_legacy_spec
from .index_quote_receipts import extracted_offers
from .opponent_matchup import timestamp

TABLE='prepared_betting_v4_quotes'


def stage(con,root,now):
    con.execute('DROP TABLE IF EXISTS '+TABLE)
    con.execute('CREATE TABLE '+TABLE+' AS SELECT * FROM prepared_betting_v2_quotes')
    con.execute('CREATE UNIQUE INDEX idx_betting_v4_quotes_scope ON '+TABLE+'(competition_id,snapshot_version,match_key,ordinal)')
    columns=[r[1] for r in con.execute('PRAGMA table_info('+TABLE+')')]
    fixtures=defaultdict(list)
    for f in con.execute('SELECT * FROM prepared_fixture_matches WHERE usable_for_stats=1'):
        f=dict(f);ko=timestamp(f.get('kickoff') or f.get('date'))
        if str(f.get('id') or '') and ko>now:fixtures[str(f['id']),ko].append(f)
    packets=defaultdict(dict);observed={};counters=Counter()
    for path in sorted((Path(root)/'reports/betting_v2/quote_receipts').glob('*.gz')):
        try:doc=json.loads(gzip.decompress(path.read_bytes()));offers,_=extracted_offers(doc)
        except (ValueError,KeyError,TypeError,OSError,EOFError):continue
        for q in offers:
            if 'foul' not in q['providerMarketName'].casefold():continue
            obs=timestamp(q['clientReceivedAtUTC']);ko=timestamp(q['kickoffUTC'])
            if not now-86400<=obs<=now<ko:continue
            fs=fixtures.get((str(q['providerEventId']),ko),[])
            for f in fs:
                event=dict(home=f.get('provider_home_team') or f['home_team'],away=f.get('provider_away_team') or f['away_team'])
                spec=foul_or_legacy_spec(q,event)
                if spec is None or spec[0]!='FOULS':counters['unsupportedFoulContract']+=1;continue
                key=f['competition_id'],f['snapshot_version'],f['match_key'],path.name
                identity=tuple(spec),q['bookmaker'];prior=packets[key].get(identity)
                value=dict(f,market='TEAM_FOULS' if spec[1]=='TEAM' else 'MATCH_FOULS',
                    selection=spec[2].title(),team=(f['home_team'] if spec[3]=='HOME' else f['away_team']) if spec[1]=='TEAM' else None,
                    line=spec[4],odd=q['odd'],bookmaker=q['bookmaker'],provider_market_name=q['providerMarketName'],
                    provider_market_family='TEAM_FOULS' if spec[1]=='TEAM' else 'MATCH_FOULS')
                if prior and (prior.get('ambiguous') or prior['odd']!=value['odd']):value['ambiguous']=True
                packets[key][identity]=value
                observed[key]=obs
    normalized={}
    for key,rows in packets.items():
        selected={}
        for (identity,book),value in rows.items():
            if value.get('ambiguous'):counters['ambiguousBookmakerQuote']+=1;continue
            prior=selected.get(identity)
            if prior is None or (value['odd'],book)>(prior['odd'],prior['bookmaker']):selected[identity]=value
        normalized[key]=selected
    packets=normalized
    latest={}
    for key,rows in packets.items():
        fixture=key[:3]
        if fixture not in latest or (observed[key],key[3])>(observed[latest[fixture]],latest[fixture][3]):latest[fixture]=key
    added=[]
    for fixture,key in sorted(latest.items()):
        ordinal=con.execute('SELECT coalesce(max(ordinal),-1)+1 FROM '+TABLE+' WHERE competition_id=? AND snapshot_version=? AND match_key=?',fixture).fetchone()[0]
        for identity,value in sorted(packets[key].items(),key=lambda item:repr(item[0])):
            duplicate=con.execute('SELECT 1 FROM '+TABLE+' WHERE competition_id=? AND snapshot_version=? AND match_key=? AND market=? AND selection=? AND coalesce(team,\'\')=? AND line=?',
                (*fixture,value['market'],value['selection'],value['team'] or '',value['line'])).fetchone()
            if duplicate:continue
            value['ordinal']=ordinal;ordinal+=1;added.append(tuple(value.get(c) for c in columns))
    con.executemany('INSERT INTO '+TABLE+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',added)
    return dict(addedFoulQuotes=len(added),addedFoulFixtures=len(latest),quoteTable=TABLE,providerCalls=0,
        rule='Exact provider ID/kickoff, approved fixture, latest single prematch receipt <=24h',**dict(counters))
