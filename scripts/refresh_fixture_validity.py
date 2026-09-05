#!/usr/bin/env python3
"""Quota-safe canonical fixture validity publisher.

Runs inside the existing live-settlement workflow. It never creates recommendations and never
calls the API from Android. It validates only canonical App-Ready recommendations, batches known
API-Football fixture ids (max 20/request), and uses at most one bounded league-season lookup for
past unresolved rows without a fixture id. Only explicit provider dispositions/date moves are
published; simple absence is never converted to VOID.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import api_football_daily_quota_guard as quota_guard
import api_football_fetch_fixture_stats as stats_fetch
import refresh_live_settlements as live

ROOT = Path(__file__).resolve().parents[1]
VALIDITY_PATH = ROOT / "data" / "statmaker" / "fixture_validity.json"
FEED_PATH = ROOT / "data" / "statmaker" / "live_settlements.json"
MAIN_MANIFEST_PATH = ROOT / "data" / "statmaker" / "update_manifest.json"
ATHENS = ZoneInfo("Europe/Athens")
RETENTION_DAYS = 30
LOOKAHEAD_DAYS = 14
MAX_IDS_PER_REQUEST = 20
EXPLICIT_VOID_STATUSES = {
    "PST": "POSTPONED",
    "CANC": "CANCELLED",
    "ABD": "ABANDONED",
    "AWD": "AWARDED",
    "WO": "WALKOVER",
}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(rendered, encoding="utf-8")
    temp.replace(path)


def semantic_disposition(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in (
        "competitionId", "matchKey", "localDate", "leagueCode", "homeTeam", "awayTeam",
        "disposition", "providerStatus", "providerFixtureId", "providerLocalDate",
    )}


def disposition_key(row: Dict[str, Any]) -> str:
    return "|".join(str(row.get(key) or "") for key in (
        "competitionId", "matchKey", "localDate", "leagueCode"
    ))


def requirement_key(row: live.SettlementRequirement) -> str:
    return "|".join((row.competition_id, row.match_key, row.local_date, row.league_code))


def provider_local_date(fixture: Dict[str, Any]) -> str:
    block = fixture.get("fixture") or {}
    raw = str(block.get("date") or "").strip()
    if not raw:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(ATHENS).date().isoformat()
    except ValueError:
        return raw[:10]


def fixture_teams(fixture: Dict[str, Any]) -> Tuple[str, str]:
    teams = fixture.get("teams") or {}
    return (
        str((teams.get("home") or {}).get("name") or "").strip(),
        str((teams.get("away") or {}).get("name") or "").strip(),
    )


def requirement_matches_fixture(req: live.SettlementRequirement, fixture: Dict[str, Any]) -> bool:
    fixture_id = stats_fetch.fixture_identity(fixture)
    if req.api_fixture_id is not None and fixture_id == req.api_fixture_id:
        return True
    home, away = fixture_teams(fixture)
    return live.team_matches(home, req.home_names) and live.team_matches(away, req.away_names)


def explicit_disposition(
    req: live.SettlementRequirement,
    fixture: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    status = stats_fetch.fixture_status_short(fixture).upper()
    provider_date = provider_local_date(fixture)
    disposition = EXPLICIT_VOID_STATUSES.get(status)
    if disposition is None and provider_date and provider_date != req.local_date:
        disposition = "RESCHEDULED"
    if disposition is None:
        return None
    home, away = fixture_teams(fixture)
    return {
        "competitionId": req.competition_id,
        "matchKey": req.match_key,
        "localDate": req.local_date,
        "leagueCode": req.league_code,
        "homeTeam": req.home_names[0],
        "awayTeam": req.away_names[0],
        "disposition": disposition,
        "providerStatus": status,
        "providerFixtureId": stats_fetch.fixture_identity(fixture),
        "providerLocalDate": provider_date,
        "providerHomeTeam": home,
        "providerAwayTeam": away,
        "sourceGenerationIds": [req.generation_id] if req.generation_id else [],
        "detectedAt": iso_now(),
    }


def completed_requirement_keys(
    requirements: Sequence[live.SettlementRequirement],
    feed: Dict[str, Any],
) -> set[str]:
    completed = []
    for item in feed.get("fixtures", []) if isinstance(feed, dict) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").upper() not in live.COMPLETED:
            continue
        completed.append(item)
    keys: set[str] = set()
    for req in requirements:
        for item in completed:
            if req.league_code and str(item.get("leagueCode") or "").strip().upper() not in {
                req.league_code, "CONF" if req.league_code == "UECL" else req.league_code,
                "UECL" if req.league_code == "CONF" else req.league_code,
            }:
                continue
            date_text = str(item.get("dateUtc") or "")[:10]
            distance = live._date_distance(req.local_date, date_text)
            if distance is None or distance > 1:
                continue
            if live.team_matches(str(item.get("homeTeam") or ""), req.home_names) and \
                    live.team_matches(str(item.get("awayTeam") or ""), req.away_names):
                keys.add(requirement_key(req))
                break
    return keys


def chunks(values: Sequence[int], size: int) -> List[List[int]]:
    return [list(values[index:index + size]) for index in range(0, len(values), size)]


def fetch_by_ids(
    api_key: str,
    ids: Sequence[int],
    request_state: Dict[str, int],
    max_requests: int,
) -> List[Dict[str, Any]]:
    if not ids or request_state["count"] >= max_requests:
        return []
    payload = stats_fetch.api_get(
        api_key,
        "fixtures",
        {"ids": "-".join(str(value) for value in ids)},
        request_state,
        max_requests,
    )
    return stats_fetch.response_items(payload)


def registry_scope_by_code() -> Dict[str, Tuple[int, str]]:
    result: Dict[str, Tuple[int, str]] = {}
    for row in live.registry_rows():
        code = str(row.get("leagueCode") or "").strip().upper()
        provider_id = live.as_int(row.get("api_football_league_id") or row.get("apiFootballLeagueId"))
        season = live.provider_season(row)
        if code and provider_id is not None and season:
            result[code] = (provider_id, season)
    for provider_id, row in live.UEFA_PROVIDER_ROWS.items():
        code = str(row.get("leagueCode") or "").strip().upper()
        if code:
            result.setdefault(code, (provider_id, ""))
    return result


def resolve_no_id_group(
    api_key: str,
    rows: Sequence[live.SettlementRequirement],
    provider_id: int,
    season: str,
    request_state: Dict[str, int],
    max_requests: int,
) -> List[Dict[str, Any]]:
    if not rows or not season or request_state["count"] >= max_requests:
        return []
    try:
        payload = stats_fetch.api_get(
            api_key,
            "fixtures",
            {"league": provider_id, "season": season},
            request_state,
            max_requests,
        )
    except stats_fetch.RequestLimitReached:
        return []
    fixtures = stats_fetch.response_items(payload)
    found: List[Dict[str, Any]] = []
    for req in rows:
        matching = [fixture for fixture in fixtures if requirement_matches_fixture(req, fixture)]
        if not matching:
            continue
        exact_date = [fixture for fixture in matching if provider_local_date(fixture) == req.local_date]
        for fixture in exact_date:
            row = explicit_disposition(req, fixture)
            if row is not None:
                found.append(row)
                break
        else:
            try:
                original = dt.date.fromisoformat(req.local_date)
            except ValueError:
                continue
            future = []
            for fixture in matching:
                text = provider_local_date(fixture)
                try:
                    day = dt.date.fromisoformat(text)
                except ValueError:
                    continue
                delta = (day - original).days
                if 1 <= delta <= 60:
                    future.append(fixture)
            if len(future) == 1:
                row = explicit_disposition(req, future[0])
                if row is not None and row["disposition"] == "RESCHEDULED":
                    found.append(row)
    return found


def merge_dispositions(existing_root: Dict[str, Any], detected: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    today = now_utc().date()
    cutoff = today - dt.timedelta(days=RETENTION_DAYS)
    merged: Dict[str, Dict[str, Any]] = {}
    for item in existing_root.get("dispositions", []) if isinstance(existing_root, dict) else []:
        if not isinstance(item, dict):
            continue
        try:
            day = dt.date.fromisoformat(str(item.get("localDate") or "")[:10])
        except ValueError:
            continue
        if day < cutoff:
            continue
        merged[disposition_key(item)] = dict(item)
    for item in detected:
        key = disposition_key(item)
        previous = merged.get(key)
        if previous and semantic_disposition(previous) == semantic_disposition(item):
            item = {**item, "detectedAt": previous.get("detectedAt") or item.get("detectedAt")}
        merged[key] = dict(item)
    return sorted(
        merged.values(),
        key=lambda item: (str(item.get("localDate") or ""), str(item.get("leagueCode") or ""), str(item.get("matchKey") or "")),
    )


def write_validity(dispositions: List[Dict[str, Any]]) -> bool:
    existing = load_json(VALIDITY_PATH, {})
    old_semantic = [semantic_disposition(row) for row in existing.get("dispositions", []) if isinstance(row, dict)] \
        if isinstance(existing, dict) else []
    new_semantic = [semantic_disposition(row) for row in dispositions]
    if old_semantic == new_semantic and VALIDITY_PATH.is_file():
        return False
    atomic_write(VALIDITY_PATH, {
        "schemaVersion": 1,
        "generatedAt": iso_now(),
        "source": "api-football-canonical-fixture-validity",
        "retentionDays": RETENTION_DAYS,
        "dispositions": dispositions,
    })
    return True


def ensure_feed_dispositions(dispositions: List[Dict[str, Any]]) -> bool:
    root = load_json(FEED_PATH, {})
    if not isinstance(root, dict):
        root = {}
    current = root.get("fixtureDispositions") if isinstance(root.get("fixtureDispositions"), list) else []
    if current == dispositions and int(root.get("schemaVersion") or 0) >= 3:
        return False
    root["schemaVersion"] = max(3, int(root.get("schemaVersion") or 0))
    root["fixtureDispositions"] = dispositions
    atomic_write(FEED_PATH, root)
    return True


def ensure_main_manifest_validity_artifact() -> bool:
    manifest = load_json(MAIN_MANIFEST_PATH, {})
    if not isinstance(manifest, dict) or int(manifest.get("schemaVersion") or 0) < 2:
        return False
    if not VALIDITY_PATH.is_file():
        return False
    artifacts = [dict(item) for item in manifest.get("artifacts", []) if isinstance(item, dict)]
    artifacts = [item for item in artifacts if item.get("id") != "fixture_validity"]
    raw = VALIDITY_PATH.read_bytes()
    validity = load_json(VALIDITY_PATH, {})
    artifacts.append({
        "id": "fixture_validity",
        "group": "fixture_validity",
        "path": "data/statmaker/fixture_validity.json",
        "url": "https://raw.githubusercontent.com/Velliouras/StatMaker-Data/main/data/statmaker/fixture_validity.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "generatedAt": str(validity.get("generatedAt") or ""),
    })
    artifacts.sort(key=lambda item: str(item.get("id") or ""))
    version_payload = json.dumps([
        {
            "id": item.get("id"),
            "sha256": item.get("sha256"),
            "bytes": item.get("bytes"),
            "generatedAt": item.get("generatedAt"),
        }
        for item in artifacts
    ], ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    updated = dict(manifest)
    updated["artifacts"] = artifacts
    updated["artifactCount"] = len(artifacts)
    updated["contentVersion"] = hashlib.sha256(version_payload).hexdigest()
    generated = sorted((str(item.get("generatedAt") or "") for item in artifacts if item.get("generatedAt")), reverse=True)
    updated["generatedAt"] = generated[0] if generated else str(manifest.get("generatedAt") or "")
    if updated == manifest:
        return False
    atomic_write(MAIN_MANIFEST_PATH, updated)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh canonical fixture validity with bounded API usage")
    parser.add_argument("--max-requests", type=int, default=4)
    args = parser.parse_args()
    max_requests = max(0, min(4, args.max_requests))

    requirements = live.canonical_requirements()
    feed = load_json(FEED_PATH, {})
    completed_keys = completed_requirement_keys(requirements, feed if isinstance(feed, dict) else {})
    today = now_utc().astimezone(ATHENS).date()
    low = today - dt.timedelta(days=RETENTION_DAYS)
    high = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    candidates = []
    for req in requirements:
        try:
            day = dt.date.fromisoformat(req.local_date)
        except ValueError:
            continue
        if day < low or day > high or requirement_key(req) in completed_keys:
            continue
        candidates.append(req)

    api_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    request_state = {"count": 0}
    detected: List[Dict[str, Any]] = []
    if max_requests > 0 and api_key:
        quota_guard.install(stats_fetch)
        no_id_past = [req for req in candidates if req.api_fixture_id is None and req.local_date < today.isoformat()]
        scopes = registry_scope_by_code()
        grouped: Dict[Tuple[int, str, str], List[live.SettlementRequirement]] = {}
        for req in no_id_past:
            scope = scopes.get(req.league_code)
            if scope is None or not scope[1]:
                continue
            grouped.setdefault((scope[0], scope[1], req.league_code), []).append(req)
        if grouped and request_state["count"] < max_requests:
            group = sorted(grouped.items(), key=lambda item: (-len(item[1]), min(r.local_date for r in item[1]), item[0][2]))[0]
            (provider_id, season, _), rows = group
            detected.extend(resolve_no_id_group(api_key, rows, provider_id, season, request_state, max_requests))

        by_id: Dict[int, List[live.SettlementRequirement]] = {}
        for req in candidates:
            if req.api_fixture_id is not None:
                by_id.setdefault(req.api_fixture_id, []).append(req)
        all_chunks = chunks(sorted(by_id), MAX_IDS_PER_REQUEST)
        remaining = max_requests - request_state["count"]
        if all_chunks and remaining > 0:
            slot = int(now_utc().timestamp() // 900) % len(all_chunks)
            chosen = [all_chunks[(slot + offset) % len(all_chunks)] for offset in range(min(remaining, len(all_chunks)))]
            for id_chunk in chosen:
                try:
                    fixtures = fetch_by_ids(api_key, id_chunk, request_state, max_requests)
                except stats_fetch.RequestLimitReached:
                    break
                except Exception as error:
                    print(f"fixture-validity ids fetch failed: {error}")
                    continue
                fixture_by_id = {
                    stats_fetch.fixture_identity(fixture): fixture
                    for fixture in fixtures
                    if stats_fetch.fixture_identity(fixture) is not None
                }
                for fixture_id in id_chunk:
                    fixture = fixture_by_id.get(fixture_id)
                    if fixture is None:
                        continue
                    for req in by_id.get(fixture_id, []):
                        row = explicit_disposition(req, fixture)
                        if row is not None:
                            detected.append(row)

    existing = load_json(VALIDITY_PATH, {})
    dispositions = merge_dispositions(existing if isinstance(existing, dict) else {}, detected)
    validity_changed = write_validity(dispositions)
    feed_changed = ensure_feed_dispositions(dispositions)
    manifest_changed = ensure_main_manifest_validity_artifact()
    print(
        "fixture-validity "
        f"canonical={len(requirements)} candidates={len(candidates)} detected={len(detected)} "
        f"dispositions={len(dispositions)} requests={request_state['count']} "
        f"validityChanged={validity_changed} feedChanged={feed_changed} manifestChanged={manifest_changed} "
        f"quota={json.dumps(quota_guard.status()) if api_key else '{}'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
