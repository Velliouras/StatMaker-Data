#!/usr/bin/env python3
"""Targeted read-only Betting V2 Phase-1 audit on canonical PROD data.

No provider calls, no publisher, no Android dependencies, no source mutations.
Compare the current Phase-1 bridge against the same App-Ready generation and
same cutoff used by the pre-bridge 2026-10-13 diagnostic snapshot.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_real_prod_diagnostic import evaluate, open_app_ready


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repository-root", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()
    root = args.repository_root.resolve()
    target = args.output_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)

    manifest = json.loads((root / "data/statmaker/app_ready/update_manifest.json").read_text(encoding="utf-8"))
    matches = [v for v in manifest["artifacts"] if v.get("id") == "app_ready_betting_bundle"]
    if len(matches) != 1:
        raise ValueError("Expected exactly one canonical betting ZIP")
    artifact = matches[0]
    file = root / artifact["path"]
    if not file.is_file():
        raise FileNotFoundError(f"Canonical betting ZIP absent: {file}")
    checksum = hashlib.sha256(file.read_bytes()).hexdigest()
    if checksum != artifact["sha256"]:
        raise ValueError("Canonical App-Ready ZIP SHA256 mismatch")

    index = json.loads((root / "data/statmaker/uat/betting_v2/latest.json").read_text(encoding="utf-8"))
    baseline_path = root / "data/statmaker/uat/betting_v2" / index["artifact"]
    baseline_raw = baseline_path.read_bytes()
    if hashlib.sha256(baseline_raw).hexdigest() != index["sha256"]:
        raise ValueError("Baseline UAT diagnostic checksum mismatch")
    baseline = json.loads(baseline_raw)
    if (index["generationId"] != manifest["metadata"]["preparedPatternGenerationId"]
            or baseline["generationId"] != index["generationId"]
            or baseline["sourceBundle"]["sha256"] != checksum
            or index["sourceBundleSha256"] != checksum):
        raise ValueError("Baseline and PROD data generation are different: comparison invalid")

    as_of = datetime.fromisoformat(baseline["generatedAt"].replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        raise ValueError("Baseline cutoff lacks timezone")
    with tempfile.TemporaryDirectory(prefix="betting-v2-phase1-readonly-") as temp:
        refreshed = evaluate(root, open_app_ready(file, Path(temp)), index["generationId"], as_of)

    for key in ("rawJoinedRows", "rawQuotedRows", "futureFixtures"):
        if refreshed[key] != baseline[key]:
            raise ValueError(f"Scope mismatch for {key}: {baseline[key]} -> {refreshed[key]}")

    baseline_fixture = baseline.get("fixtureRejections", {})
    current_fixture = refreshed.get("fixtureRejections", {})
    baseline_selections = baseline.get("selectionRejections", {})
    current_selections = refreshed.get("selectionRejections", {})
    fixture_reasons = sorted(set(baseline_fixture) | set(current_fixture))
    selection_reasons = sorted(set(baseline_selections) | set(current_selections))
    delta = lambda old, new, reason: {
        "before": int(old.get(reason, 0)),
        "after": int(new.get(reason, 0)),
        "change": int(new.get(reason, 0)) - int(old.get(reason, 0)),
    }

    summary = {
        "status": "READ_ONLY_PHASE1_COMPARISON",
        "productionWrites": 0,
        "providerCalls": 0,
        "sameGeneration": True,
        "sameAsOfUTC": as_of.astimezone(timezone.utc).isoformat(),
        "canonicalBundleSha256": checksum,
        "baselineGeneratedAt": baseline["generatedAt"],
        "historyRows": {"before": baseline["historyRows"],
                        "after": refreshed["historyRows"],
                        "change": refreshed["historyRows"] - baseline["historyRows"]},
        "forecastableFixtures": {"before": baseline["forecastableFixtures"],
                                 "after": refreshed["forecastableFixtures"],
                                 "change": refreshed["forecastableFixtures"] - baseline["forecastableFixtures"]},
        "candidateCountDiagnosticOnly": {"before": baseline["candidateCount"],
                                         "after": refreshed["candidateCount"],
                                         "change": refreshed["candidateCount"] - baseline["candidateCount"]},
        "fixtureRejections": {key: delta(baseline_fixture, current_fixture, key)
                              for key in fixture_reasons},
        "selectionRejections": {key: delta(baseline_selections, current_selections, key)
                                for key in selection_reasons},
        "phase1HistoryCoverage": refreshed["phase1DataLineage"],
        "unresolvedFixtures": refreshed["phase1RejectedFixtures"],
        "marketHistoryFailures": refreshed["phase1MarketHistoryFailures"],
        "uefaExistingDomesticForm": {
            "counts": refreshed.get("phase1UefaDomesticFormSupport", {}).get("counts", {}),
            "fixturesChecked": refreshed.get("phase1UefaDomesticFormSupport", {}).get("fixtureCount", 0),
            "certifiedUefaModels": 0,
        },
        "priorDifferentDivisionHistory": {
            "counts": refreshed.get("phase1VerifiedDivisionTransitionForm", {}).get("counts", {}),
            "fixturesChecked": refreshed.get("phase1VerifiedDivisionTransitionForm", {}).get("checkedFixtures", 0),
            "certifiedCrossDivisionModels": 0,
        },
        "unverifiedGenericCardsMarketQuotes": int(refreshed.get("selectionRejections", {}).get(
            "UNVERIFIED_BOOKMAKER_CARDS_SETTLEMENT_RULE", 0)),
        "missingActualCornersMarketObservations": sum(
            1 for row in refreshed.get("phase1MarketHistoryFailures", [])
            if row.get("family") == "CORNERS"),
        "notCertifiedStrong": True,
        "UatAndroidUnchanged": True,
    }
    (target / "phase1_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (target / "phase1_diagnostic.json").write_text(
        json.dumps(refreshed, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    print("PHASE1_OFFLINE_SAME_GENERATION_COMPARISON")
    print(json.dumps({
        "generationId": index["generationId"],
        "historyRows": summary["historyRows"],
        "forecastableFixtures": summary["forecastableFixtures"],
        "candidateCountDiagnosticOnly": summary["candidateCountDiagnosticOnly"],
        "fixtureRejections": summary["fixtureRejections"],
        "missingMarketSpecificHistory": summary["selectionRejections"].get("MISSING_MARKET_SPECIFIC_HISTORY"),
        "uefaExistingDomesticForm": summary["uefaExistingDomesticForm"],
        "priorDifferentDivisionHistory": summary["priorDifferentDivisionHistory"],
        "unverifiedGenericCardsMarketQuotes": summary["unverifiedGenericCardsMarketQuotes"],
        "missingActualCornersMarketObservations": summary["missingActualCornersMarketObservations"],
        "unresolvedFixtureDetails": len(summary["unresolvedFixtures"]),
        "unresolvedMarketHistoryDetails": len(summary["marketHistoryFailures"]),
        "providerCalls": 0, "productionWrites": 0,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
