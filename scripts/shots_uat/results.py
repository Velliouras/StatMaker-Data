"""Exact post-match labels from existing repository caches, separate from model history."""
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from .features import number

SOURCES=('data/statmaker/uefa_current_stats.json','data/statmaker/live_settlements.json')

def stamp(value):
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (TypeError,ValueError):return 0.

@lru_cache(maxsize=8)
def cached_rows(signature):
    rows=[]
    for name,modified,size in signature:
        if not modified:continue
        doc=json.loads(Path(name).read_text());observed=stamp(doc.get('generatedAt'))
        fixtures=list(doc.get('fixtures',[]))
        fixtures += [f for competition in doc.get('competitions',[]) for f in competition.get('fixtures',[])]
        for f in fixtures:
            # Counts for AET/PEN can include extra time. Do not substitute them for FT props.
            if f.get('status')!='FT':continue
            try:api=int(f.get('fixtureId',0))
            except (TypeError,ValueError):continue
            ko=stamp(f.get('kickoff') or f.get('dateUtc'))
            if api<=0 or not ko:continue
            rows.append(dict(fixture_id=api,_date=ko,normalized_stats=dict(f.get('normalizedStats') or {}),
                home_team=f.get('homeTeam'),away_team=f.get('awayTeam'),
                home_goals=f.get('homeGoals'),away_goals=f.get('awayGoals'),
                hthg=f.get('homeHalfGoals'),htag=f.get('awayHalfGoals'),
                _settlementSource='CACHED_EXACT_FIXTURE:'+Path(name).name,_evidenceAt=observed))
    return rows

def fixture_matches(issue, row):
    """Bind the exact event; allow a verified same-day one-hour clock correction."""
    if str(row.get('fixture_id')) != str(issue['apiFixtureId']):return False
    scheduled,played=stamp(issue['kickoff']),row.get('_date',0)
    if not scheduled or not played:return False
    difference=abs(played-scheduled)
    if difference<=300:return True
    # A different day, arbitrary delay, unnamed historical row, or another club
    # must never become a result merely by widening a time tolerance.
    if not 3300<=difference<=3900:return False
    if not str(row.get('_settlementSource','')).startswith('CACHED_EXACT_FIXTURE:'):return False
    if datetime.fromtimestamp(played,timezone.utc).date()!=datetime.fromtimestamp(scheduled,timezone.utc).date():return False
    def identity(value):return ''.join(c for c in str(value or '').casefold() if c.isalnum())
    names=[identity(issue.get('homeTeam')),identity(issue.get('awayTeam')),
           identity(row.get('home_team')),identity(row.get('away_team'))]
    if not all(names) or names[:2]!=names[2:]:return False
    published=stamp(issue.get('publishedAt'))
    return bool(published and published<min(scheduled,played))

def exact_result(issue,actual,root,now):
    """Keep fixture ID AND kickoff binding; complete valid counts are required."""
    signature=[]
    for relative in SOURCES:
        path=Path(root)/relative
        stat=path.stat() if path.exists() else None
        signature.append((str(path),stat.st_mtime_ns if stat else 0,stat.st_size if stat else 0))
    candidates=[r for r in cached_rows(tuple(signature)) if r['_evidenceAt']<=now]
    candidates += [dict(r,_evidenceAt=0.) for r in actual]
    candidates=[r for r in candidates if fixture_matches(issue,r)]
    keys=('HS','AS') if issue['metric']=='shots' else ('HST','AST')
    complete=[]
    for r in candidates:
        stats=r.get('normalized_stats') or {};values=[number(stats.get(k)) for k in keys]
        if any(v is None or not v.is_integer() for v in values):continue
        inconsistent=False
        for total,target in (('HS','HST'),('AS','AST')):
            s,t=number(stats.get(total)),number(stats.get(target))
            if s is not None and t is not None and t>s:inconsistent=True
        if not inconsistent:complete.append(r)
    if not complete:return None
    latest=max(r['_evidenceAt'] for r in complete);latest_rows=[r for r in complete if r['_evidenceAt']==latest]
    # Same-clock conflicting cache/history records remain unresolved.
    if len({tuple(number(r['normalized_stats'][k]) for k in keys) for r in latest_rows})!=1:return None
    return latest_rows[0]
