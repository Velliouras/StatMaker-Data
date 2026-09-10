#!/usr/bin/env python3
"""Audit the exact Domestic team identity resolver used by production ingestion.

This audit intentionally does not have its own mapping algorithm. It installs the
same production expansion and uses canonical_team_identity for every provider team.
Any unresolved/ambiguous team remains unresolved; similarity suggestions are not
promoted into production mappings.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List

import canonical_team_identity
import domestic_live_july_pipeline as pipeline
import domestic_odds_expansion
import update_domestic_odds_api_io as odds

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "domestic_mapping_audit.json"
MIN_REMAINING = 25


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def event_teams(events: Iterable[Dict[str, Any]]) -> List[str]:
    names = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        for name in (odds.event_home(event), odds.event_away(event)):
            if name:
                names.add(name)
    return sorted(names)


def main() -> int:
    api_key = os.getenv("ODDS_API_IO_KEY", "").strip()
    if not api_key:
        raise SystemExit("ODDS_API_IO_KEY is required")

    domestic_odds_expansion.install(odds, pipeline)

    registry_root = pipeline.load_json(pipeline.REGISTRY_PATH, {})
    registry = registry_root.get("leagues", []) if isinstance(registry_root, dict) else []
    selected = [row for row in registry if bool(row.get("enabledForOdds", True))]
    if not selected:
        raise SystemExit("Domestic registry has no enabled odds leagues")

    indexes = canonical_team_identity.domestic_indexes(odds, pipeline, selected)
    debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}
    providers = odds.discover_provider_leagues(api_key, debug)

    league_rows = []
    team_rows = []
    for row in selected:
        code = str(row.get("leagueCode") or "").strip().upper()
        provider = odds.match_provider_league(row, providers)
        league_rows.append({
            "leagueCode": code,
            "country": row.get("country"),
            "competition": row.get("competition"),
            "configuredSlug": row.get("providerLeagueSlug"),
            "productionProvider": odds.provider_league_summary(provider) if provider else None,
        })
        if provider is None:
            continue

        remaining = debug.get("rateLimitRemaining")
        if isinstance(remaining, int) and remaining <= MIN_REMAINING:
            debug.setdefault("warnings", []).append("Stopped team audit at rate-limit guard")
            break

        events = odds.fetch_events_for_league(
            api_key,
            str(provider.get("slug") or ""),
            int(registry_root.get("horizonDays") or 21),
            debug,
        )
        index = indexes.get(code)
        mapped = []
        unresolved = []
        for provider_team in event_teams(events):
            canonical, status, candidates = (
                index.resolve(provider_team) if index is not None else (None, "missing-index", [])
            )
            item = {
                "providerTeam": provider_team,
                "canonicalTeam": canonical,
                "status": status,
                "candidates": list(candidates),
            }
            (mapped if canonical is not None else unresolved).append(item)

        team_rows.append({
            "leagueCode": code,
            "providerLeagueSlug": provider.get("slug"),
            "eventsFetched": len(events),
            "providerTeamCount": len(mapped) + len(unresolved),
            "mappedTeamCount": len(mapped),
            "unresolvedTeamCount": len(unresolved),
            "mapped": mapped,
            "unresolved": unresolved,
        })

    unresolved_count = sum(int(row.get("unresolvedTeamCount") or 0) for row in team_rows)
    payload = {
        "mode": "production-canonical-team-identity-audit",
        "identityResolver": "canonical_team_identity",
        "productionOddsTouched": False,
        "bettingEngineTouched": False,
        "selectedLeagueCount": len(selected),
        "teamAuditedLeagueCount": len(team_rows),
        "leagueMappings": league_rows,
        "teamMappings": team_rows,
        "unresolvedTeamCount": unresolved_count,
        "rateLimitRemaining": debug.get("rateLimitRemaining"),
        "apiCallCount": len(debug.get("apiCalls", [])),
        "warnings": debug.get("warnings", []),
    }
    save(REPORT, payload)
    print(json.dumps({
        "selectedLeagueCount": len(selected),
        "teamAuditedLeagueCount": len(team_rows),
        "unresolvedTeamCount": unresolved_count,
        "rateLimitRemaining": debug.get("rateLimitRemaining"),
        "apiCallCount": len(debug.get("apiCalls", [])),
    }, ensure_ascii=False, indent=2))
    return 1 if unresolved_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
