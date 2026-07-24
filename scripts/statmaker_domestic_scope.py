#!/usr/bin/env python3
"""Authoritative Domestic scope contracts and runtime guards for StatMaker-Data."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

ROOT = Path(__file__).resolve().parents[1]
SCOPE_PATH = ROOT / "config" / "statmaker_final_domestic_scope.json"


def _load_scope() -> Dict[str, Any]:
    return json.loads(SCOPE_PATH.read_text(encoding="utf-8-sig"))


def normalize_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    return "ROU" if code == "ROM" else code


def _codes(key: str) -> set[str]:
    payload = _load_scope()
    return {normalize_code(code) for code in payload.get(key, []) or []}


def core_odds_codes() -> set[str]:
    codes = _codes("coreOddsLeagueCodes")
    return codes or _codes("includedLeagueCodes")


def included_codes() -> set[str]:
    """Backward-compatible alias for the protected core odds scope."""
    return core_odds_codes()


def stats_universe_codes() -> set[str]:
    codes = _codes("statsUniverseLeagueCodes")
    return codes or included_codes()


def absolute_priority_codes() -> set[str]:
    return _codes("absoluteStatsPriorityLeagueCodes")


def league_code(item: Dict[str, Any]) -> str:
    return normalize_code(
        item.get("leagueCode")
        or item.get("league_code")
        or item.get("football_data_code")
        or item.get("code")
    )


def is_included(item_or_code: Dict[str, Any] | str) -> bool:
    """Backward-compatible check for the protected core odds scope."""
    code = league_code(item_or_code) if isinstance(item_or_code, dict) else normalize_code(item_or_code)
    return bool(code) and code in included_codes()


def is_stats_included(item_or_code: Dict[str, Any] | str) -> bool:
    code = league_code(item_or_code) if isinstance(item_or_code, dict) else normalize_code(item_or_code)
    return bool(code) and code in stats_universe_codes()


def filter_leagues(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Backward-compatible filter for the protected core odds scope."""
    return [row for row in rows if isinstance(row, dict) and is_included(row)]


def filter_stats_leagues(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in rows if isinstance(row, dict) and is_stats_included(row)]


def _filter_registry_payload(payload: Dict[str, Any], *, stats: bool) -> Dict[str, Any]:
    result = dict(payload or {})
    source = result.get("leagues", []) or []
    leagues = filter_stats_leagues(source) if stats else filter_leagues(source)
    result["leagues"] = leagues
    if "leagueCount" in result:
        result["leagueCount"] = len(leagues)
    result["domesticScope"] = {
        "authoritative": True,
        "scopeType": "stats_universe" if stats else "core_odds",
        "configuredLeagueCount": len(stats_universe_codes() if stats else included_codes()),
        "activeRegistryLeagueCount": len(leagues),
        "apiCallsOutsideScopeAllowed": False,
    }
    # Preserve the legacy diagnostic block for older readers.
    result["finalDomesticScope"] = {
        "authoritative": True,
        "includedLeagueCount": len(included_codes()),
        "statsUniverseLeagueCount": len(stats_universe_codes()),
        "activeRegistryLeagueCount": len(leagues),
        "excludedDomesticApiCallsAllowed": False,
    }
    return result


def filter_registry_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _filter_registry_payload(payload, stats=False)


def filter_stats_registry_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _filter_registry_payload(payload, stats=True)


def priority_rank(item: Dict[str, Any] | str) -> int:
    """Stats priority: 0 Main5+Greece, 1 core-27, 2 restored Tier-3 leagues."""
    code = league_code(item) if isinstance(item, dict) else normalize_code(item)
    if code in absolute_priority_codes():
        return 0
    if code in included_codes():
        return 1
    if code in stats_universe_codes():
        return 2
    return 9


def priority_tier_name(item: Dict[str, Any] | str) -> str:
    rank = priority_rank(item)
    return {0: "tier1_main5_plus_greece", 1: "tier2_core27", 2: "tier3_restored26"}.get(rank, "outside_scope")


def _install_registry_load_guard(pipeline_module, *, stats: bool) -> None:
    original_load_json = pipeline_module.load_json
    marker = "_statmaker_stats_scope_guard" if stats else "_statmaker_core_scope_guard"
    if getattr(original_load_json, marker, False):
        return

    def guarded_load_json(path, default):
        payload = original_load_json(path, default)
        if path == pipeline_module.REGISTRY_PATH and isinstance(payload, dict):
            return filter_stats_registry_payload(payload) if stats else filter_registry_payload(payload)
        return payload

    setattr(guarded_load_json, marker, True)
    pipeline_module.load_json = guarded_load_json


def install_registry_load_guard(pipeline_module) -> None:
    """Backward-compatible core-odds registry read guard."""
    _install_registry_load_guard(pipeline_module, stats=False)


def install_stats_registry_load_guard(pipeline_module) -> None:
    _install_registry_load_guard(pipeline_module, stats=True)


def _install_registry_build_guard(pipeline_module, *, stats: bool) -> None:
    original_build = pipeline_module.build_live_registry
    marker = "_statmaker_stats_scope_guard" if stats else "_statmaker_core_scope_guard"
    if getattr(original_build, marker, False):
        return

    def guarded_build(*args, **kwargs):
        rows = original_build(*args, **kwargs)
        return filter_stats_leagues(rows) if stats else filter_leagues(rows)

    setattr(guarded_build, marker, True)
    pipeline_module.build_live_registry = guarded_build


def install_registry_build_guard(pipeline_module) -> None:
    """Backward-compatible core-odds registry build guard."""
    _install_registry_build_guard(pipeline_module, stats=False)


def install_stats_registry_build_guard(pipeline_module) -> None:
    _install_registry_build_guard(pipeline_module, stats=True)


def assert_final_scope_codes(codes: Sequence[str]) -> None:
    """Backward-compatible assertion for the protected core odds scope."""
    unexpected = {normalize_code(code) for code in codes} - included_codes()
    if unexpected:
        raise RuntimeError(f"Domestic core odds scope violation: {sorted(unexpected)}")


def assert_stats_scope_codes(codes: Sequence[str]) -> None:
    unexpected = {normalize_code(code) for code in codes} - stats_universe_codes()
    if unexpected:
        raise RuntimeError(f"Domestic stats scope violation: {sorted(unexpected)}")
