#!/usr/bin/env python3
"""Read-only, chronological coverage audit for StatMaker Betting V2.

Usage:
  python research/betting_v2/audit_data.py --data-root /path/to/StatMaker-Data \
    --output /tmp/betting-v2-coverage.json

This is NOT model training, backtesting or forecast certification.
It deliberately never generates recommendations, odds or model probabilities.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

PREMATCH_SAMPLE_MIN = 8
VENUE_SAMPLE_MIN = 3
XG_SAMPLE_MIN = 8
REQUIRED_MARKET_GROUPS = {
    "match_goals": ("HxG", "AxG"),
    "team_goals": ("HxG", "AxG"),
    "match_corners": ("HC", "AC"),
    "team_corners": ("HC", "AC"),
    "shots": ("HS", "AS"),
    "shots_on_target": ("HST", "AST"),
    "cards": ("HY", "AY"),
}


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or not -1e100 < f < 1e100:
        return None
    return f


def utc_date(value: str) -> datetime | None:
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            return None
        return date.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def read_fixtures(data_root: Path) -> tuple[list[dict], Counter, dict]:
    index_path = data_root / "data/statmaker/domestic_enriched/index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("schema_version") != 3:
        raise ValueError("Unknown domestic enriched index contract")
    records: list[dict] = []
    errors: Counter = Counter()
    groups: dict[str, dict] = {}
    seen_fixture_ids: set[tuple[str, str]] = set()
    for league in index.get("leagues", []):
        rel = Path(league.get("output_path", ""))
        if rel.is_absolute() or ".." in rel.parts:
            errors["unsafe_data_path"] += 1
            continue
        path = data_root / rel
        if not path.is_file():
            errors["missing_file"] += 1
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 3:
            errors["unknown_file_schema"] += 1
            continue
        comp = data.get("competition", {})
        code = str(comp.get("league_code", ""))
        group = f"{code}|{comp.get('league', '')}"
        g = groups.setdefault(group, {
            "league": comp.get("league"), "country": comp.get("country"),
            "leagueCode": code, "seasons": [], "publishedCompleted": 0,
            "publishedXgCoverage": [], "rawCount": 0,
        })
        g["seasons"].append(str(comp.get("app_season", "")))
        g["publishedCompleted"] += int(data.get("readiness", {}).get("completed_fixtures") or 0)
        g["publishedXgCoverage"].append(
            data.get("readiness", {}).get("group_coverage", {}).get("xg")
        )
        for match in data.get("matches", []):
            if match.get("status") not in {"FT", "AET", "PEN"}:
                errors["unfinished"] += 1
                continue
            dt = utc_date(match.get("date_utc", ""))
            home = str(match.get("home_team", "")).strip()
            away = str(match.get("away_team", "")).strip()
            hg = number(match.get("home_goals"))
            ag = number(match.get("away_goals"))
            if not (dt and home and away and hg is not None and ag is not None
                    and hg >= 0 and ag >= 0 and hg.is_integer() and ag.is_integer()):
                errors["missing_match_identity_or_score"] += 1
                continue
            fixture = str(match.get("fixture_id") or f"{dt.isoformat()}|{home}|{away}")
            key = (group, fixture)
            if key in seen_fixture_ids:
                errors["duplicate_fixture_id"] += 1
                continue
            seen_fixture_ids.add(key)
            stats = match.get("normalized_stats") or {}
            records.append({
                "group": group, "fixture_id": fixture, "date": dt,
                "home": home.casefold(), "away": away.casefold(),
                "hg": int(hg), "ag": int(ag),
                "stats": stats,
            })
            g["rawCount"] += 1
    return records, errors, groups


def audit(data_root: Path) -> dict:
    matches, errors, groups = read_fixtures(data_root)
    samples: dict[str, Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    feature_coverage: Counter = Counter()

    # Never evaluate today's fixtures using today's final statistics.
    # Date batches are built from strictly PREVIOUS match days.
    days: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for m in matches:
        days[(m["group"], m["date"].date().isoformat())].append(m)
    history: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "played": 0, "venue": Counter(), "xg": 0, "venue_xg": Counter()
    })

    for group, day in sorted(days):
        fixtures = days[(group, day)]
        for m in fixtures:
            totals["completed"] += 1
            h = history[(group, m["home"])]
            a = history[(group, m["away"])]
            full_xg = number(m["stats"].get("HxG")) is not None and \
                number(m["stats"].get("AxG")) is not None
            if full_xg:
                feature_coverage["completed_with_both_xg"] += 1
            for market, fields in REQUIRED_MARKET_GROUPS.items():
                if all(number(m["stats"].get(k)) is not None for k in fields):
                    feature_coverage[f"market_history_{market}"] += 1

            # Diagnostic only: readiness of the PREMATCH historical window.
            # ELO can be derived from prior scored games but is NOT yet certified.
            long_form = min(h["played"], a["played"]) >= PREMATCH_SAMPLE_MIN
            venue_form = h["venue"]["home"] >= VENUE_SAMPLE_MIN and \
                a["venue"]["away"] >= VENUE_SAMPLE_MIN
            xg_form = min(h["xg"], a["xg"]) >= XG_SAMPLE_MIN
            xg_venue = h["venue_xg"]["home"] >= VENUE_SAMPLE_MIN and \
                a["venue_xg"]["away"] >= VENUE_SAMPLE_MIN
            if long_form:
                samples[group]["long_form_ready"] += 1
            if venue_form:
                samples[group]["home_away_ready"] += 1
            if xg_form:
                samples[group]["xg_history_ready"] += 1
            if long_form and venue_form and xg_form and xg_venue:
                samples[group]["basic_pre_match_feature_window"] += 1
                for market, fields in REQUIRED_MARKET_GROUPS.items():
                    if all(number(m["stats"].get(k)) is not None for k in fields):
                        samples[group][f"historically_ready_{market}"] += 1

        # Only after every fixture from the same date has been assessed:
        for m in fixtures:
            h = history[(group, m["home"])]
            a = history[(group, m["away"])]
            h["played"] += 1
            a["played"] += 1
            h["venue"]["home"] += 1
            a["venue"]["away"] += 1
            if number(m["stats"].get("HxG")) is not None:
                h["xg"] += 1
                h["venue_xg"]["home"] += 1
            if number(m["stats"].get("AxG")) is not None:
                a["xg"] += 1
                a["venue_xg"]["away"] += 1

    leagues = []
    for group in sorted(groups):
        meta = groups[group]
        leagues.append({
            **meta,
            "historicalWindowReadiness": dict(samples[group]),
        })

    return {
        "contract": "betting-v2-feature-audit-v1",
        "source": "StatMaker-Data/main, domestic_enriched schema v3",
        "readOnly": True,
        "notForecastCertification": True,
        "prematchPolicy": "prior match dates only; all same-date outcomes excluded",
        "minimumHistoryForDiagnostics": {
            "overallPerTeam": PREMATCH_SAMPLE_MIN,
            "venuePerTeam": VENUE_SAMPLE_MIN,
            "priorXgMatchesPerTeam": XG_SAMPLE_MIN
        },
        "totals": dict(totals),
        "coverage": dict(feature_coverage),
        "sourceErrors": dict(errors),
        "leagues": leagues
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = audit(args.data_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print("Audited completed matches:", output["totals"].get("completed", 0))
    print("Output:", args.output)
    print("No market prices evaluated. No predictions or certified selections produced.")


if __name__ == "__main__":
    main()
