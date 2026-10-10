#!/usr/bin/env python3
"""Single-command, zero-network Betting V2 research pilot and shadow publisher.

Default: cached source coverage + walk-forward/calibration/holdout diagnostics.
Optional --with-prices: replay a bounded number of locally archived exact odds
from this repository's existing *full local* Git history. NO git fetch.

No provider API keys, network clients, PROD App-Ready writes or Actions.
Cannot publish certified picks. Does not substitute user device testing.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path

from walk_forward_goals import walk_forward
from walk_forward_elo import walk_forward_elo
from export_historical_prices import export_date
from fixture_lookup import CachedFixtureLookup
from calibration_gate import evaluate as evaluate_calibrated
from publish_shadow import publish, _safe_output, _atomic_write


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        for row in records:
            output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines()
            if row.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    ap.add_argument("--output-root", type=Path,
                    default=Path("reports/betting_v2"))
    ap.add_argument("--with-prices", action="store_true")
    ap.add_argument("--from-date", type=date.fromisoformat)
    ap.add_argument("--to-date", type=date.fromisoformat)
    args = ap.parse_args()
    root = args.repository_root.resolve()
    directory = (root / args.output_root).resolve()
    paths = {k: directory / v for k, v in {
        "model": "pilot_model.json",
        "elo_model": "pilot_elo_fallback_model.json",
        "elo_calibration": "pilot_elo_fallback_calibration.jsonl",
        "elo_holdout": "pilot_elo_fallback_holdout.jsonl",
        "elo_priced": "pilot_elo_fallback_priced.json",
        "calibration": "pilot_calibration.jsonl",
        "holdout": "pilot_holdout.jsonl",
        "prices": "pilot_historical_prices.jsonl",
        "priced": "pilot_priced_holdout.json",
        "shadow": "shadow_manifest.json",
    }.items()}
    for p in paths.values():
        _safe_output(root, p, allowed_suffixes=(".json", ".jsonl"))
    if args.with_prices:
        if args.from_date is None or args.to_date is None:
            ap.error("--with-prices requires --from-date and --to-date")
        if args.to_date < args.from_date or (args.to_date - args.from_date).days > 13:
            ap.error("Price replay may cover only 1–14 explicit days")
    elif args.from_date is not None or args.to_date is not None:
        ap.error("Date options are valid only with --with-prices")

    print("V2_OFFLINE_PILOT_START provider_calls=0", flush=True)
    report, calibration, holdout = walk_forward(root)
    _atomic_write(paths["model"], report)
    _write_jsonl(paths["calibration"], calibration)
    _write_jsonl(paths["holdout"], holdout)
    # Independently tuned, disjoint Elo-only fallback: never borrow the xG
    # calibration bin or claim certification from another data regime.
    elo_report, elo_calibration, elo_holdout = walk_forward_elo(root)
    _atomic_write(paths["elo_model"], elo_report)
    _write_jsonl(paths["elo_calibration"], elo_calibration)
    _write_jsonl(paths["elo_holdout"], elo_holdout)
    priced_report = None

    if args.with_prices:
        statuses: list[dict] = []
        seen: set[str] = set()
        resolver = CachedFixtureLookup(root)
        with paths["prices"].open("w", encoding="utf-8") as out:
            day = args.from_date
            while day <= args.to_date:
                status = export_date(root, day, out, seen, resolver)
                statuses.append(status)
                print("V2_PRICE_REPLAY", status, flush=True)
                day += timedelta(days=1)
        _atomic_write(directory / "pilot_price_replay_status.json",
                      {"days": statuses, "apiCalls": 0})
        quotes = _read_jsonl(paths["prices"])
        if calibration and holdout:
            if max(row["date"] for row in calibration) >= min(row["date"] for row in holdout):
                raise ValueError("Holdout overlaps calibration dates")
            priced_report = evaluate_calibrated(calibration, holdout, quotes)
        else:
            priced_report = {
                "contract": "statmaker-v2-research-calibration-replay-v1",
                "certified": False, "noPriceEvidence": True,
                "reason": "Missing disjoint calibration or holdout history",
                "holdout": {"exactQuotes": len(quotes)},
            }
        _atomic_write(paths["priced"], priced_report)
        if elo_calibration and elo_holdout:
            if max(x["date"] for x in elo_calibration) >= min(x["date"] for x in elo_holdout):
                raise ValueError("Elo holdout overlaps Elo calibration")
            elo_priced_report = evaluate_calibrated(elo_calibration, elo_holdout, quotes)
        else:
            elo_priced_report = {
                "contract": "betting-v2-elo-fallback-pricing-v1",
                "certified": False, "noPriceEvidence": True,
                "reason": "Missing independently disjoint Elo calibration and holdout",
            }
        elo_priced_report["strategy"] = "ELO_GOALS_FALLBACK_NO_XG"
        elo_priced_report["certified"] = False
        _atomic_write(paths["elo_priced"], elo_priced_report)

    shadow = publish(
        root, paths["shadow"],
        model_report=paths["model"],
        priced_report=paths["priced"] if priced_report is not None else None,
        elo_model_report=paths["elo_model"],
        elo_priced_report=paths["elo_priced"] if priced_report is not None else None
    )
    print(json.dumps({
        "status": shadow["certificationStatus"],
        "forecastedHistoricFixtures": report.get("eligiblePrematchRows", 0),
        "calibrationFixtures": len(calibration),
        "holdoutFixtures": len(holdout),
        "eloFallbackHistoricRows": elo_report.get("eloFallbackEligible", 0),
        "eloFallbackCalibrationFixtures": len(elo_calibration),
        "eloFallbackHoldoutFixtures": len(elo_holdout),
        "apiCalls": 0,
        "realStrongSelections": 0,
        "report": str(paths["shadow"].relative_to(root)),
    }, ensure_ascii=False))
    print("No App-Ready PROD bundle was modified or Actions triggered by this script.")


if __name__ == "__main__":
    main()
