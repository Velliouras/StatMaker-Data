#!/usr/bin/env python3
"""Offline, read-only archive/SQLite feasibility preflight for Betting V2.

Counts *legacy prepared* quotes only. Never interprets them as an independent
bookmaker universe or as certified betting evidence. Never calls providers.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from io import BytesIO
import json
from math import isfinite
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from zoneinfo import ZoneInfo
from zipfile import BadZipFile, ZipFile

from export_historical_prices import (
    datetime_kickoff, snapshot_for, source_fixture_id,
    verified_archive_timestamp,
)
from publish_shadow import _atomic_write, _safe_output

ATHENS = ZoneInfo("Europe/Athens")
MAX_DATABASE_BYTES = 150_000_000
REQUIRED_MATCH = {"competition_id", "snapshot_version", "match_key", "payload"}
REQUIRED_SELECTION = {"competition_id", "snapshot_version", "match_key",
                      "selection_key", "selection_odd", "identity_sub_market_key",
                      "identity_selection_side", "identity_team_side", "identity_line"}


def examine_database(blob: bytes, target: date, cutoff: datetime) -> dict:
    """Inspect source rows without modifying original bytes or producing odds."""
    if not blob or len(blob) > MAX_DATABASE_BYTES:
        raise ValueError("Archived SQLite is empty or oversized")
    reasons: Counter = Counter()
    markets: Counter = Counter()
    id_present = 0
    considered = 0
    total = 0
    seen = set()
    with tempfile.TemporaryDirectory(prefix="sm-v2-preflight-") as td:
        path = Path(td) / "archive.db"
        path.write_bytes(blob)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            def columns(table: str) -> set[str]:
                return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            absent_match = REQUIRED_MATCH - columns("prepared_matches")
            absent_selection = REQUIRED_SELECTION - columns("prepared_selections")
            if absent_match or absent_selection:
                raise ValueError(
                    f"Archive schema mismatch: matches={sorted(absent_match)}, "
                    f"selections={sorted(absent_selection)}")
            statement = """
                SELECT s.competition_id, s.snapshot_version, s.match_key,
                       s.selection_key, s.selection_odd,
                       s.identity_sub_market_key, s.identity_selection_side,
                       s.identity_team_side, s.identity_line, m.payload
                FROM prepared_selections AS s
                LEFT JOIN prepared_matches AS m
                ON m.competition_id=s.competition_id
                   AND m.snapshot_version=s.snapshot_version
                   AND m.match_key=s.match_key
            """
            for row in conn.execute(statement):
                total += 1
                if row["payload"] is None:
                    reasons["MISSING_MATCH_HEADER"] += 1
                    continue
                try:
                    match = json.loads(row["payload"])
                except (TypeError, ValueError):
                    reasons["MALFORMED_MATCH_HEADER"] += 1
                    continue
                if not isinstance(match, dict):
                    reasons["MALFORMED_MATCH_HEADER"] += 1
                    continue
                league = str(match.get("leagueCode") or "").strip()
                if not league or not match.get("homeTeam") or not match.get("awayTeam"):
                    reasons["MISSING_LEAGUE_OR_TEAMS"] += 1
                    continue
                kick = datetime_kickoff(match)
                if kick is None:
                    reasons["UNKNOWN_ABSOLUTE_KICKOFF"] += 1
                    continue
                if kick <= cutoff.astimezone(kick.tzinfo):
                    reasons["MATCH_ALREADY_STARTED"] += 1
                    continue
                if kick.astimezone(ATHENS).date() != target:
                    reasons["NOT_TARGET_ATHENS_DATE"] += 1
                    continue
                unique = (str(row["competition_id"]), str(row["snapshot_version"]),
                          str(row["selection_key"]))
                if not unique[-1].strip():
                    reasons["MISSING_SELECTION_KEY"] += 1
                    continue
                if unique in seen:
                    reasons["DUPLICATE_SELECTION_KEY"] += 1
                    continue
                seen.add(unique)
                try:
                    odd = float(row["selection_odd"])
                except (TypeError, ValueError):
                    reasons["BAD_ODD"] += 1
                    continue
                if not isfinite(odd) or not 1.8 < odd < 3.0:
                    reasons["OUTSIDE_V2_ODDS_WINDOW"] += 1
                    continue
                if not row["identity_sub_market_key"] or not row["identity_selection_side"]:
                    reasons["MISSING_MARKET_IDENTITY"] += 1
                    continue
                considered += 1
                markets[str(row["identity_sub_market_key"])] += 1
                if source_fixture_id(match) is not None:
                    id_present += 1
        finally:
            conn.close()
    return {
        "preparedSelectionRows": total,
        "eligibleBeforeExactFixtureJoin": considered,
        "eligibleWithExplicitFixtureId": id_present,
        "marketIdentityCounts": dict(sorted(markets.items())),
        "excludedReasons": dict(sorted(reasons.items())),
        "priceUniverse": "LEGACY_PREPARED_SELECTIONS",
        "actualPriceObservationTimeVerified": False,
        "independentUnfilteredBookmakerUniverseVerified": False,
        "certified": False,
    }


def inspect_day(root: Path, target: date) -> dict:
    cutoff = datetime.combine(target, time(11, 0), ATHENS)
    try:
        snapshot = snapshot_for(root, target)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        return {"date": target.isoformat(), "status": "INVALID_HISTORICAL_MANIFEST",
                "reason": type(exc).__name__}
    if snapshot is None:
        return {"date": target.isoformat(), "status": "NO_PRE_CUTOFF_MANIFEST"}
    commit, manifest = snapshot
    artifact = next((x for x in manifest.get("artifacts", [])
                     if x.get("id") == "app_ready_betting_bundle"), None)
    if not artifact:
        return {"date": target.isoformat(), "status": "NO_BETTING_BUNDLE"}
    generated = verified_archive_timestamp(artifact, cutoff)
    if generated is None:
        return {"date": target.isoformat(), "status": "UNVERIFIED_BUNDLE_GENERATION"}
    path = Path(str(artifact.get("path") or ""))
    if (path.is_absolute() or ".." in path.parts or path.suffix != ".zip" or
            not path.as_posix().startswith("data/statmaker/app_ready/")):
        return {"date": target.isoformat(), "status": "UNSAFE_ARCHIVE_REFERENCE"}
    try:
        result = subprocess.run(["git", "show", f"{commit}:{path.as_posix()}"],
                                cwd=root, check=True, capture_output=True).stdout
    except subprocess.CalledProcessError:
        return {"date": target.isoformat(), "status": "ARCHIVED_ZIP_MISSING"}
    if sha256(result).hexdigest() != artifact.get("sha256"):
        return {"date": target.isoformat(), "status": "CHECKSUM_MISMATCH"}
    try:
        with ZipFile(BytesIO(result)) as archive:
            info = archive.getinfo("databases/statmaker_prepared_betting.db")
            if info.file_size > MAX_DATABASE_BYTES:
                return {"date": target.isoformat(), "status": "OVERSIZED_DATABASE"}
            blob = archive.read(info)
        details = examine_database(blob, target, cutoff)
    except (BadZipFile, KeyError, ValueError, sqlite3.DatabaseError) as exc:
        return {"date": target.isoformat(), "status": "INVALID_ARCHIVE_OR_SCHEMA",
                "reason": str(exc)[:220]}
    return {"date": target.isoformat(), "status": "DIAGNOSTIC_ONLY",
            "sourceCommit": commit, "bundleGeneratedAt": generated.isoformat(),
            **details}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    ap.add_argument("--from-date", type=date.fromisoformat, required=True)
    ap.add_argument("--to-date", type=date.fromisoformat, required=True)
    ap.add_argument("--output", type=Path,
                    default=Path("reports/betting_v2/archive_preflight.json"))
    args = ap.parse_args()
    if args.to_date < args.from_date or (args.to_date - args.from_date).days > 13:
        ap.error("At most 14 explicit days; end must not precede start")
    root = args.repository_root.resolve()
    destination = _safe_output(root, args.output)
    reports = []
    day = args.from_date
    while day <= args.to_date:
        reports.append(inspect_day(root, day))
        day += timedelta(days=1)
    result = {"contract": "betting-v2-archive-preflight-v1", "certified": False,
              "apiCalls": 0, "doesNotProveROI": True, "days": reports}
    _atomic_write(destination, result)
    print(json.dumps({"daysChecked": len(reports), "statuses": [r["status"] for r in reports],
                      "apiCalls": 0, "certified": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
