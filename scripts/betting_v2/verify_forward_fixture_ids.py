#!/usr/bin/env python3
"""Verify existing frozen shadow forecasts against independent cached API-Football schedules.

This is an OFFLINE evidence crosswalk. It never modifies original immutable
forecasts, assumes a past provider price update time or certifies STRONG/EV/ROI.
The cache schedule is evidence available WHEN CHECKED; it is not a retroactive
proof that API-Football fixture ID existed at the original bookmaker quote time.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path

from fixture_lookup import as_utc
from future_fixture_crosswalk import CachedUpcomingFixtureIndex

ARCHIVE = "reports/betting_v2/forward_snapshots"
MAX_FILES = 40
MAX_COMPRESSED = 1_000_000
MAX_DECOMPRESSED = 5_000_000


def _load_snapshot(path: Path) -> dict:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_COMPRESSED:
        raise ValueError("Unsafe compressed snapshot")
    with gzip.open(path, "rb") as stream:
        payload = stream.read(MAX_DECOMPRESSED + 1)
    if len(payload) > MAX_DECOMPRESSED:
        raise ValueError("Expanded snapshot exceeded cap")
    doc = json.loads(payload)
    if not isinstance(doc, dict) or (
        doc.get("contract") != "statmaker-v2-immutable-shadow-snapshot-set-v1"
    ) or doc.get("certificationStatus") != "BLOCKED" or (
        doc.get("realStrongSelections") != 0
    ):
        raise ValueError("Not a valid non-certified frozen snapshot")
    if not isinstance(doc.get("forecastData"), list):
        raise ValueError("Missing frozen forecast rows")
    if not isinstance(doc.get("forecastComputedAtUTC"), str):
        raise ValueError("Missing as-of date")
    if len(doc["forecastData"]) != doc.get("forecastCount"):
        raise ValueError("Forecast row count mismatch")
    if len(str(doc.get("historicalFeatureArchiveSha256"))) != 64:
        raise ValueError("Missing historical source fingerprint")
    if len(str(doc.get("providerScheduleSnapshotSha256"))) != 64:
        raise ValueError("Missing provider source fingerprint")
    return doc


def audit(root: Path, now: datetime, *, max_files: int = MAX_FILES,
          index: CachedUpcomingFixtureIndex | None = None) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Verifier time must be timezone-aware")
    if not 1 <= max_files <= MAX_FILES:
        raise ValueError("Invalid bounded archive count")
    root = root.resolve()
    folder = root / ARCHIVE
    paths = sorted(folder.glob("*.json.gz")) if folder.is_dir() else []
    truncated = len(paths) > max_files
    paths = paths[-max_files:]
    fixture_index = index or CachedUpcomingFixtureIndex(root)
    reasons = Counter()
    verified = []
    covered = set()
    total = 0
    for path in paths:
        try:
            doc = _load_snapshot(path)
        except (OSError, EOFError, ValueError, TypeError, KeyError,
                json.JSONDecodeError):
            reasons["CORRUPT_OR_UNVERIFIED_FROZEN_SNAPSHOT"] += 1
            continue
        asof = as_utc(doc["forecastComputedAtUTC"])
        if asof is None or asof > now.astimezone(timezone.utc):
            reasons["FUTURE_OR_UNVERIFIED_SNAPSHOT_TIMESTAMP"] += 1
            continue
        seen = set()
        for row in doc["forecastData"]:
            total += 1
            if not isinstance(row, dict):
                reasons["INVALID_SHADOW_ROW"] += 1
                continue
            source_event = str(row.get("providerEventId") or "")
            if not source_event or (
                str(row.get("contract")) != "statmaker-v2-immutable-shadow-forecast-v1"
            ):
                reasons["UNVERIFIED_FORECAST_CONTRACT"] += 1
                continue
            if (row.get("certifiedStrong") is not False or
                row.get("researchOnly") is not True or
                row.get("noObservedTargetScoreUsed") is not True or
                "homeGoals" in row or "awayGoals" in row or "observed" in row):
                reasons["NOT_PREMATCH_FEATURE_ONLY_FORECAST"] += 1
                continue
            kickoff = as_utc(row.get("kickoffUTC"))
            row_asof = as_utc(row.get("forecastComputedAtUTC"))
            if kickoff is None or row_asof != asof or row_asof >= kickoff:
                reasons["NOT_PROVEN_PRE_KICKOFF_FROZEN_FORECAST"] += 1
                continue
            unique = (str(row.get("leagueCode") or ""), source_event)
            if unique in seen:
                reasons["DUPLICATE_EVENT_IN_FROZEN_SNAPSHOT"] += 1
                continue
            seen.add(unique)
            api, why = fixture_index.resolve({
                "leagueCode": row.get("leagueCode"),
                "home": row.get("providerHomeTeam"),
                "away": row.get("providerAwayTeam"),
                "kickoffUTC": row.get("kickoffUTC"),
            })
            if api is None:
                reasons[why] += 1
                continue
            key = (path.name, api["apiFootballFixtureId"])
            if key in covered:
                reasons["DUPLICATE_INDEPENDENT_FIXTURE_IN_SNAPSHOT"] += 1
                continue
            covered.add(key)
            verified.append({
                "forecastArchive": path.name,
                "forecastComputedAtUTC": row_asof.isoformat(),
                "verifiedAtUTC": now.astimezone(timezone.utc).isoformat(),
                "bookmakerProviderEventId": source_event,
                "leagueCode": row["leagueCode"],
                "apiFootballFixtureId": api["apiFootballFixtureId"],
                "apiFootballLeagueId": api["apiFootballLeagueId"],
                "apiFootballSeason": api["apiFootballSeason"],
                "kickoffUTC": row["kickoffUTC"],
                "strategy": row["strategy"],
                "apiFootballScheduleCachePath": api["cachePath"],
                "historicalFeatureArchiveSha256": doc[
                    "historicalFeatureArchiveSha256"
                ],
                "sourceProviderScheduleSha256": doc[
                    "providerScheduleSnapshotSha256"
                ],
                "fixtureIdentityAsOfOriginalBookmakerQuoteVerified": False,
                "bookmakerPriceUpdateTimestampVerified": False,
                "certifiedStrong": False,
            })
    return {
        "contract": "betting-v2-future-api-football-crosswalk-audit-v1",
        "researchOnly": True,
        "providerCalls": 0,
        "certifiedStrong": 0,
        "certifiedEV": None,
        "certifiedROI": None,
        "checkedAtUTC": now.astimezone(timezone.utc).isoformat(),
        "archivedForecastsScanned": len(paths),
        "archiveTruncated": truncated,
        "forecastRowsExamined": total,
        "independentFixtureCrosswalksVerified": len(verified),
        "unverifiedForecastRows": total - len(verified),
        "independentFixtureCacheEntries": fixture_index.entries,
        "rejected": dict(sorted(reasons.items())),
        "verifiedFixtureLinks": verified,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    ap.add_argument("--report", type=Path,
                    default=Path("reports/betting_v2/pilot_forward_identity_crosswalk.json"))
    args = ap.parse_args()
    root = args.repository_root.resolve()
    result = audit(root, datetime.now(timezone.utc))
    output = (root / args.report).resolve()
    if not output.is_relative_to(root / "reports/betting_v2"):
        raise ValueError("Report is outside research directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n",
                      encoding="utf-8")
    print(json.dumps({
        "scannedForecastRows": result["forecastRowsExamined"],
        "exactIndependentFixtureLinks": result["independentFixtureCrosswalksVerified"],
        "rejected": result["rejected"],
        "providerCalls": 0,
        "certifiedStrong": 0,
    }))


if __name__ == "__main__":
    main()
