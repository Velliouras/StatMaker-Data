#!/usr/bin/env python3
"""Betting V2 Phase 1: integrate already-published UEFA completed scores OFFLINE.

The separate UEFA artifacts contain real current-season fixture statistics and
team-specific historical score support. This overlay is diagnostic-only: the
lack of a provider fixture ID in team-support rows is explicitly recorded,
never fabricated. Competition and season are checked, duplicates are dropped,
conflicting outcomes rejected, no cross-competition results are relabeled.
No external API or bookmaker calls and no false qualification as STRONG.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from fixture_lookup import canonical_name

GROUP_NAMES = {
    "CL": "UEFA Champions League",
    "EL": "UEFA Europa League",
    "UECL": "UEFA Conference League",
}
STATS = ("HS", "AS", "HST", "AST", "HC", "AC", "HY", "AY", "HxG", "AxG")
SUPPORTED_SEASON = 2026


def competition_code(name: object) -> str | None:
    label = canonical_name(name)
    if label in {"uefa europa conference league", "uefa conference league"}:
        return "UECL"
    if label == "uefa europa league":
        return "EL"
    if label == "uefa champions league":
        return "CL"
    return None


def _score(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return int(number) if number.is_integer() and number >= 0 else None


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if loaded.get("schemaVersion") != 1:
        raise ValueError(f"Unknown UEFA source schema: {path}")
    return loaded


def import_uefa_history(root: Path, groups: dict, aliases: dict) -> tuple[list[dict], dict]:
    current = _read(root / "data/statmaker/uefa_current_stats.json")
    team_support = _read(root / "data/statmaker/uefa_team_support_history.json")
    counters = Counter()
    evidence = {"sourceFiles": ["data/statmaker/uefa_current_stats.json",
                                "data/statmaker/uefa_team_support_history.json"],
                "dataRole": "historical_support_only", "providerCalls": 0,
                "supplementalFixtureIdsSynthesized": 0}

    # Source-verified participant aliases apply only to their actual UEFA code.
    for participant in team_support.get("participants") or []:
        source_names = [participant.get("canonicalTeam"), participant.get("providerTeam")]
        source_names += list(participant.get("aliases") or [])
        codes = {competition_code(m.get("competition")) for m in participant.get("matches") or []}
        for code in codes - {None}:
            canonical = canonical_name(participant.get("canonicalTeam"))
            if not canonical:
                continue
            for label in source_names:
                if canonical_name(label):
                    aliases[code][canonical_name(label)].add(canonical)

    def norm(code: str, value: object) -> str:
        label = canonical_name(value)
        claims = aliases[code].get(label, set())
        return next(iter(claims)) if len(claims) == 1 else label

    # A separate source carries some verified current-season aliases as
    # canonical/display and provider names.
    for league in current.get("competitions") or []:
        code = {"CONF": "UECL"}.get(str(league.get("leagueCode") or ""),
                                   str(league.get("leagueCode") or ""))
        if code not in GROUP_NAMES:
            continue
        for fixture in league.get("fixtures") or []:
            for side in ("Home", "Away"):
                canonical = canonical_name(fixture.get("homeTeam" if side=="Home" else "awayTeam"))
                original = canonical_name(fixture.get("provider"+side+"Team"))
                if canonical and original:
                    aliases[code][original].add(canonical)

    entries: dict[tuple[str, str, str, str], dict] = {}
    blocked: set[tuple[str, str, str, str]] = set()

    def add(code: str, date: datetime, home: object, away: object, gh: object,
            ga: object, fixture_id: object, stats: dict, source: str) -> None:
        if code not in GROUP_NAMES:
            return
        home_token, away_token = norm(code, home), norm(code, away)
        score_home, score_away = _score(gh), _score(ga)
        if not home_token or not away_token or home_token == away_token or (
                score_home is None or score_away is None):
            counters["MISSING_REAL_UEFA_SCORE_OR_IDENTITY"] += 1
            return
        # Same team/day is not always sufficient for a unique match, therefore
        # include both teams and the explicit UEFA competition code.
        key = (code, date.date().isoformat(), home_token, away_token)
        if key in blocked:
            return
        previous = entries.get(key)
        if previous and (previous["hg"], previous["ag"]) != (score_home, score_away):
            entries.pop(key)
            blocked.add(key)
            counters["CONFLICTING_UEFA_SCORE_SOURCE"] += 1
            return
        if previous:
            counters["DUPLICATE_UEFA_SCORE_DROPPED"] += 1
            if source != "CURRENT_EXACT_FIXTURE":
                return
        group = code + "|" + GROUP_NAMES[code]
        groups.setdefault(group, {"league": GROUP_NAMES[code], "leagueCode": code,
                                  "country": "UEFA", "seasons": ["2026"],
                                  "historicalDataRole": "uefa_support"})
        if fixture_id is None:
            # Local immutable *diagnostic row key*, never a claimed API fixture ID.
            row_id = "uefa-support:" + hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:20]
        else:
            row_id = str(fixture_id)
        entries[key] = {
            "group": group, "fixture_id": row_id, "date": date,
            "home": home_token, "away": away_token,
            "hg": score_home, "ag": score_away,
            "stats": {field: stats.get(field) for field in STATS} if isinstance(stats, dict) else {},
            "phase1UEFASource": source,
            "apiFixtureIdentityVerified": source == "CURRENT_EXACT_FIXTURE",
        }
        counters["CURRENT_UEFA_FIXTURES" if source=="CURRENT_EXACT_FIXTURE" else "TEAM_SUPPORT_ROWS"] += 1

    if current:
        if current.get("season") != SUPPORTED_SEASON:
            raise ValueError("UEFA current history wrong season")
        for league in current.get("competitions") or []:
            code = {"CONF": "UECL"}.get(str(league.get("leagueCode") or ""),
                                       str(league.get("leagueCode") or ""))
            if code not in GROUP_NAMES:
                continue
            if str(league.get("season")) != str(SUPPORTED_SEASON):
                counters["WRONG_CURRENT_UEFA_SEASON"] += 1
                continue
            for match in league.get("fixtures") or []:
                if match.get("status") != "FT":
                    continue
                date = _datetime(match.get("kickoff"))
                if date is None:
                    counters["MISSING_UEFA_KICKOFF"] += 1
                    continue
                fixture_id = match.get("fixtureId")
                if not fixture_id:
                    counters["UNVERIFIED_CURRENT_UEFA_FIXTURE_ID"] += 1
                    continue
                add(code, date, match.get("homeTeam"), match.get("awayTeam"),
                    match.get("homeGoals"), match.get("awayGoals"), fixture_id,
                    match.get("normalizedStats") or {}, "CURRENT_EXACT_FIXTURE")

    if team_support:
        for participant in team_support.get("participants") or []:
            for match in participant.get("matches") or []:
                code = competition_code(match.get("competition"))
                season = str(match.get("season") or "")
                if code not in GROUP_NAMES or season != str(SUPPORTED_SEASON):
                    counters["NONMATCHING_UEFA_SUPPORT_SCOPE"] += 1
                    continue
                date_text = str(match.get("date") or "")
                if len(date_text) != 10:
                    counters["INVALID_UEFA_SUPPORT_DATE"] += 1
                    continue
                dt = _datetime(date_text)
                if not dt:
                    counters["INVALID_UEFA_SUPPORT_DATE"] += 1
                    continue
                add(code, dt, match.get("homeTeam"), match.get("awayTeam"),
                    match.get("homeGoals"), match.get("awayGoals"),
                    None, {}, "VERIFIED_TEAM_SUPPORT_SCORE")

    result = list(entries.values())
    result.sort(key=lambda r: (r["date"], r["group"], r["fixture_id"]))
    evidence["rowsByLeague"] = dict(Counter(r["group"].split("|",1)[0] for r in result))
    evidence["withMatchStats"] = sum(any(r["stats"].get(k) is not None for k in STATS) for r in result)
    evidence["counters"] = dict(counters)
    evidence["totalHistoricalUEFARows"] = len(result)
    return result, evidence
