#!/usr/bin/env python3
"""Offline verification of canonical xG against cached provider observations.

READ ONLY. Never substitutes, synthesizes, estimates, or fetches missing xG.
The raw provider fixture and enriched fixture must have the same fixture ID.
This is a provenance/coverage audit, not a model backtest or pick publisher.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from math import isfinite
from pathlib import Path
import re
import unicodedata


def numeric_xg(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if isfinite(x) and 0 <= x <= 20 else None


def canonical_team(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def observed_raw_xg(match: dict) -> tuple[float, float] | None:
    """Use provider expected_goals, verifying which team each stat belongs to."""
    rows = match.get("raw_statistics")
    if not isinstance(rows, list) or len(rows) != 2:
        return None
    by_team = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        team = canonical_team((row.get("team") or {}).get("name"))
        if not team or team in by_team:
            return None
        found = []
        for field in row.get("statistics") or []:
            if not isinstance(field, dict):
                continue
            label = re.sub(r"[^a-z0-9]+", "", str(field.get("type") or "").lower())
            if label in {"expectedgoals", "xg"}:
                found.append(numeric_xg(field.get("value")))
        if len(found) != 1 or found[0] is None:
            return None
        by_team[team] = found[0]
    home = canonical_team(match.get("home_team"))
    away = canonical_team(match.get("away_team"))
    if not home or home == away or home not in by_team or away not in by_team:
        return None
    return by_team[home], by_team[away]


def safe_source(root: Path, raw: object, prefix: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Missing archived source path")
    path = (root / raw).resolve()
    if not path.is_relative_to(root.resolve()) or not path.relative_to(root.resolve()).as_posix().startswith(prefix):
        raise ValueError("Archived path outside approved cached source directory")
    if path.suffix != ".json":
        raise ValueError("Archived source must be a JSON file")
    return path


def audit_pair(enriched: dict, raw: dict) -> dict:
    """Compare one published season with its original cache by provider ID."""
    source = raw.get("fixtures")
    if not isinstance(source, list):
        raise ValueError("Raw cache has no fixture list")
    by_id = {}
    duplicate = set()
    for m in source:
        if not isinstance(m, dict) or not m.get("fixture_id"):
            continue
        fid = str(m["fixture_id"])
        if fid in by_id:
            duplicate.add(fid)
        by_id[fid] = m
    items = enriched.get("matches")
    if not isinstance(items, list):
        raise ValueError("Enriched cache has no match list")
    months = defaultdict(lambda: defaultdict(int))
    seen = set()
    for m in items:
        if not isinstance(m, dict) or m.get("status") not in {"FT", "AET", "PEN"}:
            continue
        dt = m.get("date_utc")
        if not isinstance(dt, str):
            continue
        try:
            parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("no timezone")
        except ValueError:
            continue
        month = dt[:7]
        counts = months[month]
        counts["completed"] += 1
        fid = str(m.get("fixture_id") or "")
        if not fid or fid in seen or fid in duplicate:
            counts["invalidOrDuplicateFixtureIdentity"] += 1
            continue
        seen.add(fid)
        source_match = by_id.get(fid)
        if source_match is None:
            counts["missingRawFixture"] += 1
            continue
        try:
            raw_dt = datetime.fromisoformat(
                str(source_match.get("date") or "").replace("Z", "+00:00")
            )
            if raw_dt.tzinfo is None or abs((raw_dt - parsed).total_seconds()) > 900:
                raise ValueError("raw kickoff differs from canonical fixture")
        except ValueError:
            counts["rawKickoffMismatch"] += 1
            continue
        if (canonical_team(source_match.get("home_team")) != canonical_team(m.get("home_team")) or
                canonical_team(source_match.get("away_team")) != canonical_team(m.get("away_team"))):
            counts["rawTeamsMismatch"] += 1
            continue
        normalized = m.get("normalized_stats") or {}
        enriched_xg = (numeric_xg(normalized.get("HxG")), numeric_xg(normalized.get("AxG")))
        if None not in enriched_xg:
            counts["publishedBothXg"] += 1
        source_normalized = source_match.get("normalized_stats") or {}
        source_xg = (numeric_xg(source_normalized.get("HxG")),
                     numeric_xg(source_normalized.get("AxG")))
        if None not in source_xg:
            counts["sourceNormalizedBothXg"] += 1
        if source_xg != enriched_xg:
            counts["sourceVsPublishedXgMismatch"] += 1
        raw_xg = observed_raw_xg(source_match)
        if raw_xg is not None:
            counts["providerObservedBothXg"] += 1
            if (source_xg[0] is None or source_xg[1] is None or
                    abs(raw_xg[0] - source_xg[0]) > 0.011 or
                    abs(raw_xg[1] - source_xg[1]) > 0.011):
                counts["rawVsSourceNormalizedXgMismatch"] += 1
        else:
            counts["rawXgNotIndependentlyVerified"] += 1
    measures = ("completed", "invalidOrDuplicateFixtureIdentity", "missingRawFixture",
                "rawKickoffMismatch", "rawTeamsMismatch", "publishedBothXg",
                "sourceNormalizedBothXg", "sourceVsPublishedXgMismatch",
                "providerObservedBothXg", "rawVsSourceNormalizedXgMismatch",
                "rawXgNotIndependentlyVerified")
    return {k: {metric: v.get(metric, 0) for metric in measures} for k, v in sorted(months.items())}


def audit_repository(root: Path, leagues: set[str] | None = None) -> dict:
    root = root.resolve()
    index = json.loads((root / "data/statmaker/domestic_enriched/index.json").read_text(encoding="utf-8"))
    if index.get("schema_version") != 3:
        raise ValueError("Unknown enriched index schema")
    reports = []
    for entry in index.get("leagues") or []:
        code = str(entry.get("league_code") or "")
        if leagues and code not in leagues:
            continue
        label = {"leagueCode": code, "season": str(entry.get("app_season") or "")}
        try:
            normalized_path = safe_source(root, entry.get("output_path"), "data/statmaker/domestic_enriched/")
            source_path = safe_source(root, entry.get("cache_path"), "data/api_football/fixture_stats/")
            enriched = json.loads(normalized_path.read_text(encoding="utf-8"))
            raw = json.loads(source_path.read_text(encoding="utf-8"))
            if str((enriched.get("competition") or {}).get("league_code")) != code:
                raise ValueError("Published cache competition does not match index")
            label["monthly"] = audit_pair(enriched, raw)
            label["status"] = "READ_ONLY_AUDITED"
        except (OSError, ValueError, TypeError) as exc:
            label["status"] = "UNAVAILABLE_OR_INVALID_CACHE"
            label["error"] = str(exc)[:180]
        reports.append(label)
    return {
        "contract": "betting-v2-xg-cache-provenance-v1",
        "certified": False,
        "providerCalls": 0,
        "source": "LOCAL_CACHED_PROVIDER_FIXTURES_ONLY",
        "noSyntheticXg": True,
        "leagues": reports,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, required=True)
    ap.add_argument("--league-codes", nargs="*", default=[])
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    report = audit_repository(args.repository_root, set(args.league_codes) or None)
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(content, end="")
    else:
        from publish_shadow import _safe_output
        target = _safe_output(args.repository_root, args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        print(f"Written {target}; provider calls = 0; certificates = 0")


if __name__ == "__main__":
    main()
