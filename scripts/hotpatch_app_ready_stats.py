#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import struct
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
APP_READY = ROOT / "data/statmaker/app_ready"
APP_READY_MANIFEST = APP_READY / "update_manifest.json"
MAIN_MANIFEST = ROOT / "data/statmaker/update_manifest.json"
DOMESTIC_INDEX = ROOT / "data/statmaker/domestic_enriched/index.json"
NORMALIZED_STATS = ROOT / "data/api_football/domestic_normalized_fixture_stats.json"

TEAM_MATCHING_ALIASES = {
    "aek": "AEK Athens FC",
    "olympiakos": "Olympiakos Piraeus",
    "asteras_tripolis": "Asteras Tripolis",
    "volos_nfc": "Volos NFC",
    "panathinaikos": "Panathinaikos",
    "paok": "PAOK",
}
NORMALIZED_FIELDS = (
    "HxG", "AxG", "HSaves", "ASaves", "HPossession", "APossession",
    "HPasses", "APasses", "HPassesAccurate", "APassesAccurate",
    "HF", "AF", "HShotsOffGoal", "AShotsOffGoal",
    "HBlockedShots", "ABlockedShots", "HShotsInsideBox", "AShotsInsideBox",
    "HShotsOutsideBox", "AShotsOutsideBox", "HOffsides", "AOffsides",
    "HPassAccuracy", "APassAccuracy", "HGoalsPrevented", "AGoalsPrevented",
    "HFreeKicks", "AFreeKicks",
)


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"Expected object JSON: {path}")
    return payload


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", str(value).lower())
    no_marks = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", no_marks).strip("_")


def normalize_team_key(value: str) -> str:
    base = normalize_key(value)
    return normalize_key(TEAM_MATCHING_ALIASES.get(base, value))


def normalize_date_key(value: str) -> str:
    raw = str(value).strip().replace("-", "_").replace("/", "_")
    parts = [part for part in raw.split("_") if part]
    if len(parts) == 3:
        try:
            first, second, third = (int(part) for part in parts)
        except ValueError:
            pass
        else:
            if len(parts[0]) == 4:
                return f"{first:04d}_{second:02d}_{third:02d}"
            if len(parts[2]) == 4:
                return f"{third:04d}_{second:02d}_{first:02d}"
    return normalize_key(value)


def app_season_to_db_season(value: str) -> str:
    season = str(value or "").strip()
    aliases = {
        "2025-2026": "2526", "2025 - 2026": "2526", "25/26": "2526",
        "2024-2025": "2425", "2024 - 2025": "2425", "24/25": "2425",
        "2023-2024": "2324", "2023 - 2024": "2324", "23/24": "2324",
    }
    return aliases.get(season, season or "2526")


def int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def int_or_zero(value: Any) -> int:
    resolved = int_or_none(value)
    return 0 if resolved is None else resolved


def bool_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value if value is not None else "")


