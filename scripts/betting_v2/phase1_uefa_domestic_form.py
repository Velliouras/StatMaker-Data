"""Source-verified European participant form across domestic competitions.

This produces a separate feature-availability audit, never rebadges domestic
matches as UEFA results or generates probabilities from uncalibrated form.
All evidence is dated and comes from verified source team archive rows.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import date
import json
from pathlib import Path
from fixture_lookup import canonical_name


def prior_date(value):
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (ValueError,TypeError):
        return None


def prepare_form_index(root: Path, cutoff: date):
    """Build a UNIQUE alias -> independently verified participant identity index.

    Both existing archives are source-authenticated. UEFA team-specific rows
    include actual European/cup/domestic results and must be kept in their
    original competition, never relabeled as European fixtures.
    """
    paths=["uefa_support_history.json","uefa_team_support_history.json"]
    names=defaultdict(set)
    profiles={}
    observed=defaultdict(set)
    for filename in paths:
        source=root/"data/statmaker"/filename
        if not source.is_file():
            continue
        data=json.loads(source.read_text(encoding="utf-8"))
        if data.get("schemaVersion")!=1:
            raise ValueError("Invalid UEFA form source schema: "+filename)
        for participant in data.get("participants") or []:
            canonical=canonical_name(participant.get("canonicalTeam"))
            if not canonical:
                continue
            p=profiles.setdefault(canonical,{"team":participant.get("canonicalTeam"),
                        "leagueCodes":set(),"matches":[],"sources":set()})
            p["leagueCodes"].add(str(participant.get("leagueCode") or ""))
            p["sources"].add(filename)
            participant_names={canonical,canonical_name(participant.get("providerTeam"))}
            participant_names.update(canonical_name(a) for a in participant.get("aliases") or [])
            for label in participant_names:
                if label:
                    names[label].add(canonical)
            for m in participant.get("matches") or []:
                d=prior_date(m.get("date"))
                if not d or d>=cutoff or not str(m.get("sourceLabel") or "").startswith("StatMaker verified"):
                    continue
                if "friendl" in canonical_name(m.get("competition")):
                    continue
                if not isinstance(m.get("homeGoals"),int) or not isinstance(m.get("awayGoals"),int):
                    continue
                home=canonical_name(m.get("homeTeam"))
                away=canonical_name(m.get("awayTeam"))
                if home not in participant_names and away not in participant_names:
                    continue
                key=(d,home,away,m["homeGoals"],m["awayGoals"])
                if key in observed[canonical]:
                    continue
                observed[canonical].add(key)
                is_home=home in participant_names
                p["matches"].append({
                    "date":d,"venue":"HOME" if is_home else "AWAY",
                    "goalsFor":m["homeGoals"] if is_home else m["awayGoals"],
                    "goalsAgainst":m["awayGoals"] if is_home else m["homeGoals"],
                    "competition":m.get("competition"),
                    "sourceLeagueCode":participant.get("leagueCode"),
                    "hasCorners":m.get("homeCorners") is not None and m.get("awayCorners") is not None,
                    "hasCards":m.get("homeYellowCards") is not None and m.get("awayYellowCards") is not None})
    for p in profiles.values():
        p["matches"].sort(key=lambda x:x["date"])
        p["leagueCodes"]=sorted(p["leagueCodes"])
        p["sources"]=sorted(p["sources"])
    return names,profiles


def evaluate_uefa_form(root:Path, fixture_rows:list[dict], cutoff:date)->dict:
    names,profiles=prepare_form_index(root,cutoff)
    outcomes=Counter()
    fixtures=[]
    for row in fixture_rows:
        if row.get("reason")!="NO_VERIFIED_UEFA_TEAM_HISTORY":
            continue
        sides={}
        for key in ("homeTeam","awayTeam"):
            owners=names.get(canonical_name(row.get(key)),set())
            if len(owners)!=1:
                sides[key]={"sourceIdentity":"AMBIGUOUS" if owners else "NOT_LINKED",
                            "historicalMatches":0,"priorVenueMatches":0,
                            "hasMinimumGoalForm":False}
                continue
            v=profiles[next(iter(owners))]
            past=v["matches"][-20:]
            venue="HOME" if key=="homeTeam" else "AWAY"
            atvenue=[m for m in past if m["venue"]==venue]
            sides[key]={"sourceIdentity":"EXACT_ARCHIVED_PARTICIPANT_ALIAS",
                        "canonicalTeam":v["team"],"sourceLeagueCodes":v["leagueCodes"],"archiveSources":v["sources"],
                        "historicalMatches":len(past),"priorVenueMatches":len(atvenue),
                        "historyFirst":past[0]["date"].isoformat() if past else None,
                        "historyLast":past[-1]["date"].isoformat() if past else None,
                        "cornerSamples":sum(m["hasCorners"] for m in past),
                        "cardSamples":sum(m["hasCards"] for m in past),
                        "hasMinimumGoalForm":len(past)>=8 and len(atvenue)>=3}
        both=all(s["hasMinimumGoalForm"] for s in sides.values())
        outcomes["both_teams_have_verified_cross_competition_goal_form" if both else
                 "requires_more_verified_prior_form_or_identity"]+=1
        fixtures.append({"fixtureId":row["fixtureId"],"leagueCode":row["leagueCode"],
                         "homeTeam":row["homeTeam"],"awayTeam":row["awayTeam"],
                         "sides":sides,"verifiedGoalFormForBoth":both,
                         "eligibleForUEFAPrediction":False,
                         "requiresIndependentUEFAVenueCalibration":True,
                         "notCertified":True})
    return {"fixtureCount":len(fixtures),"counts":dict(outcomes),
            "fixtures":fixtures,"providerCalls":0,
            "source":"data/statmaker/uefa_support_history.json",
            "policy":"Existing domestic form is historical evidence; not UEFA results or a certified prediction"}
