#!/usr/bin/env python3
"""Repair historical canonical recommendation identity from the authoritative final fixture feed.

This is deliberately repository-only and fail-closed. A historical recommendation is rewritten
only when exactly one completed fixture matches its local Athens date, compatible league and all
known home/away aliases. No provider/API request is made here.

The repair is global across Domestic and UEFA competitions. It updates exact fixture id, provider
fixture names and runtime match key while preserving the recommendation market/selection evidence.
Changed dates are marked backfilled so Android removes superseded local recommendation identities
before re-grading them.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from zoneinfo import ZoneInfo

import refresh_live_settlements as live

ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "data" / "statmaker" / "canonical_recommendation_ledger.json"
LIVE_PATH = ROOT / "data" / "statmaker" / "live_settlements.json"
ATHENS = ZoneInfo("Europe/Athens")
COMPLETED = {"FT", "AET", "PEN"}


def load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def compatible_league(left: Any, right: Any) -> bool:
    a = str(left or "").strip().upper()
    b = str(right or "").strip().upper()
    if not a or not b or a == b:
        return True
    return {a, b} <= {"CONF", "UECL"}


def athens_date(item: Dict[str, Any]) -> str:
    raw = str(item.get("dateUtc") or item.get("date") or "").strip()
    if not raw:
        return ""
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(ATHENS).date().isoformat()
    except ValueError:
        return raw[:10]


def names(row: Dict[str, Any], list_key: str, fallback_key: str) -> Tuple[str, ...]:
    raw = row.get(list_key)
    values: Iterable[Any]
    if isinstance(raw, list):
        values = [*raw, row.get(fallback_key)]
    else:
        values = [row.get(fallback_key)]
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = live.normalize_team(text)
        if text and key and key not in seen:
            out.append(text)
            seen.add(key)
    return tuple(out)


def final_rows(root: Dict[str, Any]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for item in root.get("fixtures", []) if isinstance(root, dict) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").strip().upper() not in COMPLETED:
            continue
        fixture_id = live.as_int(item.get("fixtureId"))
        home = str(item.get("homeTeam") or "").strip()
        away = str(item.get("awayTeam") or "").strip()
        date = athens_date(item)
        if fixture_id is None or not home or not away or not date:
            continue
        copy = dict(item)
        copy["_athensDate"] = date
        result.append(copy)
    return result


def matching_finals(row: Dict[str, Any], finals: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    date = str(row.get("localDate") or "").strip()[:10]
    league = str(row.get("leagueCode") or "").strip().upper()
    home_names = names(row, "homeNames", "homeTeam")
    away_names = names(row, "awayNames", "awayTeam")
    if not date or not home_names or not away_names:
        return []
    return [
        fixture for fixture in finals
        if fixture.get("_athensDate") == date
        and compatible_league(league, fixture.get("leagueCode"))
        and live.team_matches(str(fixture.get("homeTeam") or ""), home_names)
        and live.team_matches(str(fixture.get("awayTeam") or ""), away_names)
    ]


def source_date_from_match_key(row: Dict[str, Any]) -> str:
    key = str(row.get("matchKey") or "").strip()
    parts = key.split("|", 2)
    if len(parts) == 3 and len(parts[0]) >= 10:
        return parts[0][:10]
    return str(row.get("localDate") or "").strip()[:10]


def merge_aliases(primary: str, values: Sequence[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for value in (primary, *values):
        text = str(value or "").strip()
        key = live.normalize_team(text)
        if text and key and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def repair_row(row: Dict[str, Any], fixture: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    result = dict(row)
    old_home = str(row.get("homeTeam") or "").strip()
    old_away = str(row.get("awayTeam") or "").strip()
    new_home = str(fixture.get("homeTeam") or "").strip()
    new_away = str(fixture.get("awayTeam") or "").strip()
    fixture_id = live.as_int(fixture.get("fixtureId"))
    source_date = source_date_from_match_key(row)

    old_home_names = names(row, "homeNames", "homeTeam")
    old_away_names = names(row, "awayNames", "awayTeam")
    result["apiFixtureId"] = fixture_id
    result["homeTeam"] = new_home
    result["awayTeam"] = new_away
    result["homeNames"] = merge_aliases(new_home, old_home_names)
    result["awayNames"] = merge_aliases(new_away, old_away_names)
    result["matchKey"] = f"{source_date}|{new_home}|{new_away}"

    team = str(row.get("team") or "").strip()
    if team:
        if live.team_matches(team, old_home_names) or live.normalize_team(team) == live.normalize_team(old_home):
            result["team"] = new_home
        elif live.team_matches(team, old_away_names) or live.normalize_team(team) == live.normalize_team(old_away):
            result["team"] = new_away

    return result, result != row


def main() -> int:
    ledger = load(LEDGER_PATH, {})
    final_root = load(LIVE_PATH, {})
    if not isinstance(ledger, dict) or int(ledger.get("schemaVersion") or 0) < 4:
        raise SystemExit("CANONICAL_LEDGER_IDENTITY_RECONCILE_INVALID_LEDGER")
    finals = final_rows(final_root if isinstance(final_root, dict) else {})
    today = dt.datetime.now(dt.timezone.utc).astimezone(ATHENS).date().isoformat()

    changed_dates: set[str] = set()
    repaired = 0
    ambiguous = 0
    eligible_finished = 0
    output: List[Dict[str, Any]] = []
    for raw in ledger.get("entries", []) or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        local_date = str(row.get("localDate") or "").strip()[:10]
        if not local_date or local_date > today:
            output.append(row)
            continue
        candidates = matching_finals(row, finals)
        if len(candidates) == 1:
            eligible_finished += 1
            repaired_row, changed = repair_row(row, candidates[0])
            output.append(repaired_row)
            if changed:
                repaired += 1
                changed_dates.add(local_date)
        else:
            if len(candidates) > 1:
                ambiguous += 1
                print(
                    "CANONICAL_LEDGER_IDENTITY_AMBIGUOUS",
                    f"date={local_date}",
                    f"league={row.get('leagueCode')}",
                    f"match={row.get('homeTeam')} vs {row.get('awayTeam')}",
                    f"candidateIds={[item.get('fixtureId') for item in candidates]}",
                )
            output.append(row)

    dedup: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in output:
        key = (
            str(row.get("competitionId") or ""),
            str(row.get("localDate") or "")[:10],
            str(row.get("matchKey") or ""),
        )
        previous = dedup.get(key)
        if previous is None or int(row.get("generationBuiltAtMs") or 0) >= int(previous.get("generationBuiltAtMs") or 0):
            dedup[key] = row

    backfilled = {
        str(value)[:10]
        for value in ledger.get("backfilledDates", []) or []
        if str(value).strip()
    }
    backfilled.update(changed_dates)
    semantic = dict(ledger)
    semantic.pop("generatedAt", None)
    semantic["backfilledDates"] = sorted(backfilled)
    semantic["entries"] = sorted(
        dedup.values(),
        key=lambda row: (str(row.get("localDate") or ""), str(row.get("matchKey") or "")),
    )

    before = dict(ledger)
    before.pop("generatedAt", None)
    changed = semantic != before
    if changed:
        payload = {
            "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            **semantic,
        }
        temp = LEDGER_PATH.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(LEDGER_PATH)

    print(
        "canonical-ledger-identity-reconcile",
        f"finalFixtures={len(finals)}",
        f"finishedMatches={eligible_finished}",
        f"repaired={repaired}",
        f"ambiguous={ambiguous}",
        f"changedDates={','.join(sorted(changed_dates)) or '-'}",
        f"changed={changed}",
    )
    if ambiguous:
        raise SystemExit(f"CANONICAL_LEDGER_IDENTITY_RECONCILE_AMBIGUOUS count={ambiguous}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
