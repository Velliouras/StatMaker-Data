"""Use unique, verified API-Football team IDs from canonical domestic fixture caches.

Never infer team IDs from unverified similarity. ID is accepted only when
a league+season scoped fixture supplies the real provider ID, and the given
name/explicit league-local alias has exactly one source owner. This is
independent of expensive /teams?search calls. No network access.
"""
from __future__ import annotations
from collections import Counter,defaultdict
import json
from pathlib import Path
from build_uefa_support_history import normalize_team_key

def load_verified_api_team_index(root:Path):
    root=root.resolve()
    index=json.loads((root/"data/statmaker/domestic_enriched/index.json").read_text(encoding="utf-8"))
    aliases_file=root/"mappings/domestic_team_aliases.json"
    aliases=json.loads(aliases_file.read_text(encoding="utf-8")).get("aliases",{}) if aliases_file.is_file() else {}
    owners=defaultdict(set)
    official=defaultdict(Counter)
    direct_by_scope=defaultdict(set)
    paths=sorted(set(str(x.get("cache_path")) for x in index.get("leagues",[])
                     if x.get("cache_path")))
    for relative in paths:
        path=(root/relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        data=json.loads(path.read_text(encoding="utf-8"))
        if data.get("provider")!="api-football":
            continue
        lid=str(data.get("league_id") or "")
        season=str(data.get("season") or "")
        league_info=next((x for x in index["leagues"] if x.get("cache_path")==relative),None)
        if not league_info or lid!=str(league_info.get("api_football_league_id")) or season!=str(league_info.get("api_football_season")):
            continue
        code=str(league_info.get("league_code") or "")
        for row in data.get("schedule_fixtures") or []:
            src=row.get("source_league") or {}
            if str(src.get("id"))!=lid or str(src.get("season"))!=season:
                continue
            for side in ("home","away"):
                name=row.get(side+"_team")
                team_id=str(row.get(side+"_team_id") or "")
                norm=normalize_team_key(name)
                if not norm or not team_id.isdigit():
                    continue
                owners[norm].add(team_id)
                direct_by_scope[code,norm].add(team_id)
                official[team_id][str(name).strip()]+=1
    # Alias use requires unique already verified source ownership in its own
    # competition, then a unique global team id for that alias.
    for code,teams in aliases.items():
        for canonical,names in teams.items():
            source_ids=direct_by_scope.get((str(code),normalize_team_key(canonical)),set())
            if len(source_ids)!=1:
                continue
            team_id=next(iter(source_ids))
            for alias in names or []:
                key=normalize_team_key(alias)
                if key:
                    owners[key].add(team_id)

    resolved={}
    ambiguous=set()
    for name,ids in owners.items():
        if len(ids)!=1:
            ambiguous.add(name)
            continue
        team_id=next(iter(ids))
        if not official[team_id]:
            continue
        official_name=sorted(official[team_id].items(),
                             key=lambda x:(-x[1],x[0]))[0][0]
        resolved[name]={"id":int(team_id),"name":official_name,
                        "source":"CACHED_API_FOOTBALL_VERIFIED_TEAM_ID"}
    return resolved,{"cachedUniqueTeamAliases":len(resolved),
                     "ambiguousNamesRejected":len(ambiguous),
                     "canonicalTeamIds":len(official),
                     "providerCalls":0}


def find_verified_team(index,team):
    return index.get(normalize_team_key(team))
