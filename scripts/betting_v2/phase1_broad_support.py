"""Attach verified archived domestic match observations to the V2 history stream.

Only existing real outcomes are used; an archived match is never represented
as a European match, and no fixture/provider ID is fabricated. Identity
ambiguity and conflicting scores fail closed. Archived 0 is a real numeric
zero; absent metrics remain null. No network calls.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time, timezone
import hashlib
import json
from pathlib import Path

from fixture_lookup import canonical_name

STATS = {"homeShots":"HS","awayShots":"AS","homeShotsOnTarget":"HST",
         "awayShotsOnTarget":"AST","homeCorners":"HC","awayCorners":"AC",
         "homeYellowCards":"HY","awayYellowCards":"AY",
         "homeRedCards":"HR","awayRedCards":"AR"}
STAT_NAMES = ("HS","AS","HST","AST","HC","AC","HY","AY","HR","AR")
SCORE_FIELDS=("hg","ag")


def number(value):
    if isinstance(value,bool) or value is None:
        return None
    try:
        n=float(value)
        return n if n>=0 and n==n and n<float("inf") else None
    except (TypeError,ValueError,OverflowError):
        return None


def archive_entries(root: Path, groups: dict, raw_history: dict, history: list[dict],
                    normalize_team) -> tuple[list[dict],dict]:
    path=root/"data/statmaker/uefa_support_history.json"
    if not path.is_file():
        return history, {"status":"NO_ARCHIVE","providerCalls":0}
    obj=json.loads(path.read_text(encoding="utf-8"))
    if obj.get("schemaVersion") != 1:
        raise ValueError("Invalid verified domestic archive schema")
    counters=Counter()
    scopes=defaultdict(set)
    for group in groups:
        scopes[group.split("|",1)[0]].add(group)
    # Verified API fixture IDs remain the strongest possible identity.
    known=defaultdict(set)
    for raw in raw_history.values():
        if not raw.get("ids"):
            continue
        group=raw["group"]
        code=group.split("|",1)[0]
        for side,tid in (("home",raw["ids"][0]),("away",raw["ids"][1])):
            name=normalize_team(code,raw[side])
            if name:
                known[group,name].add(f"api-team:{raw['leagueId']}:{tid}")
    by_key=defaultdict(list)
    for match in history:
        key=(match["group"],match["date"].date().isoformat(),
             match["home"],match["away"])
        by_key[key].append(match)
    alias_claims=defaultdict(set)
    for participant in obj.get("participants") or []:
        code=str(participant.get("leagueCode") or "")
        ident=normalize_team(code,participant.get("canonicalTeam"))
        if not code or not ident:
            continue
        for name in [participant.get("canonicalTeam"),participant.get("providerTeam")]+list(participant.get("aliases") or []):
            if name:
                alias_claims[code,normalize_team(code,name)].add(ident)

    def resolve(code,name,group):
        token=normalize_team(code,name)
        claims=alias_claims.get((code,token),set())
        if len(claims)==1:
            token=next(iter(claims))
        elif len(claims)>1:
            return None
        identities=known.get((group,token),set())
        if len(identities)==1:
            return next(iter(identities))
        if len(identities)>1:
            return None
        return token if token else None

    observations={}
    blocked=set()
    for participant in obj.get("participants") or []:
        code=str(participant.get("leagueCode") or "")
        eligible=scopes.get(code,set())
        if len(eligible)!=1:
            counters["AMBIGUOUS_ARCHIVE_LEAGUE_SCOPE"]+=1
            continue
        group=next(iter(eligible))
        for match in participant.get("matches") or []:
            if not str(match.get("sourceLabel") or "").startswith("StatMaker verified"):
                counters["UNVERIFIED_ARCHIVE_SOURCE"]+=1
                continue
            date=str(match.get("date") or "")
            try:
                dateobj=datetime.strptime(date,"%Y-%m-%d").replace(
                    hour=23,minute=59,second=59,tzinfo=timezone.utc)
            except (TypeError,ValueError):
                counters["INVALID_ARCHIVED_DATE"]+=1
                continue
            home=resolve(code,match.get("homeTeam"),group)
            away=resolve(code,match.get("awayTeam"),group)
            hg,ag=number(match.get("homeGoals")),number(match.get("awayGoals"))
            if (not home or not away or home==away or
                hg is None or ag is None or int(hg)!=hg or int(ag)!=ag):
                counters["INVALID_ARCHIVED_RESULT_OR_IDENTITY"]+=1
                continue
            k=(group,date,home,away)
            if k in blocked:
                continue
            existing=observations.get(k)
            if existing and (existing["hg"],existing["ag"])!=(int(hg),int(ag)):
                blocked.add(k)
                observations.pop(k)
                counters["CONFLICTING_ARCHIVED_SCORE"]+=1
                continue
            metrics={dst:number(match.get(src)) for src,dst in STATS.items()}
            if existing:
                for fld,val in metrics.items():
                    if val is not None and existing["stats"].get(fld) is not None and existing["stats"][fld]!=val:
                        counters["CONFLICTING_ARCHIVED_METRIC"]+=1
                        # Conflict in source observations: do not use either value.
                        existing["stats"][fld]=None
                        existing.setdefault("conflictedMetrics",set()).add(fld)
                    elif val is not None and fld not in existing.get("conflictedMetrics",set()):
                        existing["stats"][fld]=val
                counters["ARCHIVE_DUPLICATE_PARTICIPANT_ROW"]+=1
                continue
            observations[k]={"group":group,"date":dateobj,"home":home,"away":away,
                             "hg":int(hg),"ag":int(ag),"stats":metrics}

    for k,record in observations.items():
        matches=by_key.get(k,[])
        if len(matches)>1:
            counters["AMBIGUOUS_ARCHIVE_EXISTING_MATCH"]+=1
            continue
        if matches:
            existing=matches[0]
            if (existing["hg"],existing["ag"])!=(record["hg"],record["ag"]):
                counters["ARCHIVE_VS_CANONICAL_SCORE_CONFLICT"]+=1
                continue
            e_stats=existing.setdefault("stats",{})
            inserted=0
            for metric,value in record["stats"].items():
                if value is None:
                    continue
                observed=number(e_stats.get(metric))
                if observed is None:
                    e_stats[metric]=value
                    inserted+=1
                elif observed!=value:
                    counters["ARCHIVE_VS_CANONICAL_METRIC_CONFLICT"]+=1
            if inserted:
                counters["ARCHIVE_EXISTING_ROWS_WITH_ENRICHED_STATS"]+=1
                counters["ARCHIVE_METRIC_FIELDS_ADDED"]+=inserted
        else:
            # This is a real historical score row with source-specific key.
            # It is NEVER attributed a synthetic provider fixture ID.
            key="support-archive:"+hashlib.sha256("|".join(k).encode()).hexdigest()[:24]
            record["fixture_id"]=key
            record["phase1ArchiveSource"]="VERIFIED_DOMESTIC_SCORE_NO_PROVIDER_FIXTURE_ID"
            history.append(record)
            counters["ARCHIVE_VERIFIED_HISTORICAL_ROWS_ADDED"]+=1
    return history,{"status":"VERIFIED_ARCHIVE_USED",
                    "providerCalls":0,"counters":dict(counters),
                    "uniqueSourceObservations":len(observations),
                    "source":"data/statmaker/uefa_support_history.json"}
