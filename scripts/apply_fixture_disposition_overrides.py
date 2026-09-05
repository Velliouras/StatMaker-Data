#!/usr/bin/env python3
"""Apply audited fixture-disposition overrides without any provider/API call.

The normal validity publisher remains API-Football-first. This layer exists only for an explicit,
audited competition-authority correction when the provider does not yet expose a usable fixture
link. Overrides are fail-closed: they require competition/match/date, an allowed disposition,
source name, source URL and verification timestamp. A provider-detected disposition always wins.
"""
from __future__ import annotations

import json
from pathlib import Path

import refresh_fixture_validity as validity

ROOT = Path(__file__).resolve().parents[1]
OVERRIDES_PATH = ROOT / "data" / "statmaker" / "fixture_disposition_overrides.json"
ALLOWED = {"POSTPONED", "CANCELLED", "RESCHEDULED", "ABANDONED", "AWARDED", "WALKOVER"}


def key(row: dict) -> str:
    return "|".join(str(row.get(name) or "") for name in (
        "competitionId", "matchKey", "localDate", "leagueCode"
    ))


def load_overrides() -> list[dict]:
    root = validity.load_json(OVERRIDES_PATH, {})
    rows = root.get("overrides", []) if isinstance(root, dict) else []
    accepted: list[dict] = []
    for value in rows or []:
        if not isinstance(value, dict):
            continue
        disposition = str(value.get("disposition") or "").strip().upper()
        required = (
            str(value.get("competitionId") or "").strip(),
            str(value.get("matchKey") or "").strip(),
            str(value.get("localDate") or "").strip()[:10],
            str(value.get("leagueCode") or "").strip(),
            str(value.get("source") or "").strip(),
            str(value.get("sourceUrl") or "").strip(),
            str(value.get("verifiedAt") or "").strip(),
        )
        if disposition not in ALLOWED or any(not item for item in required):
            raise SystemExit(f"Invalid fixture disposition override: {value}")
        accepted.append({**value, "disposition": disposition})
    return accepted


def main() -> int:
    existing = validity.load_json(validity.VALIDITY_PATH, {})
    if not isinstance(existing, dict):
        existing = {}
    current = [row for row in existing.get("dispositions", []) if isinstance(row, dict)]
    current_keys = {key(row) for row in current}

    additions: list[dict] = []
    for row in load_overrides():
        if key(row) in current_keys:
            continue
        additions.append({
            "competitionId": str(row.get("competitionId") or "").strip(),
            "matchKey": str(row.get("matchKey") or "").strip(),
            "localDate": str(row.get("localDate") or "").strip()[:10],
            "leagueCode": str(row.get("leagueCode") or "").strip().upper(),
            "homeTeam": str(row.get("homeTeam") or "").strip(),
            "awayTeam": str(row.get("awayTeam") or "").strip(),
            "disposition": str(row.get("disposition") or "").strip().upper(),
            "providerStatus": str(row.get("providerStatus") or "").strip(),
            "providerFixtureId": row.get("providerFixtureId"),
            "providerLocalDate": str(row.get("providerLocalDate") or "").strip(),
            "providerHomeTeam": str(row.get("providerHomeTeam") or row.get("homeTeam") or "").strip(),
            "providerAwayTeam": str(row.get("providerAwayTeam") or row.get("awayTeam") or "").strip(),
            "sourceGenerationIds": list(row.get("sourceGenerationIds") or []),
            "detectedAt": str(row.get("verifiedAt") or "").strip(),
            "evidenceSource": str(row.get("source") or "").strip(),
            "evidenceUrl": str(row.get("sourceUrl") or "").strip(),
        })

    if not additions:
        print("fixture-disposition-overrides applied=0")
        return 0

    merged = validity.merge_dispositions(existing, additions)
    validity_changed = validity.write_validity(merged)
    feed_changed = validity.ensure_feed_dispositions(merged)
    manifest_changed = validity.ensure_main_manifest_validity_artifact()
    print(
        "fixture-disposition-overrides "
        f"applied={len(additions)} dispositions={len(merged)} "
        f"validityChanged={validity_changed} feedChanged={feed_changed} "
        f"manifestChanged={manifest_changed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
