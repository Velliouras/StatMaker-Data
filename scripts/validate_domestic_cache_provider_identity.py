#!/usr/bin/env python3
"""Fail closed when an app-facing Domestic cache contains another provider competition."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "data" / "statmaker" / "domestic_enriched" / "index.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    index = load_json(INDEX_PATH)
    rows = index.get("leagues", []) if isinstance(index, dict) else []
    if not rows:
        raise SystemExit("Domestic enriched index is empty")

    errors: list[str] = []
    checked_caches = 0
    checked_fixtures = 0

    for row in rows:
        if not isinstance(row, dict):
            errors.append("invalid non-object Domestic enriched index row")
            continue
        code = str(row.get("league_code") or "?").strip().upper()
        season = str(row.get("app_season") or row.get("api_football_season") or "?")
        expected = as_int(row.get("api_football_league_id"))
        cache_rel = str(row.get("cache_path") or "").strip()
        if expected is None or not cache_rel:
            errors.append(f"{code}@{season}: missing provider id/cache path")
            continue

        cache_path = ROOT / cache_rel
        if not cache_path.is_file():
            errors.append(f"{code}@{season}: missing cache {cache_rel}")
            continue
        cache = load_json(cache_path)
        checked_caches += 1

        actual = as_int(cache.get("league_id") if isinstance(cache, dict) else None)
        if actual is not None and actual != expected:
            errors.append(
                f"{code}@{season}: cache league_id={actual} but index expects {expected} ({cache_rel})"
            )

        fixtures = cache.get("fixtures", []) if isinstance(cache, dict) else []
        if not isinstance(fixtures, list):
            errors.append(f"{code}@{season}: cache fixtures is not a list")
            continue

        bad_sources: list[str] = []
        for fixture in fixtures:
            if not isinstance(fixture, dict):
                continue
            checked_fixtures += 1
            source = fixture.get("source_league")
            if not isinstance(source, dict):
                continue
            source_id = as_int(source.get("id"))
            if source_id is not None and source_id != expected:
                bad_sources.append(
                    f"{fixture.get('fixture_id')}:{source_id}:{source.get('name') or '?'}"
                )
                if len(bad_sources) >= 8:
                    break
        if bad_sources:
            errors.append(
                f"{code}@{season}: fixture source league mismatch expected={expected} "
                + "samples=" + ",".join(bad_sources)
            )

    if errors:
        raise SystemExit(
            "DOMESTIC_PROVIDER_IDENTITY_INVALID\n - " + "\n - ".join(errors)
        )

    print(
        "DOMESTIC_PROVIDER_IDENTITY_OK",
        f"caches={checked_caches}",
        f"fixtures={checked_fixtures}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
