#!/usr/bin/env python3
"""Learn Domestic provider team aliases from exact cross-provider fixture identity.

This repair is intentionally conservative:
- Odds-API.io is never called.
- API-Football is queried at most once per unresolved league for the exact target
  league/season/date range already present in the provider archive.
- A provider name is learned only when league + date + kickoff (and any already
  resolved side) identify exactly one API-Football fixture.
- Ambiguous or conflicting names remain unresolved; no fuzzy-name guess is made.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import api_football_daily_quota_guard as quota_guard
import api_football_fetch_fixture_stats as stats_fetch
import domestic_live_july_pipeline as pipeline
import domestic_odds_expansion
import update_domestic_odds_api_io as odds

ROOT = Path(__file__).resolve().parents[1]
ALIASES_PATH = ROOT / "mappings" / "domestic_team_aliases.json"
ROSTER_PATH = ROOT / "data" / "statmaker" / "domestic_rosters.json"
ARCHIVE_PATH = ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"
REHYDRATE_REPORT_PATH = ROOT / "reports" / "domestic_archive_betting_rehydrate.json"
REPORT_PATH = ROOT / "reports" / "domestic_team_alias_schedule_repair.json"
DEFAULT_MAX_REQUESTS = 30


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_dt(value: Any) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def norm(value: Any) -> str:
    return odds.normalize_text(value, drop_suffixes=True)


def same_team(left: Any, right: Any) -> bool:
    return bool(left and right and norm(left) == norm(right))


_GENERIC_ANCHOR_TOKENS = {
    "club", "city", "united", "athletic", "sporting", "real", "racing",
    "deportivo", "football", "association", "team", "town", "county",
    "fc", "cf", "sc", "afc", "fk", "ac", "if", "sv", "sk", "tsg",
    "pfc", "wks", "kks", "acs", "asc", "cd", "ud", "sd", "ca",
    "de", "da", "do", "dos", "das", "the",
}


def meaningful_tokens(value: Any) -> set[str]:
    return {
        token
        for token in norm(value).split()
        if token not in _GENERIC_ANCHOR_TOKENS
        and len(token) >= 4
        and not token.isdigit()
    }


def roster_by_code(payload: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for row in payload.get("leagues", []) or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("leagueCode") or "").strip().upper()
        teams = [
            str(name).strip()
            for name in row.get("teams", []) or []
            if str(name).strip()
        ]
        if code and teams:
            out[code] = teams
    return out


def unique_roster_anchor(
    provider_name: Any,
    code: str,
    rosters: Dict[str, List[str]],
) -> Optional[str]:
    """Return one league-local roster candidate sharing a unique strong token.

    This is not accepted as a final alias on its own. It is only a constraint for
    the exact date/kickoff fixture identity check, which must still resolve uniquely.
    """
    tokens = meaningful_tokens(provider_name)
    if not tokens:
        return None
    candidates: List[str] = []
    for canonical in rosters.get(code, []):
        if tokens.intersection(meaningful_tokens(canonical)):
            candidates.append(canonical)
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


def roster_anchor_strength(provider_name: Any, canonical: Any) -> int:
    """Rank deterministic roster anchors without turning tokens into fuzzy aliases.

    2 = exact normalized provider/canonical name (accent/punctuation insensitive)
    1 = league-local unique strong-token anchor
    0 = unusable
    """
    if not provider_name or not canonical:
        return 0
    if norm(provider_name) == norm(canonical):
        return 2
    return 1 if meaningful_tokens(provider_name).intersection(
        meaningful_tokens(canonical)
    ) else 0


def archive_by_id(archive: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for league in archive.get("leagues", []) or []:
        if not isinstance(league, dict):
            continue
        code = str(league.get("leagueCode") or "").strip().upper()
        for match in league.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            match_id = str(match.get("id") or "").strip()
            if code and match_id:
                out[(code, match_id)] = match
    return out


def api_fixture_record(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fixture = item.get("fixture") or {}
    teams = item.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    kickoff = parse_dt(fixture.get("date"))
    home_name = str(home.get("name") or "").strip()
    away_name = str(away.get("name") or "").strip()
    if kickoff is None or not home_name or not away_name:
        return None
    return {
        "fixtureId": fixture.get("id"),
        "kickoff": kickoff,
        "date": kickoff.date().isoformat(),
        "homeTeam": home_name,
        "awayTeam": away_name,
        "homeTeamId": home.get("id"),
        "awayTeamId": away.get("id"),
    }


def unique_fixture_identity(
    archive_match: Dict[str, Any],
    unresolved: Dict[str, Any],
    fixtures: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    provider_kickoff = parse_dt(archive_match.get("kickoff"))
    date = str(archive_match.get("date") or archive_match.get("kickoff") or "")[:10]
    home_resolved = unresolved.get("homeResolved")
    away_resolved = unresolved.get("awayResolved")

    candidates: List[Tuple[float, Dict[str, Any]]] = []
    for fixture in fixtures:
        if str(fixture.get("date") or "") != date:
            continue
        if home_resolved and not same_team(home_resolved, fixture.get("homeTeam")):
            continue
        if away_resolved and not same_team(away_resolved, fixture.get("awayTeam")):
            continue

        kickoff = fixture.get("kickoff")
        delta_minutes = 10_000.0
        if provider_kickoff is not None and isinstance(kickoff, dt.datetime):
            delta_minutes = abs((provider_kickoff - kickoff).total_seconds()) / 60.0
            if delta_minutes > 180.0:
                continue
        candidates.append((delta_minutes, fixture))

    if home_resolved or away_resolved:
        return candidates[0][1] if len(candidates) == 1 else None

    # With neither side known, kickoff must uniquely identify the fixture.
    close = [item for item in candidates if item[0] <= 90.0]
    return close[0][1] if len(close) == 1 else None


def canonical_name(
    api_name: str,
    code: str,
    generated_aliases: Dict[str, Dict[str, str]],
) -> str:
    bucket = generated_aliases.get(code, {})
    for candidate in (
        odds.normalize_text(api_name, drop_suffixes=True),
        odds.simplified_team_name(api_name),
    ):
        if candidate and candidate in bucket:
            return str(bucket[candidate])
    return api_name


def alias_owner(raw_aliases: Dict[str, Any], code: str, provider_name: str) -> Optional[str]:
    wanted = norm(provider_name)
    for canonical, variants in ((raw_aliases.get("aliases") or {}).get(code, {}) or {}).items():
        names = [canonical, *(variants or [])]
        if any(norm(name) == wanted for name in names):
            return str(canonical)
    return None


def add_verified_alias(
    raw_aliases: Dict[str, Any],
    code: str,
    canonical: str,
    provider_name: str,
) -> Tuple[bool, Optional[str]]:
    provider_name = str(provider_name or "").strip()
    canonical = str(canonical or "").strip()
    if not provider_name or not canonical:
        return False, "missing_name"
    if norm(provider_name) == norm(canonical):
        return False, None

    owner = alias_owner(raw_aliases, code, provider_name)
    if owner and norm(owner) != norm(canonical):
        return False, f"conflict:{owner}"

    league = raw_aliases.setdefault("aliases", {}).setdefault(code, {})
    variants = league.setdefault(canonical, [])
    if provider_name in variants:
        return False, None
    variants.append(provider_name)
    variants.sort(key=str.casefold)
    return True, None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required.")
        return 2

    quota_guard.install(stats_fetch)
    registry_payload = load(pipeline.REGISTRY_PATH, {})
    registry = [
        row for row in registry_payload.get("leagues", []) or []
        if isinstance(row, dict)
    ]
    registry_by_code = {
        str(row.get("leagueCode") or "").strip().upper(): row
        for row in registry
    }
    unresolved_report = load(REHYDRATE_REPORT_PATH, {})
    unresolved_rows = [
        row for row in unresolved_report.get("unresolvedHistoricalTeams", []) or []
        if isinstance(row, dict)
    ]
    archive = load(ARCHIVE_PATH, {})
    archive_matches = archive_by_id(archive)
    raw_aliases = load(ALIASES_PATH, {"version": 1, "normalizationRules": {}, "aliases": {}})
    rosters = roster_by_code(load(ROSTER_PATH, {}))

    domestic_odds_expansion.install(odds, pipeline)
    generated_aliases = pipeline.generated_aliases(registry)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in unresolved_rows:
        code = str(row.get("leagueCode") or "").strip().upper()
        match_id = str(row.get("matchId") or "").strip()
        if code and match_id and (code, match_id) in archive_matches:
            grouped.setdefault(code, []).append(row)

    request_state = {"count": 0}
    max_requests = max(0, int(args.max_requests))
    learned: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    roster_anchors: List[Dict[str, Any]] = []
    league_reports: List[Dict[str, Any]] = []
    changed = False

    for code, rows in sorted(grouped.items()):
        if request_state["count"] >= max_requests:
            break
        meta = registry_by_code.get(code)
        if not meta:
            continue
        try:
            league_id = int(meta.get("apiFootballLeagueId") or meta.get("api_football_league_id"))
            season = int(meta.get("targetApiSeason") or meta.get("season"))
        except (TypeError, ValueError):
            continue

        matches = [
            archive_matches[(code, str(row.get("matchId") or ""))]
            for row in rows
            if (code, str(row.get("matchId") or "")) in archive_matches
        ]
        dates = [
            str(match.get("date") or match.get("kickoff") or "")[:10]
            for match in matches
            if str(match.get("date") or match.get("kickoff") or "")[:10]
        ]
        if not dates:
            continue

        try:
            payload = stats_fetch.api_get(
                api_key,
                "fixtures",
                {
                    "league": league_id,
                    "season": season,
                    "from": min(dates),
                    "to": max(dates),
                    "timezone": "UTC",
                },
                request_state,
                max_requests,
            )
        except stats_fetch.RequestLimitReached:
            break
        except Exception as exc:
            league_reports.append({
                "leagueCode": code,
                "status": "api_error",
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        api_items = stats_fetch.response_items(payload)
        fixtures = [
            record for item in api_items
            if (record := api_fixture_record(item)) is not None
        ]
        matched_count = 0
        for unresolved in rows:
            match_id = str(unresolved.get("matchId") or "")
            archive_match = archive_matches.get((code, match_id))
            if not archive_match:
                continue
            identity_row = dict(unresolved)
            pending_anchors: List[Tuple[str, str, str, int]] = []
            for side, resolved_key, provider_key in (
                ("home", "homeResolved", "providerHomeTeam"),
                ("away", "awayResolved", "providerAwayTeam"),
            ):
                if identity_row.get(resolved_key):
                    continue
                provider_name = str(archive_match.get(provider_key) or "")
                anchor = unique_roster_anchor(provider_name, code, rosters)
                if not anchor:
                    continue
                pending_anchors.append((
                    side,
                    provider_name,
                    anchor,
                    roster_anchor_strength(provider_name, anchor),
                ))

            # If one side is an exact normalized roster identity, never let a weaker
            # one-token anchor on the other side veto it (Young Violets Wien vs
            # Rapid Wien II was the concrete failure). Weak anchors remain usable
            # only when no stronger exact roster anchor exists.
            strongest = max((item[3] for item in pending_anchors), default=0)
            for side, provider_name, anchor, strength in pending_anchors:
                if strongest and strength < strongest:
                    continue
                resolved_key = "homeResolved" if side == "home" else "awayResolved"
                identity_row[resolved_key] = anchor
                roster_anchors.append({
                    "leagueCode": code,
                    "matchId": match_id,
                    "side": side,
                    "providerTeam": provider_name,
                    "anchorTeam": anchor,
                    "anchorStrength": "exact_normalized" if strength == 2 else "unique_token",
                })

            fixture = unique_fixture_identity(archive_match, identity_row, fixtures)
            if fixture is None:
                ambiguous.append({
                    "leagueCode": code,
                    "matchId": match_id,
                    "providerHomeTeam": archive_match.get("providerHomeTeam"),
                    "providerAwayTeam": archive_match.get("providerAwayTeam"),
                })
                continue

            home_canonical = canonical_name(
                str(fixture.get("homeTeam") or ""),
                code,
                generated_aliases,
            )
            away_canonical = canonical_name(
                str(fixture.get("awayTeam") or ""),
                code,
                generated_aliases,
            )
            learned_this_match = False
            for side, canonical, provider_name in (
                ("home", home_canonical, archive_match.get("providerHomeTeam")),
                ("away", away_canonical, archive_match.get("providerAwayTeam")),
            ):
                added, conflict = add_verified_alias(
                    raw_aliases,
                    code,
                    canonical,
                    str(provider_name or ""),
                )
                if conflict:
                    conflicts.append({
                        "leagueCode": code,
                        "matchId": match_id,
                        "side": side,
                        "providerTeam": provider_name,
                        "canonicalTeam": canonical,
                        "reason": conflict,
                    })
                elif added:
                    changed = True
                    learned_this_match = True
                    learned.append({
                        "leagueCode": code,
                        "matchId": match_id,
                        "side": side,
                        "providerTeam": provider_name,
                        "canonicalTeam": canonical,
                        "fixtureId": fixture.get("fixtureId"),
                        "policy": "unique league+date+kickoff API-Football fixture identity",
                    })
            if learned_this_match:
                matched_count += 1

        league_reports.append({
            "leagueCode": code,
            "apiFootballLeagueId": league_id,
            "season": season,
            "unresolvedMatches": len(rows),
            "apiFixturesReturned": len(fixtures),
            "matchesWithAliasesLearned": matched_count,
        })

    if changed:
        save(ALIASES_PATH, raw_aliases)

    report = {
        "generatedAt": pipeline.now_utc(),
        "source": "API-Football exact fixture identity + Odds-API.io archived provider names",
        "oddsApiCalls": 0,
        "apiFootballRequestsUsed": request_state["count"],
        "apiFootballMaxRequests": max_requests,
        "unresolvedInputCount": len(unresolved_rows),
        "unresolvedLeagueCount": len(grouped),
        "aliasesLearned": len(learned),
        "conflicts": len(conflicts),
        "ambiguousMatches": len(ambiguous),
        "rosterAnchorsUsed": len(roster_anchors),
        "learned": learned,
        "rosterAnchorDetails": roster_anchors,
        "conflictDetails": conflicts,
        "ambiguousDetails": ambiguous,
        "leagueReports": league_reports,
        "apiFootballQuotaGuard": quota_guard.status(),
    }
    save(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
