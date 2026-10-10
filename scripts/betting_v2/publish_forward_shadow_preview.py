#!/usr/bin/env python3
"""Generate a compact read-only Android UAT preview of *frozen* xG/ELO probabilities.

Only existing immutable gzip research snapshots are read. Latest snapshot wins;
never generates forecasts, never calls any provider or represents prices as
certified, never touches App-Ready PROD or Android packages.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path

from fixture_lookup import as_utc

ARCHIVE = Path("reports/betting_v2/forward_snapshots")
OUTPUT = Path("reports/betting_v2/pilot_forward_shadow_preview.json")
MAX_COMPRESSED_BYTES = 2_000_000
MAX_UNCOMPRESSED_BYTES = 6_000_000
MAX_ROWS = 400
MARKET_KEYS = (
    "1X2_HOME", "1X2_DRAW", "1X2_AWAY",
    "MATCH_OVER_0_5", "MATCH_OVER_1_5", "MATCH_OVER_2_5",
    "MATCH_OVER_3_5", "MATCH_OVER_4_5", "MATCH_OVER_5_5",
    "HOME_OVER_0_5", "HOME_OVER_1_5", "HOME_OVER_2_5",
    "AWAY_OVER_0_5", "AWAY_OVER_1_5", "AWAY_OVER_2_5",
)


def build(root: Path) -> dict:
    root = root.resolve()
    files = sorted((root / ARCHIVE).glob("*.json.gz"))
    if not files:
        return {"contract": "statmaker-v2-uat-forecast-preview-v1",
                "readOnly": True, "researchOnly": True,
                "certificationStatus": "BLOCKED", "certifiedStrong": 0,
                "apiCalls": 0, "matches": [],
                "error": "NO_FROZEN_FORECAST_ARCHIVE"}
    file = files[-1]
    if file.is_symlink() or file.stat().st_size > MAX_COMPRESSED_BYTES:
        raise ValueError("Frozen forecast archive exceeds size/safety cap")
    with gzip.open(file, "rb") as stream:
        raw = stream.read(MAX_UNCOMPRESSED_BYTES + 1)
    if len(raw) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("Expanded forecast archive exceeds size cap")
    data = json.loads(raw)
    if (data.get("contract") != "statmaker-v2-immutable-shadow-snapshot-set-v1"
        or data.get("certificationStatus") != "BLOCKED"
        or data.get("realStrongSelections") != 0
        or not isinstance(data.get("forecastData"), list)
        or len(data["forecastData"]) != data.get("forecastCount")):
        raise ValueError("Source snapshot lacks frozen research-only contract")
    at = as_utc(data.get("forecastComputedAtUTC"))
    if at is None:
        raise ValueError("Missing verified forecast snapshot UTC time")
    verified = {}
    identity_report = root / "reports/betting_v2/pilot_forward_identity_crosswalk.json"
    if identity_report.is_file():
        checked = json.loads(identity_report.read_text(encoding="utf-8"))
        if checked.get("contract") == "betting-v2-future-api-football-crosswalk-audit-v1":
            for item in checked.get("verifiedFixtureLinks") or []:
                if item.get("forecastArchive") != file.name:
                    continue
                if not item.get("apiFootballFixtureId"):
                    continue
                key = (str(item.get("leagueCode") or ""),
                       str(item.get("bookmakerProviderEventId") or ""),
                       str(item.get("kickoffUTC") or ""))
                if key in verified:
                    verified[key] = None  # conflicting proof must fail closed
                else:
                    verified[key] = str(item["apiFootballFixtureId"])
    records = []
    for row in data["forecastData"]:
        if not isinstance(row, dict) or row.get("certifiedStrong") is not False:
            continue
        kickoff = as_utc(row.get("kickoffUTC"))
        saved_at = as_utc(row.get("forecastComputedAtUTC"))
        if kickoff is None or saved_at != at or saved_at >= kickoff:
            continue
        if row.get("noObservedTargetScoreUsed") is not True:
            continue
        probs = row.get("probabilities")
        if not isinstance(probs, dict):
            continue
        selected = {}
        for key in MARKET_KEYS:
            value = probs.get(key)
            if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not 0.0 <= value <= 1.0):
                continue
            selected[key] = round(float(value), 6)
        if not all(k in selected for k in ("1X2_HOME","1X2_DRAW","1X2_AWAY")):
            continue
        if abs(sum(selected[k] for k in ("1X2_HOME","1X2_DRAW","1X2_AWAY"))-1)>0.002:
            continue
        crosswalk_key = (
            str(row.get("leagueCode") or ""),
            str(row.get("providerEventId") or ""),
            str(row.get("kickoffUTC") or ""),
        )
        fixture_id = verified.get(crosswalk_key)
        records.append({
            "independentApiFootballFixtureVerified": bool(fixture_id),
            "independentApiFootballFixtureId": fixture_id,
            "providerEventId": str(row.get("providerEventId") or ""),
            "leagueCode": str(row.get("leagueCode") or ""),
            "homeTeam": str(row.get("providerHomeTeam") or ""),
            "awayTeam": str(row.get("providerAwayTeam") or ""),
            "kickoffUTC": kickoff.isoformat(),
            "model": str(row.get("strategy") or ""),
            "forecastComputedAtUTC": saved_at.isoformat(),
            "expectedHomeGoals": row.get("expectedHomeGoals"),
            "expectedAwayGoals": row.get("expectedAwayGoals"),
            "probabilities": selected,
            "certifiedStrong": False,
        })
    records.sort(key=lambda r: (r["kickoffUTC"], r["leagueCode"],
                                r["providerEventId"]))
    return {
        "contract": "statmaker-v2-uat-forecast-preview-v1",
        "readOnly": True,
        "researchOnly": True,
        "source": "frozen shadow probability archive, not a live betting tip",
        "sourceArchive": file.name,
        "generatedAtUTC": datetime.now(timezone.utc).isoformat(),
        "forecastComputedAtUTC": at.isoformat(),
        "certificationStatus": "BLOCKED",
        "certifiedStrong": 0,
        "bookmakerOddsCertified": False,
        "EV_Certified": False,
        "ROI_Certified": False,
        "apiCalls": 0,
        "truncated": len(records) > MAX_ROWS,
        "totalFrozenForecastRows": len(records),
        "independentlyVerifiedFixtureCount": sum(
            bool(r["independentApiFootballFixtureVerified"])
            for r in records[:MAX_ROWS]
        ),
        "matches": records[:MAX_ROWS],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    args = parser.parse_args()
    root = args.repository_root.resolve()
    result = build(root)
    output = root / OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                 allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps({"previewMatches":len(result["matches"]),
                      "source":result.get("sourceArchive"),
                      "certifiedStrong":0,"providerCalls":0}))


if __name__ == "__main__":
    main()
