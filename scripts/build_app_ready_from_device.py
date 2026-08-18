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


def validate_generated_stats(domestic_fp):
    db_path = root / "databases" / "statmaker.db"
    if not db_path.is_file() or db_path.stat().st_size <= 0:
        raise SystemExit(f"Missing/empty generated stats DB: {db_path}")
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            match_count = int(connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0])
        finally:
            connection.close()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise SystemExit(f"Invalid generated stats DB {db_path}: {exc}") from exc
    if match_count <= 0:
        raise SystemExit("Refusing app-ready publish: generated stats DB contains 0 matches")

    parts = domestic_fp.split("|")
    try:
        fingerprint_count = int(parts[-1]) if len(parts) >= 3 else 0
    except ValueError as exc:
        raise SystemExit(f"Invalid domestic history fingerprint: {domestic_fp}") from exc
    if fingerprint_count <= 0 or domestic_fp == "0|0|0":
        raise SystemExit(f"Refusing app-ready publish: empty domestic history fingerprint {domestic_fp}")
    if fingerprint_count != match_count:
        raise SystemExit(
            f"Domestic fingerprint/DB mismatch: fingerprint={fingerprint_count}, db={match_count}"
        )


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


# The emulator is responsible only for the expensive generated read models and the two
# fingerprints that describe them. Canonical manifests/odds already live in StatMaker-Data;
# reading those from Android SharedPreferences adds no value and made the publisher dependent on
# incidental preference-file persistence. Use repository inputs directly and verify their hashes.
main_raw, main = read_json(source / "main_manifest.json", "main manifest")
uefa_raw, uefa = read_json(source / "uefa_manifest.json", "UEFA manifest")
if main.get("schemaVersion", 0) < 2 or uefa.get("schemaVersion", 0) < 2:
    raise SystemExit("Unsupported source manifest schema")

canonical_odds = {
    "domestic": (
        source / "domestic.json",
        artifact_by_id(main, "domestic_odds"),
    ),
    "champions_league": (
        source / "champions_league.json",
        artifact_by_id(uefa, "champions_league_odds"),
    ),
    "europa_league": (
        source / "europa_league.json",
        artifact_by_id(uefa, "europa_league_odds"),
    ),
    "conference_league": (
        source / "conference_league.json",
        artifact_by_id(uefa, "conference_league_odds"),
    ),
}
for name, (path, artifact) in canonical_odds.items():
    validate_source(path, artifact, f"{name} odds")

versions = prefs("statmaker_prepared_data_versions")
domestic_fp = versions.get("domestic_history_fingerprint", "").strip()
support_fp = versions.get("uefa_support_fingerprint", "").strip()
if not domestic_fp or not support_fp:
    raise SystemExit("Missing prepared fingerprints")
validate_generated_stats(domestic_fp)

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
        **{
            f"files/app_ready_odds/{name}.json": path
            for name, (path, _) in canonical_odds.items()
        },
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
