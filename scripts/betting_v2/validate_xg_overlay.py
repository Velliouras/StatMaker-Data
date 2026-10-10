#!/usr/bin/env python3
"""Read-only strict integrity + temporal firewall for separately recovered xG.

Provenance-approved recovery is NOT a license to substitute providers in an
existing calibrated model. Research overlay must never rewrite past predictions.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path

from publish_shadow import _safe_output, _atomic_write


def as_utc(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("Missing timestamp")
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Timezone required")
    return value.astimezone(timezone.utc)


def xg(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (ValueError, TypeError):
        return None
    return v if isfinite(v) and 0 <= v <= 20 else None


def inspect_overlay(overlay: dict, enriched: dict) -> dict:
    if overlay.get("contract") != "betting-v2-understat-xg-research-overlay-v1":
        raise ValueError("Unknown overlay contract")
    if (overlay.get("certified") is not False or
            overlay.get("historicalAsOfVerified") is not False or
            overlay.get("canonicalDataModified") is not False or
            overlay.get("mayBackfillTrainingHistory") is not False or
            overlay.get("mayCertifyPicks") is not False or
            overlay.get("directUnderstatVerified") is not False):
        raise ValueError("Overlay must remain read-only, uncertified and provenance-limited")
    if overlay.get("sourceModelsMayDiffer") is not True:
        raise ValueError("Mixing different provider xG models is forbidden")
    if (not isinstance(overlay.get("sourceCommit"), str) or
            len(overlay["sourceCommit"]) != 40 or
            not isinstance(overlay.get("sourceBlobSha"), str) or
            len(overlay["sourceBlobSha"]) != 40):
        raise ValueError("Missing immutable mirror Git identifiers")
    published = as_utc(overlay.get("sourceCommitTimestampUTC"))
    observed = as_utc(overlay.get("sourceObservedAtUTC"))
    if observed < published:
        raise ValueError("Source cannot be observed before its published commit")
    if str((enriched.get("competition") or {}).get("league_code")) != overlay.get("leagueCode"):
        raise ValueError("League identity mismatch")
    index = {}
    for m in enriched.get("matches") or []:
        fid = str(m.get("fixture_id") or "")
        if not fid or fid in index:
            raise ValueError("Missing or duplicate canonical fixture IDs")
        index[fid] = m
    reasons = Counter()
    fixture_ids, upstream_ids = set(), set()
    verified = []
    for entry in overlay.get("recovered") or []:
        fid = str(entry.get("fixtureId") or "")
        sid = str(entry.get("understatMatchId") or "")
        if not fid or fid in fixture_ids or not sid or sid in upstream_ids:
            reasons["duplicate_or_missing_identifier"] += 1
            continue
        fixture_ids.add(fid)
        upstream_ids.add(sid)
        original = index.get(fid)
        if original is None or original.get("status") not in {"FT", "AET", "PEN"}:
            reasons["no_canonical_completed_match"] += 1
            continue
        try:
            kickoff = as_utc(entry["kickoffUTC"])
            canonical_kickoff = as_utc(original["date_utc"])
        except (KeyError, ValueError, TypeError):
            reasons["invalid_kickoff"] += 1
            continue
        if abs((kickoff - canonical_kickoff).total_seconds()) > 900 or kickoff >= published:
            reasons["kickoff_mismatch_or_not_before_source_commit"] += 1
            continue
        if (str(entry.get("leagueCode")) != overlay.get("leagueCode") or
                str(entry.get("homeTeam")) != str(original.get("home_team")) or
                str(entry.get("awayTeam")) != str(original.get("away_team")) or
                str(entry.get("homeGoals")) != str(original.get("home_goals")) or
                str(entry.get("awayGoals")) != str(original.get("away_goals"))):
            reasons["team_or_score_mismatch"] += 1
            continue
        h, a = xg(entry.get("homeXg")), xg(entry.get("awayXg"))
        if h is None or a is None:
            reasons["invalid_xg"] += 1
            continue
        primary = original.get("normalized_stats") or {}
        existing_h, existing_a = xg(primary.get("HxG")), xg(primary.get("AxG"))
        if existing_h is not None and abs(existing_h - h) > 0.01:
            reasons["would_overwrite_canonical_home_xg"] += 1
            continue
        if existing_a is not None and abs(existing_a - a) > 0.01:
            reasons["would_overwrite_canonical_away_xg"] += 1
            continue
        if existing_h is not None and existing_a is not None:
            reasons["not_actually_missing_xg"] += 1
            continue
        if entry.get("historicalAsOfVerified") is not False:
            reasons["incorrect_temporal_claim"] += 1
            continue
        verified.append(fid)
    return {
        "contract": "betting-v2-validated-xg-overlay-v1",
        "certified": False,
        "providerCalls": 0,
        "source": overlay["source"],
        "sourceCommit": overlay["sourceCommit"],
        "mirrorSourceObservedUTC": observed.isoformat(),
        "readyForHistoricalBacktestBeforeSourceCommit": False,
        "canMixDifferentXgModelsWithoutRecalibration": False,
        "canPublishStrong": False,
        "submitted": len(overlay.get("recovered") or []),
        "validResearchOverlayRows": len(verified),
        "rejectedByReason": dict(sorted(reasons.items())),
        "allRowsPassed": len(verified) == len(overlay.get("recovered") or []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path,
                        default=Path("reports/betting_v2/recovered_xg_integrity.json"))
    args = parser.parse_args()
    root = args.repository_root.resolve()
    overlay_path = _safe_output(root, args.input)
    overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    index = json.loads((root / "data/statmaker/domestic_enriched/index.json").read_text())
    chosen = [i for i in index["leagues"]
              if i.get("league_code") == overlay.get("leagueCode") and
              str(i.get("api_football_season")) == str(overlay.get("understatSeason"))]
    if len(chosen) != 1:
        raise ValueError("Ambiguous canonical league/season match")
    path = (root / chosen[0]["output_path"]).resolve()
    if not path.is_relative_to(root / "data/statmaker/domestic_enriched"):
        raise ValueError("Unsafe canonical fixture path")
    canonical = json.loads(path.read_text())
    outcome = inspect_overlay(overlay, canonical)
    _atomic_write(_safe_output(root, args.output), outcome)
    print(json.dumps(outcome, ensure_ascii=False))


if __name__ == "__main__":
    main()
