#!/usr/bin/env python3
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

import backfill_uefa_support_history as base
from build_uefa_support_history import normalize_team_key

PLACEHOLDER_RE = re.compile(r"^(winner|loser|tbd|to be determined|match\s+\d+)", re.IGNORECASE)
GENERIC_LOCATION_TOKENS = {
    "amsterdam", "athens", "belgrade", "białystok", "bialystok", "bratislava", "bucharest",
    "dublin", "istanbul", "jerusalem", "kiev", "kyiv", "limassol", "ljubljana", "madrid",
    "minsk", "nicosia", "novi", "sad", "piraeus", "praha", "prague", "razgrad", "reykjavik",
    "skopje", "sofia", "split", "streda", "tbilisi", "tel", "aviv", "thessaloniki", "vienna",
    "wien", "zagreb", "zhytomyr",
}

# Stable references must be captured before monkey-patching.
_original_feed_team_names = base.feed_team_names
_original_api_get = base.api.api_get


def is_real_participant_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return PLACEHOLDER_RE.match(text) is None


def feed_team_names_v2(feed_dir):
    result = _original_feed_team_names(feed_dir)
    return {
        competition: {name for name in names if is_real_participant_name(name)}
        for competition, names in result.items()
    }


def key_variants(value: Any) -> set[str]:
    key = normalize_team_key(value)
    if not key:
        return set()
    tokens = key.split()
    variants = {key}
    if len(tokens) > 1:
        if tokens[0] in GENERIC_LOCATION_TOKENS:
            variants.add(" ".join(tokens[1:]))
        if tokens[-1] in GENERIC_LOCATION_TOKENS:
            variants.add(" ".join(tokens[:-1]))
        # Provider feeds often prepend/append one location or legal-name token.
        # These variants are only accepted later when exactly one API candidate matches.
        variants.add(" ".join(tokens[:-1]))
        variants.add(" ".join(tokens[1:]))
    return {item for item in variants if item}


def choose_team_candidate_v2(name: str, rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    target_variants = key_variants(name)
    if not target_variants:
        return None

    exact_matches: dict[int, Mapping[str, Any]] = {}
    relaxed_matches: dict[int, Mapping[str, Any]] = {}
    target_key = normalize_team_key(name)

    for row in rows:
        team = row.get("team") if isinstance(row, dict) else None
        if not isinstance(team, dict):
            continue
        team_id = team.get("id")
        candidate_name = str(team.get("name") or "").strip()
        if team_id is None or not candidate_name:
            continue
        candidate_key = normalize_team_key(candidate_name)
        if candidate_key == target_key:
            exact_matches[int(team_id)] = row
            continue
        if target_variants.intersection(key_variants(candidate_name)):
            relaxed_matches[int(team_id)] = row

    if len(exact_matches) == 1:
        return next(iter(exact_matches.values()))
    if len(exact_matches) > 1:
        return None
    if len(relaxed_matches) == 1:
        return next(iter(relaxed_matches.values()))
    return None


def fallback_search_query(value: str) -> str:
    original = str(value or "").strip()
    normalized = normalize_team_key(original)
    if not normalized:
        return ""

    # Prefer the longest cleaned identity first: it removes FC/FK/PFC-style noise and
    # known provider-only location suffixes without collapsing to an unsafe single token.
    variants = sorted(key_variants(original), key=lambda item: (-len(item.split()), -len(item), item))
    for candidate in variants:
        if candidate.casefold() != original.casefold():
            return candidate
    return normalized if normalized.casefold() != original.casefold() else ""


def api_get_v2(api_key, endpoint, params, request_state, max_requests):
    payload = _original_api_get(api_key, endpoint, params, request_state, max_requests)
    if endpoint != "teams" or not isinstance(params, dict) or not params.get("search"):
        return payload

    requested_name = str(params.get("search") or "").strip()
    if choose_team_candidate_v2(requested_name, base.api.response_items(payload)) is not None:
        return payload

    fallback = fallback_search_query(requested_name)
    if not fallback:
        return payload

    # One controlled retry only. This keeps request use bounded while fixing provider labels
    # such as "Ajax Amsterdam", "Fenerbahce Istanbul", "FC Dila Gori", etc.
    return _original_api_get(
        api_key,
        endpoint,
        {**params, "search": fallback},
        request_state,
        max_requests,
    )


base.feed_team_names = feed_team_names_v2
base.choose_team_candidate = choose_team_candidate_v2
base.api.api_get = api_get_v2

if __name__ == "__main__":
    raise SystemExit(base.main())
