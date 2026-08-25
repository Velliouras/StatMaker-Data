#!/usr/bin/env python3
"""Merge the 16-league top-flight expansion into the runtime Domestic registry inputs.

The canonical legacy JSON files remain unchanged so the expansion can be introduced
without rewriting the large historical registry files. The rolling registry builder
merges this overlay before API-Football season discovery, then persists a normal
registry consumed by Stats, Odds, context, app-ready and Android clients.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Iterable

ROOT = Path(__file__).resolve().parents[1]
EXPANSION_PATH = ROOT / "config" / "top_flight_expansion_2026.json"
PRIORITY_GROUP = "top_flight_expansion"


def _load() -> Dict[str, Any]:
    return json.loads(EXPANSION_PATH.read_text(encoding="utf-8-sig"))


def expansion_leagues() -> list[Dict[str, Any]]:
    payload = _load()
    rows = payload.get("leagues", []) if isinstance(payload, dict) else []
    return [dict(row) for row in rows if isinstance(row, dict)]


def expansion_codes() -> set[str]:
    return {
        str(row.get("leagueCode") or "").strip().upper()
        for row in expansion_leagues()
        if str(row.get("leagueCode") or "").strip()
    }


def _merge_rows(existing: Iterable[Dict[str, Any]], additions: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    result = [dict(row) for row in existing if isinstance(row, dict)]
    by_code = {
        str(row.get("leagueCode") or "").strip().upper(): index
        for index, row in enumerate(result)
        if str(row.get("leagueCode") or "").strip()
    }
    for addition in additions:
        code = str(addition.get("leagueCode") or "").strip().upper()
        if not code:
            continue
        if code in by_code:
            result[by_code[code]] = {**result[by_code[code]], **dict(addition)}
        else:
            by_code[code] = len(result)
            result.append(dict(addition))
    return result


def merge_domestic_config(config: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(config or {})
    additions = expansion_leagues()
    result["leagues"] = _merge_rows(result.get("leagues", []) or [], additions)

    codes = [str(row.get("leagueCode") or "").strip().upper() for row in additions]
    groups = result.setdefault("groups", {})
    if isinstance(groups, dict):
        groups[PRIORITY_GROUP] = codes
        for bucket in ("all_blue_yellow", "all_initial"):
            values = [str(value).strip().upper() for value in groups.get(bucket, []) or [] if str(value).strip()]
            groups[bucket] = list(dict.fromkeys(values + codes))

    result["version"] = max(int(result.get("version") or 0), 7)
    result["runtimeExpansion"] = {
        "source": str(EXPANSION_PATH.relative_to(ROOT)),
        "leagueCount": len(codes),
        "leagueCodes": codes,
        "coreOddsPriorityChanged": False,
    }
    return result


def merge_enrichment_config(config: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(config or {})
    additions = []
    for row in expansion_leagues():
        code = str(row.get("leagueCode") or "").strip().upper()
        additions.append({
            "leagueCode": code,
            "continent": row.get("continent"),
            "country": row.get("country"),
            "display_name": row.get("competition"),
            "football_data_code": code,
            "api_football_league_id": row.get("apiFootballLeagueId"),
            "season": "2026",
            "app_season": "2026",
            "enabled": bool(row.get("enabled", True)),
            "priority_group": PRIORITY_GROUP,
        })
    result["leagues"] = _merge_rows(result.get("leagues", []) or [], additions)
    result["version"] = max(int(result.get("version") or 0), 5)
    result["runtimeExpansion"] = {
        "source": str(EXPANSION_PATH.relative_to(ROOT)),
        "leagueCount": len(additions),
    }
    return result


def validate_contract(expected_count: int = 16) -> None:
    rows = expansion_leagues()
    codes = expansion_codes()
    ids = [row.get("apiFootballLeagueId") for row in rows]
    if len(rows) != expected_count or len(codes) != expected_count:
        raise RuntimeError(f"Top-flight expansion must contain {expected_count} unique leagues")
    if len(set(ids)) != expected_count or any(value in (None, "") for value in ids):
        raise RuntimeError("Top-flight expansion must contain unique API-Football league IDs")
    for row in rows:
        if not all(bool(row.get(key)) for key in ("leagueCode", "country", "competition", "apiFootballLeagueId")):
            raise RuntimeError(f"Incomplete top-flight expansion row: {row}")
        if not bool(row.get("enabledForStats", True)):
            raise RuntimeError(f"Expansion league must be Stats-enabled: {row.get('leagueCode')}")


if __name__ == "__main__":
    validate_contract()
    print(f"Top-flight expansion OK: {len(expansion_codes())} leagues")
