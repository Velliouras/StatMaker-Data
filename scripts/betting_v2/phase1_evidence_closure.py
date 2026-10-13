#!/usr/bin/env python3
"""Read-only Phase-1 evidence closure: exact PROD-input diagnostic + separate club support.

No requests, publishing, re-scoring, fabricated samples or relaxation of 8-game
minimums. All times strictly precede the frozen forecast reference day.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

def read(p):
    return json.loads(p.read_text(encoding="utf-8"))

def normalized(value):
    import unicodedata
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())

def date_only(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z","+00:00")).date()
    except ValueError:
        return None

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--root",type=Path,default=Path("."))
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    root=args.root.resolve()
    latest=read(root/"data/statmaker/uat/betting_v2/latest.json")
    report=read(root/"data/statmaker/uat/betting_v2"/latest["artifact"])
    reference=date_only(report["generatedAt"])
    if reference is None or report.get("status") != "REAL_PROD_INPUT_DIAGNOSTIC":
        raise ValueError("Expected dated read-only PROD diagnostic")
    large=read(root/"data/statmaker/uefa_support_history.json")
    if large.get("schemaVersion") != 1:
        raise ValueError("Unsupported supplemental history")
    aliases=defaultdict(set)
    participants=large.get("participants",[])
    for i,p in enumerate(participants):
        for name in [p.get("canonicalTeam"),p.get("providerTeam")]+list(p.get("aliases") or []):
            if name:
                aliases[normalized(name)].add(i)

    def source_for(label):
        owners=aliases.get(normalized(label),set())
        return next(iter(owners)) if len(owners)==1 else None

    output={"status":"PHASE1_EVIDENCE_READINESS_ONLY",
            "sourceDiagnostic":latest["artifact"],"generationId":report["generationId"],
            "cutoffDateExclusive":reference.isoformat(),
            "providerCalls":0,"publisherWrites":0,"certifiedPicks":0,
            "uefa":{"sourceParticipants":len(participants),"fixtureChecks":[],
                    "classified":{}},
            "missingModelForm":[],"missingMarketHistory":[],
            "coverageByCompetition":{}}
    categories=Counter()
    for row in report.get("phase1RejectedFixtures") or []:
        reason=row.get("reason")
        if reason=="NO_VERIFIED_UEFA_TEAM_HISTORY":
            sides={}
            for side in ("home","away"):
                names=[row.get(side+"Team")]
                pids={source_for(v) for v in names if v}
                pids.discard(None)
                if len(pids)!=1:
                    sides[side]={"sourceMatched":False,"candidateParticipants":len(pids),"pastResultRows":0}
                    continue
                participant=participants[next(iter(pids))]
                past=[x for x in participant.get("matches") or []
                      if date_only(x.get("date")) and date_only(x["date"])<reference
                      and isinstance(x.get("homeGoals"),int)
                      and isinstance(x.get("awayGoals"),int)]
                sides[side]={"sourceMatched":True,"participant":participant.get("canonicalTeam"),
                             "sourceLeagueCode":participant.get("leagueCode"),
                             "pastResultRows":len(past),
                             "pastCornerRows":sum(x.get("homeCorners") is not None and x.get("awayCorners") is not None for x in past),
                             "pastShotRows":sum(x.get("homeShots") is not None and x.get("awayShots") is not None for x in past)}
            key=("both" if all(s.get("sourceMatched") for s in sides.values()) else
                 "one" if any(s.get("sourceMatched") for s in sides.values()) else "none")
            categories[key]+=1
            output["uefa"]["fixtureChecks"].append({"fixtureId":row.get("fixtureId"),
                      "leagueCode":row.get("leagueCode"),"homeTeam":row.get("homeTeam"),
                      "awayTeam":row.get("awayTeam"),"sourceAvailability":key,
                      "verifiedDomesticSupport":sides,
                      "eligibleGoalForecast":False,
                      "requiresCrossCompetitionCalibration":True})
        elif reason=="NO_PRIOR_TEAM_FORM_FOR_GOALS_MODEL":
            output["missingModelForm"].append({k:row.get(k) for k in
                ("leagueCode","fixtureId","homeTeam","awayTeam","priorHomeMatches",
                 "priorAwayMatches","priorHomeVenue","priorAwayVenue",
                 "identityMethod","group")})

    output["uefa"]["classified"]=dict(categories)
    for row in report.get("phase1MarketHistoryFailures") or []:
        source=row.get("leagueCode")
        sources=[x for x in report["phase1DataLineage"].get("sources",[])
                if x.get("leagueCode")==source]
        fam=row.get("family")
        fields={"CORNERS":["HC","AC"],"SHOTS":["HS","AS"],"SOT":["HST","AST"],
                "YELLOW_CARDS":["HY","AY"],"CARDS":["HY","AY","HR","AR"],
                "GOALS_1H":["HTHG","HTAG"],"GOALS_2H":["H2HG","H2AG"]}.get(fam,[])
        observed=max((min(x.get("metricFieldCoverage",{}).get(k,0) for k in fields)
                      for x in sources),default=0) if fields else 0
        output["missingMarketHistory"].append({k:row.get(k) for k in
                ("fixtureId","leagueCode","market","selection","family","group","observedHistoryRows")} |
                {"sourceCacheCount":len(sources),"sourceAtLeastOneSeasonMetricRows":observed,
                 "reason":("SOURCE_HAS_NO_RECORDED_METRIC" if not observed else
                           "REAL_METRIC_PRESENT_BUT_INSUFFICIENT_MATCHED_TEAM_SAMPLES"),
                 "notArtificialZero":True})
    by_league=defaultdict(Counter)
    for row in output["missingMarketHistory"]:
        by_league[row["leagueCode"]][row["reason"]]+=1
    output["coverageByCompetition"]={k:dict(v) for k,v in sorted(by_league.items())}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"uefa":output["uefa"]["classified"],
                      "missingModelForm":len(output["missingModelForm"]),
                      "missingMarketHistory":len(output["missingMarketHistory"]),
                      "coverageByCompetition":output["coverageByCompetition"],
                      "providerCalls":0},ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()
