#!/usr/bin/env python3
"""Phase 2 authoritative STOP/GO on exact checked-in producer evidence.

The historical research holdout does not imply a certified priced STRONG.
This read-only audit intentionally refuses inferred timestamps, odds-universe
attestations, real lineup scenarios or synthetic training sample records.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from scripts.betting_v2.production_sources import cached_history

def source_truth(root:Path)->dict:
    root=root.resolve()
    receipts=root/"reports/betting_v2/quote_receipt_index.json"
    holdout=root/"reports/betting_v2/REAL_HOLDOUT_EVIDENCE_AUDIT_2026-10-11.json"
    if not receipts.is_file() or not holdout.is_file():
        raise ValueError("Missing required read-only evidence audits")
    receipt_index=json.loads(receipts.read_text(encoding="utf-8"))
    old=json.loads(holdout.read_text(encoding="utf-8"))
    if receipt_index.get("contract")!="betting-v2-client-quote-index-v1":
        raise ValueError("Unknown quote receipt index contract")
    if old.get("contract")!="betting-v2-real-holdout-evidence-audit-v1":
        raise ValueError("Unknown real holdout evidence contract")
    h,s,reasons=cached_history(root)
    fields={
      "completedHistoricalRowsWithTruePointInTimeStatisticsReceipt":len(h),
      "scheduledProviderFixtures":len(s),
      "historicBlockedByMissingPointInTimeReceipt":int(reasons.get("MISSING_POINT_IN_TIME_STATISTICS_RECEIPT",0)),
      "historyOtherBlockReasons":{k:v for k,v in reasons.items()
                                  if k!="MISSING_POINT_IN_TIME_STATISTICS_RECEIPT"},
      "verifiedQuoteSnapshotReceipts":int(receipt_index.get("verifiedSnapshotReceipts") or 0),
      "completeQuoteSnapshotReceipts":int(receipt_index.get("completeSnapshotReceipts") or 0),
      "preKickoffQuoteRows":int(receipt_index.get("preKickoffPriceRows") or 0),
      "independentlyPricedHoldoutCertified":bool(receipt_index.get("hasPricedProfitCertificate") is True),
      "previousHoldoutProviderPriceTimestampVerified":
         bool(old.get("bookmakerPriceAudit",{}).get("individualPriceObservationTimestampVerified") is True),
      "previousHoldoutUnfilteredBookmakerUniverseVerified":
         bool(old.get("bookmakerPriceAudit",{}).get("independentlyCompletePriceUniverseVerified") is True),
      "previousHoldoutROIProven":
         bool(old.get("bookmakerPriceAudit",{}).get("pricedROIProven") is True),
      "historicalAdverseLineupCaseFiles":len(
          list((root/"reports/betting_v2").glob("*adverse*lineup*receipt*.json"))
      ) if (root/"reports/betting_v2").is_dir() else 0,
    }
    gate={
      "contract":"statmaker-betting-v2-phase2-producer-evidence-readiness-v1",
      "evaluatedAtUTC":datetime.now(timezone.utc).isoformat(),
      "source":"CURRENT_CHECKED_IN_DATA_REPO_CACHES_AND_AUDITS",
      "facts":fields,
      "blockingRequirements":[],
      "sourceCalls":0,"prodWrites":0,"strongPublished":0,
    }
    problems=gate["blockingRequirements"]
    if not fields["completedHistoricalRowsWithTruePointInTimeStatisticsReceipt"]:
        problems.append("NO_POINT_IN_TIME_PROVIDER_STATISTICS_TRAINING_ROWS")
    if not fields["independentlyPricedHoldoutCertified"]:
        problems.append("NO_INDEPENDENT_PRICED_ROI_CERTIFICATE")
    if not fields["previousHoldoutProviderPriceTimestampVerified"]:
        problems.append("NO_ATTESTED_HISTORIC_QUOTE_LEVEL_PRICE_OBSERVATION_TIME")
    if not fields["previousHoldoutUnfilteredBookmakerUniverseVerified"]:
        problems.append("NO_ATTESTED_COMPLETE_UNFILTERED_HISTORICAL_BOOKMAKER_PRICE_UNIVERSE")
    if not fields["previousHoldoutROIProven"]:
        problems.append("NO_PROVEN_INDEPENDENT_PROFITABILITY_BOUND")
    if not fields["historicalAdverseLineupCaseFiles"]:
        problems.append("NO_RECORDED_ACTUAL_STARTING_XI_ADVERSE_REALIZATION_ARCHIVE")
    gate["decision"]="BLOCKED" if problems else "EVIDENCE_PREREQUISITES_PRESENT_REVIEW_REQUIRED"
    gate["sufficientForStrong"]=False
    gate["note"]="The archive count is a repository-source audit, not a claim that a provider or user has no additional private data. Historical receipts must never be backdated."
    return gate


def main():
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument("--repository-root",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    answer=source_truth(a.repository_root)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(answer,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("BETTING_V2_PHASE2_PROD_READINESS",json.dumps({
      "decision":answer["decision"],
      "blockers":answer["blockingRequirements"],
      "statisticsTrainingRows":answer["facts"]["completedHistoricalRowsWithTruePointInTimeStatisticsReceipt"],
      "missingStatsProvenance":answer["facts"]["historicBlockedByMissingPointInTimeReceipt"],
      "providerCalls":0,"prodWrites":0
    },ensure_ascii=False))

if __name__=="__main__":
    main()
