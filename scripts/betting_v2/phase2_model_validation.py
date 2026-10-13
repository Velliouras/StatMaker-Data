#!/usr/bin/env python3
"""Phase 2A: frozen, separate xG-primary and no-xG Elo research holdouts.

All inputs are previously cached completed match scores and real observed xG.
Only genuinely earlier samples can train or calibrate a held-out event.
Never uses bookmaker price as a predictive feature or grants STRONG.
No API, production writes, or forecast publishing.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import json
from math import isfinite, log
from pathlib import Path

from elo_holdout_diagnostics import wilson_lower
from walk_forward_goals import walk_forward
from walk_forward_elo import walk_forward_elo

ONE_X_TWO = ("1X2_HOME", "1X2_DRAW", "1X2_AWAY")
MARKET_EVENTS = ONE_X_TWO + tuple(
    f"{side}_OVER_{line}_5"
    for side in ("HOME","AWAY","MATCH") for line in (0,1,2,3,4,5)
)
MIN_CALIBRATION_BIN_CASES = 30


def _date(value: object) -> date:
    return date.fromisoformat(str(value or "")[:10])


def strict_evidence(cal: list[dict], holdout: list[dict], strategy: str) -> dict:
    if not cal or not holdout:
        return {"valid":False, "reason":"EMPTY_CALIBRATION_OR_HOLDOUT"}
    cal_dates = [_date(x["date"]) for x in cal]
    hold_dates = [_date(x["date"]) for x in holdout]
    if max(cal_dates) >= min(hold_dates):
        return {"valid":False, "reason":"CHRONOLOGICAL_CALIBRATION_LEAKAGE"}
    cal_ids = {(x["league"],str(x["fixtureId"])) for x in cal}
    hold_ids = {(x["league"],str(x["fixtureId"])) for x in holdout}
    if cal_ids.intersection(hold_ids):
        return {"valid":False,"reason":"DUPLICATED_CALIBRATION_HOLDOUT_FIXTURE"}
    if strategy == "ELO_GOALS_FALLBACK_NO_XG":
        if any(x.get("historicalXgSufficient") is not False or
               x.get("strategy") != strategy for x in cal+holdout):
            return {"valid":False,"reason":"XG_PRIMARY_DATA_MIXED_INTO_FALLBACK"}
    return {"valid":True,"calEnd":max(cal_dates).isoformat(),
            "holdoutStart":min(hold_dates).isoformat(),
            "calibrationFixtures":len(cal),
            "untouchedHoldoutFixtures":len(holdout)}


def _bin(p: float) -> int:
    return min(9, int(p*10))


def _observations(rows: list[dict], event: str) -> list[tuple[str, int, float, int]]:
    result=[]
    for r in rows:
        prob=r.get("probabilities") or {}
        actual=r.get("observed") or {}
        if event not in prob or event not in actual:
            continue
        try:
            p=float(prob[event])
            y=int(actual[event])
        except (ValueError,TypeError):
            continue
        if not (isfinite(p) and 0 <= p <= 1 and y in (0,1)):
            continue
        result.append((str(r.get("leagueCode") or ""),_bin(p),p,y))
    return result


def frozen_market_calibration(cal: list[dict], holdout: list[dict],
                              *, minimum: int = MIN_CALIBRATION_BIN_CASES) -> dict:
    """Freeze per-market calibration lower bounds BEFORE observing holdout.

    This is diagnostic evidence, not the production calibration plan:
    context/adverse-scenario uncertainty, exact dated prices, independent ROI
    and bookmaker-specific settlement are all deliberately unavailable.
    """
    by_market={}
    summary=Counter()
    for event in MARKET_EVENTS:
        cal_bins=defaultdict(list)
        for league,b,p,y in _observations(cal,event):
            cal_bins[b].append((p,y))
        frozen={}
        for b,observations in cal_bins.items():
            n=len(observations)
            wins=sum(y for _,y in observations)
            frozen[str(b)]={"n":n,"wins":wins,
                  "meanRawProbability":sum(p for p,_ in observations)/n,
                  "wilson95Lower":wilson_lower(wins,n),
                  "sampleSufficient":n>=minimum}
        hold=_observations(holdout,event)
        scored=[]
        by_league=defaultdict(list)
        for league,b,p,y in hold:
            entry=frozen.get(str(b))
            # Do NOT use holdout outcomes to select an acceptance cutoff.
            sufficient=bool(entry and entry["sampleSufficient"])
            lower=entry["wilson95Lower"] if sufficient else None
            case={"leagueCode":league,"p":p,"actual":y,
                  "frozenCalibratedLower":lower,
                  "bin":b,"hasCalibratedBin":sufficient}
            scored.append(case)
            by_league[league].append(case)
        valid=[x for x in scored if x["hasCalibratedBin"]]
        conservative=[x for x in valid if x["frozenCalibratedLower"]>=.60]
        summary["holdout_events"]+=len(scored)
        summary["holdout_events_with_calibration_evidence"]+=len(valid)
        summary["frozen_lower_at_least_60"]+=len(conservative)
        def metrics(items):
            n=len(items)
            return {"n":n,"observedHitRate":sum(x["actual"] for x in items)/n if n else None,
                    "observedWilson95Lower":wilson_lower(sum(x["actual"] for x in items),n) if n else None,
                    "meanRawP":sum(x["p"] for x in items)/n if n else None}
        by_market[event]={
            "frozenCalibrationBins":frozen,
            "holdout":metrics(scored),
            "calibratedBinHoldout":metrics(valid),
            "frozenConservativePAtLeast60Holdout":metrics(conservative),
            "byLeague":{k:metrics(v) for k,v in sorted(by_league.items())},
            "canCertifyStrong":False}
    return {"events":by_market,"totals":dict(summary),
            "notCertified":True,
            "reason":"UNPRICED_RESEARCH_WITHOUT_ADVERSE_CONTEXT_OR_INDEPENDENT_ROI"}


def validate_mode(report:dict, cal:list[dict], hold:list[dict], strategy:str) -> dict:
    proof=strict_evidence(cal,hold,strategy)
    if not proof["valid"]:
        return {"researchOnly":True,"status":"BLOCKED","proof":proof,
                "holdout1X2LogLoss":None,"marketCalibration":None,
                "publishedStrongPicks":0}
    nll=0.0
    brier=0.0
    count=0
    for item in hold:
        probs=item["probabilities"]
        outcomes=item["observed"]
        if any(k not in probs or k not in outcomes for k in ONE_X_TWO):
            raise ValueError("Missing one of the three 1X2 outcomes")
        if abs(sum(probs[k] for k in ONE_X_TWO)-1)>1e-6:
            raise ValueError("Invalid 1X2 distribution")
        if sum(int(outcomes[k]) for k in ONE_X_TWO)!=1:
            raise ValueError("Invalid realized 1X2 label")
        actual=next(k for k in ONE_X_TWO if int(outcomes[k]))
        nll-=log(max(float(probs[actual]),1e-15))
        brier+=sum((float(probs[k])-int(outcomes[k]))**2 for k in ONE_X_TWO)
        count+=1
    return {"researchOnly":True,"status":"RESEARCH_HOLDOUT_MEASURED",
            "strategy":strategy,"proof":proof,
            "holdout1X2LogLoss":nll/count,
            "holdout1X2MulticlassBrier":brier/count,
            "marketCalibration":frozen_market_calibration(cal,hold),
            "publishedStrongPicks":0,"canCertifyStrong":False,
            "requiresIndependentOddsROI":True,
            "requiresAdverseLineupConfidence":True,
            "modelReport":{"tunedParams":report.get("tunedParams"),
                           "splits":report.get("splits"),"readiness":report.get("readiness")}}


def evaluate(root:Path)->dict:
    xg,xg_cal,xg_hold=walk_forward(root)
    elo,elo_cal,elo_hold=walk_forward_elo(root)
    # Both modes may describe the same underlying clubs, but never the same
    # calibration/holdout prediction row. Mode-specific calibration is frozen.
    for name,records in (("ELO calibration",elo_cal),("ELO holdout",elo_hold)):
        if any(x.get("historicalXgSufficient") is not False for x in records):
            raise ValueError("Mode contamination in "+name)
    results={"XG_PRIMARY":validate_mode(xg,xg_cal,xg_hold,"XG_PRIMARY"),
             "ELO_GOALS_FALLBACK_NO_XG":validate_mode(
                 elo,elo_cal,elo_hold,"ELO_GOALS_FALLBACK_NO_XG")}
    both_modes=sum(v["status"]=="RESEARCH_HOLDOUT_MEASURED" for v in results.values())
    return {"contract":"statmaker-betting-v2-phase2-research-evidence-v1",
            "status":"READ_ONLY_CHRONOLOGICAL_RESEARCH",
            "uncertifiedModes":2,
            "modeHoldoutsMeasured":both_modes,"modes":results,
            "noAutomaticStrong":True,"publishedStrongPicks":0,
            "hasVerifiedMarketPriceROI":False,
            "hasAdverseLineupEvidence":False,
            "sourceCalls":0,"prodWrites":0,
            "nextGate":"independent_scenario_price_and_holdout_calibration"}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repository-root",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    report=evaluate(args.repository_root)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("BETTING_V2_PHASE2_CALIBRATION_HOLDOUT",json.dumps({
        "measuredModes":report["modeHoldoutsMeasured"],
        "modes":{k:{"status":v["status"],"holdoutLogLoss":v["holdout1X2LogLoss"],
                  "holdoutN":v["proof"].get("untouchedHoldoutFixtures")}
                 for k,v in report["modes"].items()},
        "providerCalls":0,"strongPicks":0},ensure_ascii=False))


if __name__=="__main__":
    main()
