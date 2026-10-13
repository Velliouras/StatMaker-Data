#!/usr/bin/env python3
"""Official cached-source league/season fixture statistics preflight (max 6 calls).

This does NOT create corners or bookmaker settlement contracts. It establishes
provider coverage without spending per-fixture API requests where unsupported.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
import api_football_stats_coverage_gate as probe

CODES=("NOR2","SC1","SC2","SC3","SVK","MAR")

def coverage_report(root:Path, key:str)->dict:
    idx=json.loads((root/"data/statmaker/domestic_enriched/index.json").read_text(encoding="utf-8"))
    rows=[]
    for code in CODES:
        choices=sorted((x for x in idx.get("leagues") or []
                        if x.get("league_code")==code and x.get("api_football_league_id")
                        and x.get("api_football_season")),
                       key=lambda x:int(x["api_football_season"]),reverse=True)
        if not choices:
            raise ValueError("Cannot establish official provider scope: "+code)
        source=choices[0]
        rows.append({"code":code,"league":source.get("league"),"country":source.get("country"),
                     "api_football_league_id":source["api_football_league_id"],
                     "season":str(source["api_football_season"])})
    state={"count":0}
    result=[]
    for row in rows:
        covered,record,reason=probe.read_statistics_coverage(
            row,None,key,state,len(CODES))
        result.append({"leagueCode":row["code"],"name":row["league"],"country":row["country"],
                       "apiLeagueId":row["api_football_league_id"],"season":row["season"],
                       "officialFixtureStatsCoverage":covered,"reason":reason,
                       "officialEvidence":record,
                       "allCompetitionsRemainEnabled":True})
    return {"schemaVersion":1,"status":"OFFICIAL_PROVIDER_COVERAGE_ONLY",
            "generatedAtUtc":datetime.now(timezone.utc).isoformat(),
            "leagues":result,"apiCallsUsed":state["count"],"apiCallsCeiling":len(CODES),
            "quotasAndPricingUnchanged":True,"notCertifiedBettingForecast":True,
            "doesNotInferAbsentStatisticsAsZeros":True,
            "note":"Per-season coverage cannot guarantee that any specific completed match supplies corner statistics"}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--root",type=Path,default=ROOT)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    key=os.environ.get("API_FOOTBALL_KEY","")
    if not key:
        raise SystemExit("API_FOOTBALL_KEY is required; no API call made")
    result=coverage_report(a.root,key)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("PHASE1_OFFICIAL_COVERAGE",json.dumps({"apiCallsUsed":result["apiCallsUsed"],
          "leagues":[{"code":x["leagueCode"],"season":x["season"],
                      "available":x["officialFixtureStatsCoverage"],
                      "reason":x["reason"]} for x in result["leagues"]]},ensure_ascii=False))


if __name__=="__main__":
    main()
