#!/usr/bin/env python3
import hashlib
import json
import shutil
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "app-ready-export/raw")
out = Path(sys.argv[2] if len(sys.argv) > 2 else "app-ready-export/out")
source = Path(sys.argv[3] if len(sys.argv) > 3 else "app-ready-export/source")
out.mkdir(parents=True, exist_ok=True)


def prefs(name):
    path = root / "shared_prefs" / f"{name}.xml"
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(f"Missing/empty preferences {path}")
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise SystemExit(f"Invalid preferences XML {path}: {exc}") from exc
    values = {}
    for node in tree.getroot():
        key = node.attrib.get("name")
        if key and node.tag == "string":
            values[key] = node.text or ""
    return values


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path, label):
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(f"Missing/empty {label}: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid {label}: {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"Invalid {label} root: {path}")
    return raw, parsed


def artifact_by_id(manifest, artifact_id):
    for artifact in manifest.get("artifacts", []):
        if artifact.get("id") == artifact_id:
            return artifact
    raise SystemExit(f"Manifest missing artifact {artifact_id}")


def validate_source(path, artifact, label):
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(f"Missing/empty canonical {label}: {path}")
    expected_bytes = int(artifact.get("bytes", 0) or 0)
    expected_sha = str(artifact.get("sha256", "")).strip()
    if expected_bytes > 0 and path.stat().st_size != expected_bytes:
        raise SystemExit(f"Canonical {label} size mismatch")
    if expected_sha and sha(path) != expected_sha:
        raise SystemExit(f"Canonical {label} SHA256 mismatch")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid canonical {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Invalid canonical {label} root")
    return payload


def exact_market_count(payload, label):
    matches = payload.get("matches", []) or []
    if not isinstance(matches, list):
        raise SystemExit(f"Invalid canonical {label} matches")
    total = 0
    for match in matches:
        if not isinstance(match, dict):
            raise SystemExit(f"Invalid canonical {label} match row")
        markets = match.get("markets", []) or []
        if not isinstance(markets, list):
            raise SystemExit(f"Invalid canonical {label} markets")
        for market in markets:
            if not isinstance(market, dict):
                raise SystemExit(f"Invalid canonical {label} market row")
            if market.get("exactBookmakerOdds") is True and str(market.get("bookmaker") or "").strip():
                total += 1
    return total


def validate_fingerprint(value, label):
    value = value.strip()
    if not value or value in {"0", "0|0|0"}:
        raise SystemExit(f"Refusing app-ready publish: empty {label} fingerprint {value!r}")
    return value


def normalize_league_code(value):
    code = str(value or "").strip().upper()
    return "ROU" if code == "ROM" else code


def app_season_to_db_season(value):
    season = str(value or "").strip()
    aliases = {
        "2025-2026": "2526",
        "2025 - 2026": "2526",
        "25/26": "2526",
        "2024-2025": "2425",
        "2024 - 2025": "2425",
        "24/25": "2425",
        "2023-2024": "2324",
        "2023 - 2024": "2324",
        "23/24": "2324",
    }
    return aliases.get(season, season or "2526")


def validate_generated_stats(domestic_index):
    db_path = root / "databases" / "statmaker.db"
    if not db_path.is_file() or db_path.stat().st_size <= 0:
        raise SystemExit(f"Missing/empty generated stats DB: {db_path}")

    leagues = domestic_index.get("leagues", []) or []
    if not isinstance(leagues, list) or not leagues:
        raise SystemExit("Invalid/empty canonical Domestic enriched index")

    expected = {}
    for row in leagues:
        if not isinstance(row, dict):
            raise SystemExit("Invalid Domestic enriched index league row")
        completed = int(row.get("completed_fixtures", 0) or 0)
        if completed <= 0:
            continue
        code = normalize_league_code(
            row.get("league_code") or row.get("leagueCode") or row.get("code")
        )
        season = app_season_to_db_season(
            row.get("app_season") or row.get("appSeason") or row.get("season")
        )
        if not code or not season:
            raise SystemExit(f"Invalid expected Domestic stats scope: code={code!r} season={season!r}")
        key = (season, code)
        if key in expected:
            raise SystemExit(f"Duplicate expected Domestic stats scope: {code} {season}")
        expected[key] = completed

    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            match_count = int(connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0])
            actual_rows = connection.execute(
                "SELECT season, division, COUNT(*) FROM matches GROUP BY season, division"
            ).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise SystemExit(f"Invalid generated stats DB {db_path}: {exc}") from exc

    if match_count <= 0:
        raise SystemExit("Refusing app-ready publish: generated stats DB contains 0 matches")

    actual = {
        (str(season), normalize_league_code(division)): int(count)
        for season, division, count in actual_rows
    }
    missing = sorted(key for key in expected if key not in actual)
    unexpected = sorted(key for key in actual if key not in expected)
    mismatched = sorted(
        (key, expected[key], actual.get(key))
        for key in expected
        if key in actual and actual[key] != expected[key]
    )
    if missing or unexpected or mismatched:
        details = []
        if missing:
            details.append("missing=" + ",".join(f"{code}@{season}" for season, code in missing[:20]))
        if unexpected:
            details.append("unexpected=" + ",".join(f"{code}@{season}" for season, code in unexpected[:20]))
        if mismatched:
            details.append(
                "count_mismatch=" + ",".join(
                    f"{code}@{season}:{expected_count}!={actual_count}"
                    for (season, code), expected_count, actual_count in mismatched[:20]
                )
            )
        raise SystemExit(
            "Refusing app-ready publish: generated stats DB does not match canonical "
            "league+season scopes: " + " ".join(details)
        )

    expected_total = sum(expected.values())
    if match_count != expected_total:
        raise SystemExit(
            "Refusing app-ready publish: generated stats DB total does not match canonical "
            f"completed fixtures: db={match_count} expected={expected_total}"
        )

    print(
        "APP_READY_STATS_SCOPE_VALIDATION_OK",
        f"scopes={len(expected)}",
        f"matches={match_count}",
    )
    return match_count

def validate_generated_betting(source_exact_markets):
    db_path = root / "databases" / "statmaker_prepared_betting.db"
    if not db_path.is_file() or db_path.stat().st_size <= 0:
        raise SystemExit(f"Missing/empty generated prepared betting DB: {db_path}")
    uefa = {"champions_league", "europa_league", "conference_league"}
    # Production clients require all four competition metadata rows even when a UEFA
    # competition has zero exact markets. Empty 0/0 READY snapshots are valid; absence is not.
    required = {"domestic", "champions_league", "europa_league", "conference_league"}
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT competition_id, state, match_count, selection_count FROM prepared_snapshot_meta"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise SystemExit(f"Invalid generated prepared betting DB {db_path}: {exc}") from exc

    ready = {
        str(competition_id): (int(match_count), int(selection_count))
        for competition_id, state, match_count, selection_count in rows
        if state == "ready"
    }
    missing = sorted(required - ready.keys())
    if missing:
        raise SystemExit(
            "Refusing app-ready publish: prepared betting DB missing READY snapshots: "
            + ", ".join(missing)
        )

    domestic_counts = ready["domestic"]
    if domestic_counts[0] <= 0 or domestic_counts[1] <= 0:
        raise SystemExit(
            "Refusing app-ready publish: empty Domestic prepared betting snapshot: "
            f"matches={domestic_counts[0]}, selections={domestic_counts[1]}"
        )

    invalid_uefa = {}
    valid_empty_uefa = []
    for competition_id in sorted(uefa):
        exact_markets = int(source_exact_markets.get(competition_id, 0) or 0)
        counts = ready.get(competition_id, (0, 0))
        if exact_markets > 0 and (counts[0] <= 0 or counts[1] <= 0):
            invalid_uefa[competition_id] = (counts, exact_markets)
        elif exact_markets == 0 and counts[1] <= 0:
            valid_empty_uefa.append(competition_id)

    if invalid_uefa:
        detail = ", ".join(
            f"{competition_id}(source_exact_markets={exact_markets}, "
            f"matches={counts[0]}, selections={counts[1]})"
            for competition_id, (counts, exact_markets) in sorted(invalid_uefa.items())
        )
        raise SystemExit(
            "Refusing app-ready publish: UEFA source has exact bookmaker markets "
            f"but prepared snapshot is empty: {detail}"
        )

    if valid_empty_uefa:
        print(
            "APP_READY_VALID_EMPTY_UEFA",
            ",".join(valid_empty_uefa),
            "source_exact_markets=0",
        )

    return {competition_id: ready[competition_id] for competition_id in sorted(required)}


def bundle(artifact_id, kind, sources):
    work = out / f".{kind}"
    shutil.rmtree(work, ignore_errors=True)
    content = work / "content"
    content.mkdir(parents=True)
    rows = []
    for rel, src in sorted(sources.items()):
        src = Path(src)
        if not src.is_file() or src.stat().st_size <= 0:
            raise SystemExit(f"Missing {src}")
        dst = content / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        rows.append({"path": rel, "sha256": sha(dst), "bytes": dst.stat().st_size})
    (content / "bundle_manifest.json").write_text(
        json.dumps(
            {"schemaVersion": 1, "bundleType": kind, "files": rows},
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    tmp = out / f"{artifact_id}.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(p for p in content.rglob("*") if p.is_file()):
            zi = zipfile.ZipInfo(
                f.relative_to(content).as_posix(),
                (1980, 1, 1, 0, 0, 0),
            )
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(
                zi,
                f.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    digest = sha(tmp)
    final = out / f"{artifact_id}-{digest}.zip"
    tmp.replace(final)
    return {
        "id": artifact_id,
        "group": "read_model" if kind == "stats" else "prepared",
        "path": f"data/statmaker/app_ready/{final.name}",
        "url": f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/main/data/statmaker/app_ready/{final.name}",
        "sha256": digest,
        "bytes": final.stat().st_size,
    }


main_raw, main = read_json(source / "main_manifest.json", "main manifest")
uefa_raw, uefa = read_json(source / "uefa_manifest.json", "UEFA manifest")
if main.get("schemaVersion", 0) < 2 or uefa.get("schemaVersion", 0) < 2:
    raise SystemExit("Unsupported source manifest schema")

domestic_index = validate_source(
    source / "domestic_enriched_index.json",
    artifact_by_id(main, "domestic_enriched_index"),
    "Domestic enriched index",
)

canonical_odds = {
    "domestic": (source / "domestic.json", artifact_by_id(main, "domestic_odds")),
    "champions_league": (source / "champions_league.json", artifact_by_id(uefa, "champions_league_odds")),
    "europa_league": (source / "europa_league.json", artifact_by_id(uefa, "europa_league_odds")),
    "conference_league": (source / "conference_league.json", artifact_by_id(uefa, "conference_league_odds")),
}
canonical_payloads = {}
for name, (path, artifact) in canonical_odds.items():
    canonical_payloads[name] = validate_source(path, artifact, f"{name} odds")

source_exact_markets = {
    competition_id: exact_market_count(canonical_payloads[competition_id], f"{competition_id} odds")
    for competition_id in ("champions_league", "europa_league", "conference_league")
}

versions = prefs("statmaker_prepared_data_versions")
domestic_fp = validate_fingerprint(versions.get("domestic_history_fingerprint", ""), "domestic history")
support_fp = validate_fingerprint(versions.get("uefa_support_fingerprint", ""), "UEFA support")
stats_match_count = validate_generated_stats(domestic_index)
prepared_counts = validate_generated_betting(source_exact_markets)
print(
    "APP_READY_VALIDATION_OK",
    f"stats_matches={stats_match_count}",
    "source_exact_markets=" + ",".join(
        f"{competition_id}:{count}"
        for competition_id, count in sorted(source_exact_markets.items())
    ),
    "prepared=" + ",".join(
        f"{competition_id}:{counts[0]}/{counts[1]}"
        for competition_id, counts in prepared_counts.items()
    ),
)

stats = bundle(
    "app_ready_stats_bundle",
    "stats",
    {
        "databases/statmaker.db": root / "databases/statmaker.db",
        "files/domestic_normalized_stats_v2.bin": root / "files/domestic_normalized_stats_v2.bin",
        "files/statmaker_stats_snapshots/champions_league.bin": root / "files/statmaker_stats_snapshots/champions_league.bin",
        "files/statmaker_stats_snapshots/europa_league.bin": root / "files/statmaker_stats_snapshots/europa_league.bin",
        "files/statmaker_stats_snapshots/conference_league.bin": root / "files/statmaker_stats_snapshots/conference_league.bin",
    },
)
betting = bundle(
    "app_ready_betting_bundle",
    "betting",
    {
        "databases/statmaker_prepared_betting.db": root / "databases/statmaker_prepared_betting.db",
        **{f"files/app_ready_odds/{name}.json": path for name, (path, _) in canonical_odds.items()},
    },
)

now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
for item in (stats, betting):
    item["generatedAt"] = now
seed = "\n".join(
    sorted(
        [
            f"main|{main.get('contentVersion', '')}",
            f"uefa|{uefa.get('contentVersion', '')}",
            f"stats|{stats['sha256']}",
            f"betting|{betting['sha256']}",
            f"domestic|{domestic_fp}",
            f"support|{support_fp}",
        ]
    )
)
manifest = {
    "schemaVersion": 1,
    "profile": "app_ready",
    "contentVersion": hashlib.sha256(seed.encode()).hexdigest(),
    "generatedAt": now,
    "artifactCount": 2,
    "artifacts": [stats, betting],
    "metadata": {
        "domesticHistoryFingerprint": domestic_fp,
        "uefaSupportFingerprint": support_fp,
        "mainContentVersion": main.get("contentVersion", ""),
        "uefaContentVersion": uefa.get("contentVersion", ""),
        "mainManifestRaw": main_raw,
        "uefaManifestRaw": uefa_raw,
    },
}
(out / "update_manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print("APP_READY_EXPORT_OK", manifest["contentVersion"])