def create_stats_db(target: Path) -> tuple[int, dict[tuple[str, str], int]]:
    index = load_json(DOMESTIC_INDEX)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    con = sqlite3.connect(target)
    expected: dict[tuple[str, str], int] = {}
    inserted_total = 0
    try:
        con.executescript(
            """
            PRAGMA user_version=4;
            CREATE TABLE matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_key TEXT NOT NULL UNIQUE,
                season TEXT NOT NULL,
                division TEXT NOT NULL,
                date_text TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                fthg INTEGER NOT NULL DEFAULT 0,
                ftag INTEGER NOT NULL DEFAULT 0,
                hthg INTEGER NOT NULL DEFAULT 0,
                htag INTEGER NOT NULL DEFAULT 0,
                hc INTEGER NOT NULL DEFAULT 0,
                ac INTEGER NOT NULL DEFAULT 0,
                hy INTEGER NOT NULL DEFAULT 0,
                ay INTEGER NOT NULL DEFAULT 0,
                hr INTEGER NOT NULL DEFAULT 0,
                ar INTEGER NOT NULL DEFAULT 0,
                hs INTEGER NOT NULL DEFAULT 0,
                away_shots INTEGER NOT NULL DEFAULT 0,
                hst INTEGER NOT NULL DEFAULT 0,
                ast INTEGER NOT NULL DEFAULT 0,
                ht_available INTEGER NOT NULL DEFAULT 0,
                corners_available INTEGER NOT NULL DEFAULT 0,
                cards_available INTEGER NOT NULL DEFAULT 0,
                shots_available INTEGER NOT NULL DEFAULT 0,
                sot_available INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE domestic_artifact_state (
                artifact_key TEXT PRIMARY KEY,
                signature TEXT NOT NULL
            );
            CREATE INDEX idx_matches_league ON matches(season, division);
            CREATE INDEX idx_matches_home ON matches(home_team);
            CREATE INDEX idx_matches_away ON matches(away_team);
            CREATE INDEX idx_matches_league_date ON matches(season, division, date_text, id DESC);
            CREATE INDEX idx_matches_league_home ON matches(season, division, home_team, id DESC);
            CREATE INDEX idx_matches_league_away ON matches(season, division, away_team, id DESC);
            CREATE INDEX idx_matches_h2h ON matches(season, division, home_team, away_team, id DESC);
            """
        )

        for league in index.get("leagues", []) or []:
            if not isinstance(league, dict):
                continue
            completed = int(league.get("completed_fixtures") or 0)
            output_path = str(league.get("output_path") or "").strip()
            if completed <= 0 or not output_path:
                continue
            artifact_path = ROOT / output_path
            if not artifact_path.is_file():
                raise SystemExit(f"Missing Domestic artifact: {output_path}")
            artifact = load_json(artifact_path)
            season = app_season_to_db_season(
                str(league.get("app_season") or league.get("season") or "")
            )
            division = str(league.get("league_code") or "").strip()
            if not season or not division:
                raise SystemExit(f"Invalid Domestic scope in index: {league}")

            rows = 0
            for match in artifact.get("matches", []) or []:
                if not isinstance(match, dict):
                    continue
                date = str(match.get("date_utc") or match.get("date") or "")[:10]
                home = str(match.get("home_team") or match.get("homeTeam") or "").strip()
                away = str(match.get("away_team") or match.get("awayTeam") or "").strip()
                home_goals = int_or_none(
                    match.get("home_goals", match.get("home_score", match.get("fthg")))
                )
                away_goals = int_or_none(
                    match.get("away_goals", match.get("away_score", match.get("ftag")))
                )
                if not date or not home or not away or home_goals is None or away_goals is None:
                    continue

                score = match.get("score") if isinstance(match.get("score"), dict) else {}
                halftime = score.get("halftime") if isinstance(score.get("halftime"), dict) else {}
                hthg = int_or_none(match.get("hthg"))
                htag = int_or_none(match.get("htag"))
                if hthg is None:
                    hthg = int_or_none(halftime.get("home"))
                if htag is None:
                    htag = int_or_none(halftime.get("away"))

                stats = match.get("normalized_stats")
                if not isinstance(stats, dict):
                    stats = {}
                non_null = {key for key, value in stats.items() if value is not None}
                quality = match.get("quality") if isinstance(match.get("quality"), dict) else {}
                groups = quality.get("groups") if isinstance(quality.get("groups"), dict) else {}

                def available(group: str, *required: str) -> int:
                    if groups.get(group) is False:
                        return 0
                    return 1 if all(key in non_null for key in required) else 0

                match_key = "|".join(
                    [season, division, date, home, away]
                ).lower().strip()
                values = (
                    match_key, season, division, date, home, away,
                    home_goals, away_goals, hthg or 0, htag or 0,
                    int_or_zero(stats.get("HC")), int_or_zero(stats.get("AC")),
                    int_or_zero(stats.get("HY")), int_or_zero(stats.get("AY")),
                    int_or_zero(stats.get("HR")), int_or_zero(stats.get("AR")),
                    int_or_zero(stats.get("HS")), int_or_zero(stats.get("AS")),
                    int_or_zero(stats.get("HST")), int_or_zero(stats.get("AST")),
                    1 if hthg is not None and htag is not None else 0,
                    available("corners", "HC", "AC"),
                    available("yellow_cards", "HY", "AY"),
                    available("shots_total", "HS", "AS"),
                    available("shots_on_target", "HST", "AST"),
                )
                cur = con.execute(
                    """
                    INSERT OR IGNORE INTO matches(
                        match_key,season,division,date_text,home_team,away_team,
                        fthg,ftag,hthg,htag,hc,ac,hy,ay,hr,ar,hs,away_shots,hst,ast,
                        ht_available,corners_available,cards_available,shots_available,sot_available
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    values,
                )
                if cur.rowcount == 1:
                    rows += 1
                    inserted_total += 1

            expected[(season, division)] = completed
            if rows != completed:
                raise SystemExit(
                    f"Stats hot-publish row mismatch {division}@{season}: "
                    f"artifact={completed} inserted={rows}"
                )

            signature = "|".join(
                [
                    division,
                    str(league.get("app_season") or ""),
                    output_path,
                    str(completed),
                    str(int(league.get("fixtures_with_score") or completed)),
                    bool_text(league.get("bb_ready_candidate")),
                ]
            )
            con.execute(
                "INSERT OR REPLACE INTO domestic_artifact_state(artifact_key,signature) VALUES(?,?)",
                (output_path, signature),
            )

        con.commit()

        actual = {
            (str(season), str(division)): int(count)
            for season, division, count in con.execute(
                "SELECT season,division,COUNT(*) FROM matches GROUP BY season,division"
            )
        }
        if actual != expected:
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            mismatch = sorted(
                (key, expected[key], actual.get(key))
                for key in expected
                if actual.get(key) != expected[key]
            )
            raise SystemExit(
                f"Stats hot-publish DB scope mismatch missing={missing[:10]} "
                f"extra={extra[:10]} mismatch={mismatch[:10]}"
            )
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise SystemExit(f"Stats hot-publish DB quick_check failed: {quick}")
    finally:
        con.close()

    return inserted_total, expected


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().removesuffix("%").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def stored_keys(fixture: dict[str, Any]) -> list[str]:
    result: list[str] = []
    league_id = str(fixture.get("league_id") or "").strip()
    fixture_id = str(fixture.get("fixture_id") or "").strip()
    if league_id and fixture_id:
        result.append(f"api_{league_id}_{fixture_id}".lower())

    league_code = str(fixture.get("league_code") or "").strip()
    date = str(fixture.get("date") or "")[:10]
    home = str(fixture.get("home_team") or "").strip()
    away = str(fixture.get("away_team") or "").strip()
    if league_code and date and home and away:
        result.append(
            "|".join(
                [
                    normalize_key(league_code),
                    normalize_date_key(date),
                    normalize_team_key(home),
                    normalize_team_key(away),
                ]
            )
        )
    if date and home and away:
        result.append(
            "|".join(
                [normalize_date_key(date), normalize_team_key(home), normalize_team_key(away)]
            )
        )
    return result


def write_utf(handle, value: str) -> None:
    # All persisted StatMaker normalized keys are ASCII after normalize_key().
    data = value.encode("utf-8")
    if len(data) > 65535:
        raise SystemExit("Normalized stats key is too long for DataOutputStream.writeUTF")
    handle.write(struct.pack(">H", len(data)))
    handle.write(data)


def create_normalized_snapshot(target: Path) -> int:
    payload = load_json(NORMALIZED_STATS)
    values: dict[str, tuple[float | None, ...]] = {}
    for fixture in payload.get("fixtures", []) or []:
        if not isinstance(fixture, dict):
            continue
        stats = fixture.get("stats")
        if not isinstance(stats, dict):
            continue
        row = tuple(parse_number(stats.get(field)) for field in NORMALIZED_FIELDS)
        if not any(value is not None for value in row):
            continue
        for key in stored_keys(fixture):
            values[key] = row

    if not values:
        raise SystemExit("Normalized Domestic stats snapshot would be empty")

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        handle.write(struct.pack(">i", 0x534D4453))
        handle.write(struct.pack(">i", 2))
        handle.write(struct.pack(">i", len(values)))
        for key, row in values.items():
            write_utf(handle, key)
            for number in row:
                handle.write(b"\x01" if number is not None else b"\x00")
                if number is not None:
                    handle.write(struct.pack(">d", number))
    return len(values)


def deterministic_zip(source: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            rel = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(
                info,
                path.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )


def rebuild_bundle_manifest(root: Path) -> None:
    bundle_manifest = root / "bundle_manifest.json"
    rows = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p != bundle_manifest):
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    bundle_manifest.write_text(
        json.dumps(
            {"schemaVersion": 1, "bundleType": "stats", "files": rows},
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def main() -> int:
    manifest = load_json(APP_READY_MANIFEST)
    artifacts = manifest.get("artifacts") or []
    stats = next((item for item in artifacts if item.get("id") == "app_ready_stats_bundle"), None)
    betting = next((item for item in artifacts if item.get("id") == "app_ready_betting_bundle"), None)
    if not stats or not betting:
        raise SystemExit("Current App-Ready manifest must contain stats and betting bundles")

    old_stats_bundle = ROOT / str(stats.get("path") or "")
    if not old_stats_bundle.is_file():
        raise SystemExit(f"Current stats bundle missing: {old_stats_bundle}")

    main_raw = MAIN_MANIFEST.read_text(encoding="utf-8")
    main_manifest = json.loads(main_raw)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        extracted = temp / "stats"
        extracted.mkdir()
        with zipfile.ZipFile(old_stats_bundle) as archive:
            archive.extractall(extracted)

        db_path = extracted / "databases/statmaker.db"
        normalized_path = extracted / "files/domestic_normalized_stats_v2.bin"
        required_uefa = [
            extracted / "files/statmaker_stats_snapshots/champions_league.bin",
            extracted / "files/statmaker_stats_snapshots/europa_league.bin",
            extracted / "files/statmaker_stats_snapshots/conference_league.bin",
        ]
        missing_uefa = [str(path) for path in required_uefa if not path.is_file()]
        if missing_uefa:
            raise SystemExit(f"Current stats bundle is missing UEFA snapshots: {missing_uefa}")

        match_count, scopes = create_stats_db(db_path)
        normalized_keys = create_normalized_snapshot(normalized_path)
        rebuild_bundle_manifest(extracted)

        provisional = temp / "stats.zip"
        deterministic_zip(extracted, provisional)
        new_sha = sha256(provisional)
        new_name = f"app_ready_stats_bundle-{new_sha}.zip"
        new_bundle = APP_READY / new_name
        shutil.copy2(provisional, new_bundle)

    stats["path"] = f"data/statmaker/app_ready/{new_name}"
    stats["url"] = (
        "https://raw.githubusercontent.com/Velliouras/StatMaker-Data/main/"
        + stats["path"]
    )
    stats["sha256"] = new_sha
    stats["bytes"] = new_bundle.stat().st_size
    stats["generatedAt"] = now

    metadata = manifest.setdefault("metadata", {})
    metadata["mainContentVersion"] = str(main_manifest.get("contentVersion") or "")
    metadata["mainManifestRaw"] = main_raw
    metadata["statsHotPublish"] = True
    metadata["statsHotPublishMatchCount"] = match_count
    metadata["statsHotPublishScopeCount"] = len(scopes)
    metadata["statsHotPublishNormalizedKeyCount"] = normalized_keys

    seed = "\n".join(
        sorted(
            [
                f"main|{metadata.get('mainContentVersion', '')}",
                f"uefa|{metadata.get('uefaContentVersion', '')}",
                f"stats|{new_sha}",
                f"betting|{betting.get('sha256', '')}",
                f"domestic|{metadata.get('domesticHistoryFingerprint', '')}",
                f"support|{metadata.get('uefaSupportFingerprint', '')}",
            ]
        )
    )
    manifest["contentVersion"] = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    manifest["generatedAt"] = now
    manifest["artifactCount"] = 2
    APP_READY_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        "APP_READY_STATS_HOT_PUBLISH_OK",
        f"matches={match_count}",
        f"scopes={len(scopes)}",
        f"normalized_keys={normalized_keys}",
        f"bundle={new_name}",
        f"sha256={new_sha}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
