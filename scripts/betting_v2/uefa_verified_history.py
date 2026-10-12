"""Verified completed UEFA history for the real-input V2 evaluator.

This is NOT a calibration certificate. Dates from team-support records are
day-only; midnight UTC is used solely for strict preceding-day form batches,
never misrepresented as an actual fixture kickoff or an odds timestamp.
All missing statistics remain absent rather than invented.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
import json
from pathlib import Path

from fixture_lookup import canonical_name

CODES = {
    "champions_league": ("CL", "UEFA Champions League"),
    "europa_league": ("EL", "UEFA Europa League"),
    "conference_league": ("UECL", "UEFA Conference League"),
}


def competition_code(label: str) -> str | None:
    value = str(label).casefold()
    if "conference" in value:
        return "UECL"
    if "europa" in value:
        return "EL"
    if "champions" in value:
        return "CL"
    return None


def parse_score(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (ValueError, TypeError):
        return None
    return result if result >= 0 and str(value).strip() == str(result) else None


def source_date(value: str, day_only: bool) -> datetime | None:
    try:
        if day_only:
            day = datetime.strptime(str(value), "%Y-%m-%d").date()
            return datetime.combine(day, time.min, tzinfo=timezone.utc)
        instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return instant.astimezone(timezone.utc) if instant.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def verified_uefa_history(root: Path) -> tuple[list[dict], dict, dict]:
    """Return independent, actual FT results; current-stat records outrank support duplicates."""
    paths = [
        (root / "data/statmaker/uefa_current_stats.json", "current"),
        (root / "data/statmaker/uefa_team_support_history.json", "support"),
    ]
    included: dict[tuple[str, str, str, str], dict] = {}
    groups: dict[str, dict] = {}
    reasons: dict[str, int] = {}

    def skipped(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    def add(code: str | None, date: datetime | None, home: str, away: str,
            hg, ag, stats: dict, fixture_id: str, verified: bool) -> None:
        if code not in {"CL", "EL", "UECL"} or date is None or not verified:
            skipped("NOT_VERIFIED_UEFA_FT")
            return
        h, a = parse_score(hg), parse_score(ag)
        home, away = str(home).strip(), str(away).strip()
        if h is None or a is None or not home or not away or canonical_name(home) == canonical_name(away):
            skipped("INVALID_SCORE_OR_TEAMS")
            return
        competition = next(name for k, name in CODES.values() if k == code)
        group = f"{code}|{competition}"
        key = (group, date.date().isoformat(), canonical_name(home), canonical_name(away))
        if key in included:
            skipped("DUPLICATE_VERIFIED_UEFA_MATCH")
            return
        groups[group] = {"league": competition, "leagueCode": code, "country": "UEFA"}
        included[key] = {
            "group": group,
            # A date/team key for the historical backtest, NOT a provider fixture id.
            "fixture_id": fixture_id,
            "date": date,
            "home": home.casefold(), "away": away.casefold(),
            "hg": h, "ag": a,
            "stats": {k: v for k, v in (stats or {}).items()
                      if v is not None and not isinstance(v, bool)},
        }

    current = paths[0][0]
    if current.is_file():
        payload = json.loads(current.read_text(encoding="utf-8-sig"))
        if payload.get("schemaVersion") != 1:
            raise ValueError("Invalid current UEFA statistics schema")
        for comp in payload.get("competitions", []):
            code = CODES.get(str(comp.get("competitionId")), (None,))[0]
            for m in comp.get("fixtures", []):
                if m.get("status") != "FT":
                    skipped("NON_REGULATION_OR_UNFINISHED")
                    continue
                dt = source_date(m.get("kickoff", ""), False)
                fixture = str(m.get("fixtureId") or "")
                if not fixture:
                    skipped("MISSING_PROVIDER_FIXTURE_ID")
                    continue
                add(code, dt, m.get("homeTeam", ""), m.get("awayTeam", ""),
                    m.get("homeGoals"), m.get("awayGoals"),
                    m.get("normalizedStats", {}) if m.get("statsFetched") else {},
                    fixture, True)

    support = paths[1][0]
    if support.is_file():
        payload = json.loads(support.read_text(encoding="utf-8-sig"))
        if payload.get("schemaVersion") != 1:
            raise ValueError("Invalid verified UEFA support schema")
        for participant in payload.get("participants", []):
            for m in participant.get("matches", []):
                code = competition_code(m.get("competition", ""))
                if code is None:
                    continue  # never silently mix domestic or friendly games into UEFA
                source = str(m.get("sourceLabel") or "")
                if "verified" not in source.casefold() or "API-Football" not in source:
                    skipped("UNVERIFIED_SUPPORT_SOURCE")
                    continue
                dt = source_date(m.get("date", ""), True)
                home, away = m.get("homeTeam", ""), m.get("awayTeam", "")
                identifier = "|".join(("uefa-verified-support", code, str(m.get("date", "")),
                                       canonical_name(home), canonical_name(away)))
                add(code, dt, home, away, m.get("homeGoals"), m.get("awayGoals"),
                    {}, identifier, True)

    result = sorted(included.values(), key=lambda row: (row["date"], row["fixture_id"]))
    return result, reasons, groups
