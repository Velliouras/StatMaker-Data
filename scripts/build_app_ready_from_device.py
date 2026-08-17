#!/usr/bin/env python3
import hashlib, json, shutil, sys, xml.etree.ElementTree as ET, zipfile
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "app-ready-export/raw")
out = Path(sys.argv[2] if len(sys.argv) > 2 else "app-ready-export/out")
out.mkdir(parents=True, exist_ok=True)

def prefs(name):
    values = {}
    for node in ET.parse(root / "shared_prefs" / f"{name}.xml").getroot():
        key = node.attrib.get("name")
        if key and node.tag == "string": values[key] = node.text or ""
    return values

def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()

def bundle(artifact_id, kind, sources):
    work = out / f".{kind}"
    shutil.rmtree(work, ignore_errors=True)
    content = work / "content"; content.mkdir(parents=True)
    rows = []
    for rel, src in sorted(sources.items()):
        src = Path(src)
        if not src.is_file() or src.stat().st_size <= 0: raise SystemExit(f"Missing {src}")
        dst = content / rel; dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dst)
        rows.append({"path": rel, "sha256": sha(dst), "bytes": dst.stat().st_size})
    (content / "bundle_manifest.json").write_text(json.dumps({"schemaVersion":1,"bundleType":kind,"files":rows}, sort_keys=True, separators=(",",":")), encoding="utf-8")
    tmp = out / f"{artifact_id}.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(p for p in content.rglob("*") if p.is_file()):
            zi = zipfile.ZipInfo(f.relative_to(content).as_posix(), (1980,1,1,0,0,0)); zi.compress_type = zipfile.ZIP_DEFLATED; zi.external_attr = 0o644 << 16
            z.writestr(zi, f.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    digest = sha(tmp); final = out / f"{artifact_id}-{digest}.zip"; tmp.replace(final)
    return {"id":artifact_id,"group":"read_model" if kind=="stats" else "prepared","path":f"data/statmaker/app_ready/{final.name}","url":f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/main/data/statmaker/app_ready/{final.name}","sha256":digest,"bytes":final.stat().st_size}

odds = prefs("statmaker_odds_feeds"); manifests = prefs("statmaker_data_manifests"); versions = prefs("statmaker_prepared_data_versions")
main_raw = manifests.get("main_manifest_raw", ""); uefa_raw = manifests.get("uefa_manifest_raw", "")
if not main_raw or not uefa_raw: raise SystemExit("Missing generated source manifests")
main, uefa = json.loads(main_raw), json.loads(uefa_raw)
domestic_fp = versions.get("domestic_history_fingerprint", ""); support_fp = versions.get("uefa_support_fingerprint", "")
if not domestic_fp or not support_fp: raise SystemExit("Missing prepared fingerprints")

odds_dir = out / ".odds"; shutil.rmtree(odds_dir, ignore_errors=True); odds_dir.mkdir()
keys = {"domestic":"domestic_odds_api_io_json","champions_league":"champions_league_odds_api_io_json","europa_league":"europa_league_odds_api_io_json","conference_league":"conference_league_odds_api_io_json"}
for name, key in keys.items():
    payload = odds.get(key, "")
    if not payload or not isinstance(json.loads(payload), dict): raise SystemExit(f"Missing/invalid odds {key}")
    (odds_dir / f"{name}.json").write_text(payload, encoding="utf-8")

stats = bundle("app_ready_stats_bundle", "stats", {
    "databases/statmaker.db": root/"databases/statmaker.db",
    "files/domestic_normalized_stats_v2.bin": root/"files/domestic_normalized_stats_v2.bin",
    "files/statmaker_stats_snapshots/champions_league.bin": root/"files/statmaker_stats_snapshots/champions_league.bin",
    "files/statmaker_stats_snapshots/europa_league.bin": root/"files/statmaker_stats_snapshots/europa_league.bin",
    "files/statmaker_stats_snapshots/conference_league.bin": root/"files/statmaker_stats_snapshots/conference_league.bin",
})
betting = bundle("app_ready_betting_bundle", "betting", {
    "databases/statmaker_prepared_betting.db": root/"databases/statmaker_prepared_betting.db",
    **{f"files/app_ready_odds/{n}.json": odds_dir/f"{n}.json" for n in keys},
})
now = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
for item in (stats, betting): item["generatedAt"] = now
seed = "\n".join(sorted([f"main|{main.get('contentVersion','')}",f"uefa|{uefa.get('contentVersion','')}",f"stats|{stats['sha256']}",f"betting|{betting['sha256']}",f"domestic|{domestic_fp}",f"support|{support_fp}"]))
manifest = {"schemaVersion":1,"profile":"app_ready","contentVersion":hashlib.sha256(seed.encode()).hexdigest(),"generatedAt":now,"artifactCount":2,"artifacts":[stats,betting],"metadata":{"domesticHistoryFingerprint":domestic_fp,"uefaSupportFingerprint":support_fp,"mainContentVersion":main.get("contentVersion",""),"uefaContentVersion":uefa.get("contentVersion",""),"mainManifestRaw":main_raw,"uefaManifestRaw":uefa_raw}}
(out/"update_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print("APP_READY_EXPORT_OK", manifest["contentVersion"])
