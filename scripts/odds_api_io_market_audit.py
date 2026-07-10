#!/usr/bin/env python3
"""Shared, side-effect-free Odds-API.io market coverage classification."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional


SUPPORTED_FAMILIES = {
    "1X2",
    "BTTS",
    "MATCH_GOALS",
    "FIRST_HALF_GOALS",
    "TEAM_TOTAL_GOALS",
    "MATCH_CORNERS",
    "TEAM_CORNERS",
    "MATCH_CARDS",
    "TEAM_CARDS",
    "MATCH_SHOTS",
    "TEAM_SHOTS",
    "MATCH_SHOTS_ON_TARGET",
    "TEAM_SHOTS_ON_TARGET",
    "DOUBLE_CHANCE",
}

AUDIT_ONLY_FAMILIES = {
    "DRAW_NO_BET",
    "ASIAN_HANDICAP",
    "ASIAN_TOTALS",
    "HALF_TIME_RESULT",
    "HALF_TIME_BTTS",
    "CORRECT_SCORE",
    "TEAM_GOALS_ALT_LINES",
    "PLAYER_PROPS",
}


def normalize_market_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def provider_market_text(market: Dict[str, Any]) -> str:
    return " ".join(
        str(market.get(key) or "") for key in ("name", "market", "type", "key")
    ).strip()


def _is_team_market(name: str) -> bool:
    return any(token in name for token in ("team", "home", "away"))


def classify_provider_market(raw_name: str) -> Dict[str, str]:
    name = normalize_market_text(raw_name)
    half_time = bool(re.search(r"\b(1h|ht)\b", name)) or any(
        token in name for token in ("first half", "1st half", "half time", "halftime")
    )

    if any(
        token in name
        for token in (
            "player",
            "goalscorer",
            "goal scorer",
            "anytime scorer",
            "first scorer",
            "last scorer",
            "player assists",
        )
    ):
        family = "PLAYER_PROPS"
    elif half_time and any(token in name for token in ("both teams", "btts", "both teams to score")):
        family = "HALF_TIME_BTTS"
    elif half_time and any(token in name for token in ("result", "winner", "moneyline", "1x2", "money line")):
        family = "HALF_TIME_RESULT"
    elif any(token in name for token in ("correct score", "exact score")):
        family = "CORRECT_SCORE"
    elif any(token in name for token in ("draw no bet", "drawn no bet")) or re.search(r"\bdnb\b", name):
        family = "DRAW_NO_BET"
    elif "double chance" in name or re.search(r"\b(1x|x2|12)\b", name):
        family = "DOUBLE_CHANCE"
    elif "asian total" in name:
        family = "ASIAN_TOTALS"
    elif any(token in name for token in ("asian handicap", "asian spread", "handicap", "spread")):
        family = "ASIAN_HANDICAP"
    elif _is_team_market(name) and any(token in name for token in ("alternative", "alternate", "alt line", "alt total")):
        family = "TEAM_GOALS_ALT_LINES"
    elif "shot on target" in name or "shots on target" in name or "on target" in name:
        family = "TEAM_SHOTS_ON_TARGET" if _is_team_market(name) else "MATCH_SHOTS_ON_TARGET"
    elif "shot" in name:
        family = "TEAM_SHOTS" if _is_team_market(name) else "MATCH_SHOTS"
    elif "corner" in name:
        family = "TEAM_CORNERS" if _is_team_market(name) else "MATCH_CORNERS"
    elif any(token in name for token in ("card", "booking", "yellow", "red card")):
        family = "TEAM_CARDS" if _is_team_market(name) else "MATCH_CARDS"
    elif any(token in name for token in ("both teams", "btts", "both teams to score")):
        family = "BTTS"
    elif half_time and any(token in name for token in ("goal", "total", "over under", "goal line")):
        family = "FIRST_HALF_GOALS"
    elif _is_team_market(name) and any(token in name for token in ("goal", "total", "over under")):
        family = "TEAM_TOTAL_GOALS"
    elif any(token in name for token in ("total", "over under", "goals", "goal line")):
        family = "MATCH_GOALS"
    elif name in {"ml", "money line", "moneyline"} or any(
        token in name for token in ("match result", "match winner", "1x2", "full time result")
    ):
        family = "1X2"
    else:
        family = "UNSUPPORTED"

    if family in SUPPORTED_FAMILIES:
        return {
            "family": family,
            "status": "supported",
            "reason": "recognized supported family; normal exact-emission rules still apply",
        }
    if family in AUDIT_ONLY_FAMILIES:
        return {
            "family": family,
            "status": "audit_only",
            "reason": "recognized audit-only family; not emitted to app markets",
        }
    return {
        "family": family,
        "status": "unsupported",
        "reason": "unrecognized or unsupported provider market",
    }


def ensure_audit_sections(debug: Dict[str, Any]) -> None:
    debug.setdefault("providerRawMarketNames", {})
    debug.setdefault("providerRawMarketClassifications", {})
    debug.setdefault("providerMarketFamilyCounts", {})
    debug.setdefault("supportedProviderMarketCounts", {})
    debug.setdefault("auditOnlyMarketCounts", {})
    debug.setdefault("auditOnlyMarketExamples", {})
    debug.setdefault("unsupportedMarketCounts", {})
    debug.setdefault("unsupportedMarketExamples", {})


def record_market_audit(
    debug: Dict[str, Any],
    raw_name: str,
    example: Optional[Dict[str, Any]] = None,
    *,
    classification_text: Optional[str] = None,
    example_limit: int = 5,
) -> Dict[str, str]:
    ensure_audit_sections(debug)
    classification = classify_provider_market(classification_text or raw_name)
    family = classification["family"]
    status = classification["status"]

    raw_counts = debug["providerRawMarketNames"]
    raw_counts[raw_name] = int(raw_counts.get(raw_name, 0)) + 1
    raw_classifications = debug["providerRawMarketClassifications"]
    raw_bucket = raw_classifications.setdefault(
        raw_name,
        {
            "family": family,
            "status": status,
            "reason": classification["reason"],
            "count": 0,
        },
    )
    raw_bucket["count"] = int(raw_bucket.get("count", 0)) + 1
    family_counts = debug["providerMarketFamilyCounts"]
    family_counts[family] = int(family_counts.get(family, 0)) + 1

    if status == "supported":
        target = debug["supportedProviderMarketCounts"]
        target[family] = int(target.get(family, 0)) + 1
    elif status == "audit_only":
        target = debug["auditOnlyMarketCounts"]
        target[family] = int(target.get(family, 0)) + 1
        _append_example(debug["auditOnlyMarketExamples"], family, raw_name, classification, example, example_limit)
    else:
        target = debug["unsupportedMarketCounts"]
        target[family] = int(target.get(family, 0)) + 1
        _append_example(debug["unsupportedMarketExamples"], family, raw_name, classification, example, example_limit)
    return classification


def _append_example(
    buckets: Dict[str, List[Dict[str, Any]]],
    family: str,
    raw_name: str,
    classification: Dict[str, str],
    example: Optional[Dict[str, Any]],
    limit: int,
) -> None:
    bucket = buckets.setdefault(family, [])
    if len(bucket) >= limit:
        return
    item: Dict[str, Any] = {
        "rawMarketName": raw_name,
        "family": family,
        "reason": classification["reason"],
    }
    if example:
        item.update(example)
    bucket.append(item)


def market_audit_report(debug: Dict[str, Any]) -> Dict[str, Any]:
    ensure_audit_sections(debug)
    return {
        "providerRawMarketNames": dict(sorted(debug["providerRawMarketNames"].items())),
        "providerRawMarketClassifications": dict(
            sorted(debug["providerRawMarketClassifications"].items())
        ),
        "providerMarketFamilyCounts": dict(sorted(debug["providerMarketFamilyCounts"].items())),
        "supportedProviderMarketCounts": dict(sorted(debug["supportedProviderMarketCounts"].items())),
        "auditOnlyMarketCounts": dict(sorted(debug["auditOnlyMarketCounts"].items())),
        "auditOnlyMarketExamples": debug["auditOnlyMarketExamples"],
        "unsupportedMarketCounts": dict(sorted(debug["unsupportedMarketCounts"].items())),
        "unsupportedMarketExamples": debug["unsupportedMarketExamples"],
    }


def run_market_audit_self_check() -> Dict[str, Any]:
    fixtures = [
        ({"name": "Match Result"}, "1X2", "supported"),
        ({"name": "Both Teams To Score"}, "BTTS", "supported"),
        ({"name": "Total Goals"}, "MATCH_GOALS", "supported"),
        ({"name": "1st Half Total Goals"}, "FIRST_HALF_GOALS", "supported"),
        ({"name": "Home Team Total Goals"}, "TEAM_TOTAL_GOALS", "supported"),
        ({"name": "Total Corners"}, "MATCH_CORNERS", "supported"),
        ({"name": "Away Team Corners"}, "TEAM_CORNERS", "supported"),
        ({"name": "Total Cards"}, "MATCH_CARDS", "supported"),
        ({"name": "Home Team Cards"}, "TEAM_CARDS", "supported"),
        ({"name": "Total Shots"}, "MATCH_SHOTS", "supported"),
        ({"name": "Away Team Shots"}, "TEAM_SHOTS", "supported"),
        ({"name": "Total Shots on Target"}, "MATCH_SHOTS_ON_TARGET", "supported"),
        ({"name": "Home Team Shots on Target"}, "TEAM_SHOTS_ON_TARGET", "supported"),
        ({"name": "Double Chance"}, "DOUBLE_CHANCE", "supported"),
        ({"name": "Draw No Bet"}, "DRAW_NO_BET", "audit_only"),
        ({"name": "Asian Handicap"}, "ASIAN_HANDICAP", "audit_only"),
        ({"name": "Asian Totals"}, "ASIAN_TOTALS", "audit_only"),
        ({"name": "1st Half Result"}, "HALF_TIME_RESULT", "audit_only"),
        ({"name": "1st Half Both Teams To Score"}, "HALF_TIME_BTTS", "audit_only"),
        ({"name": "Correct Score"}, "CORRECT_SCORE", "audit_only"),
        ({"name": "Home Team Alternative Goals"}, "TEAM_GOALS_ALT_LINES", "audit_only"),
        ({"name": "Player Shots on Target"}, "PLAYER_PROPS", "audit_only"),
        ({"name": "Provider Market", "key": "winning_margin"}, "UNSUPPORTED", "unsupported"),
    ]
    failures = []
    for market, expected_family, expected_status in fixtures:
        actual = classify_provider_market(provider_market_text(market))
        if actual["family"] != expected_family or actual["status"] != expected_status:
            failures.append(
                {
                    "market": market,
                    "expectedFamily": expected_family,
                    "expectedStatus": expected_status,
                    "actual": actual,
                }
            )
    if failures:
        raise AssertionError(f"market audit self-check failed: {failures}")
    return {
        "status": "passed",
        "fixturesChecked": len(fixtures),
        "supportedFamilies": sorted(SUPPORTED_FAMILIES),
        "auditOnlyFamilies": sorted(AUDIT_ONLY_FAMILIES),
    }
