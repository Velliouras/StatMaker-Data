"""Verified promotion/relegation and cross-division history availability.

API-Football team IDs remain globally stable while league IDs vary by season.
We preserve source competition labels and dates; NO cross-division goal rates
or Elo are calculated here (those require separate calibrated coefficients).
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime,timezone


def api_team_id(value):
    token=str(value or "")
    if token.startswith("api-team:"):
        parts=token.split(":")
        return parts[-1] if len(parts)==3 and parts[-1].isdigit() else None
    return None


def cross_division_evidence(history, fixtures, cutoff):
    by_team=defaultdict(list)
    for match in history:
        dt=match.get("date")
        if dt is None or dt>=cutoff or dt.date()>=cutoff.date():
            continue
        for side in ("home","away"):
            pid=str(match.get("providerHomeId" if side=="home" else "providerAwayId") or
                    api_team_id(match.get(side)) or "")
            if not pid.isdigit():
                continue
            by_team[pid].append({
                "group":match["group"],"date":dt,
                "fixtureId":match["fixture_id"],
                "venue":side.upper(),"goalsFor":match["hg"] if side=="home" else match["ag"],
                "goalsAgainst":match["ag"] if side=="home" else match["hg"]})
    count=Counter()
    findings=[]
    for fixture in fixtures:
        sides={}
        for key,venue in (("home","HOME"),("away","AWAY")):
            pid=api_team_id(fixture.get(key))
            external=[]
            if pid is not None:
                historical={(m["group"],str(m["fixtureId"])):m
                            for m in by_team.get(pid,[])
                            if m["group"]!=fixture["group"] and m["date"]<fixture["date"]
                            and m["date"].date()<fixture["date"].date()}
                external=sorted(historical.values(),key=lambda m:m["date"])[-20:]
            sides[key]={
                "providerIdentityVerified":pid is not None,
                "otherCompetitionMatches":len(external),
                "otherCompetitionVenueMatches":sum(m["venue"]==venue for m in external),
                "sourceCompetitions":sorted({m["group"] for m in external}),
                "lastSourceResultDate":external[-1]["date"].date().isoformat() if external else None,
                "notAutomaticallyRatedAsCurrentDivisionForm":True,
            }
        count["both_sides_other_division_history" if all(
            s["otherCompetitionMatches"] for s in sides.values()) else
            "one_side_other_division_history" if any(
            s["otherCompetitionMatches"] for s in sides.values()) else
            "no_verified_other_division_history"]+=1
        findings.append({"fixtureId":fixture["fixtureId"],"leagueCode":fixture["leagueCode"],
                         "homeTeam":fixture["homeTeam"],"awayTeam":fixture["awayTeam"],
                         "sides":sides,"calibrationRequired":True,"certified":False})
    return {"checkedFixtures":len(findings),"counts":dict(count),
            "fixtures":findings,"providerCalls":0,
            "source":"Exact API-Football team IDs in cached historical league fixtures"}
