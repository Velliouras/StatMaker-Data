#!/usr/bin/env python3
"""Authoritative final Domestic scope and runtime guards for StatMaker-Data."""
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


def included_codes() -> set[str]:
    payload = _load_scope()
    return {normalize_code(code) for code in payload.get("includedLeagueCodes", []) or []}


def absolute_priority_codes() -> set[str]:
    payload = _load_scope()
    return {normalize_code(code) for code in payload.get("absoluteStatsPriorityLeagueCodes", []) or []}


def league_code(item: Dict[str, Any]) -> str:
    return normalize_code(
        item.get("leagueCode")
        or item.get("league_code")
        or item.get("football_data_code")
        or item.get("code")
    )


def is_included(item_or_code: Dict[str, Any] | str) -> bool:
    code = league_code(item_or_code) if isinstance(item_or_code, dict) else normalize_code(item_or_code)
    return bool(code) and code in included_codes()


def filter_leagues(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in rows if isinstance(row, dict) and is_included(row)]


def filter_registry_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(payload or {})
    leagues = filter_leagues(result.get("leagues", []) or [])
    result["leagues"] = leagues
    if "leagueCount" in result:
        result["leagueCount"] = len(leagues)
    result["finalDomesticScope"] = {
        "authoritative": True,
        "includedLeagueCount": len(included_codes()),
        "activeRegistryLeagueCount": len(leagues),
        "excludedDomesticApiCallsAllowed": False,
    }
    return result


def priority_rank(item: Dict[str, Any]) -> int:
    return 0 if league_code(item) in absolute_priority_codes() else 1


def install_registry_load_guard(pipeline_module) -> None:
    """Filter every read of the live Domestic registry through the final scope."""
    original_load_json = pipeline_module.load_json
    if getattr(original_load_json, "_statmaker_scope_guard", False):
        return

    def guarded_load_json(path, default):
        payload = original_load_json(path, default)
        if path == pipeline_module.REGISTRY_PATH and isinstance(payload, dict):
            return filter_registry_payload(payload)
        return payload

    guarded_load_json._statmaker_scope_guard = True
    pipeline_module.load_json = guarded_load_json


def install_registry_build_guard(pipeline_module) -> None:
    """Ensure registry generation can publish only final-scope Domestic leagues."""
    original_build = pipeline_module.build_live_registry
    if getattr(original_build, "_statmaker_scope_guard", False):
        return

    def guarded_build(*args, **kwargs):
        return filter_leagues(original_build(*args, **kwargs))

    guarded_build._statmaker_scope_guard = True
    pipeline_module.build_live_registry = guarded_build


def assert_final_scope_codes(codes: Sequence[str]) -> None:
    unexpected = {normalize_code(code) for code in codes} - included_codes()
    if unexpected:
        raise RuntimeError(f"Domestic scope violation: {sorted(unexpected)}")
