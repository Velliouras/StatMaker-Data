#!/usr/bin/env python3
"""Host-only App-Ready hot publisher: install direct V2 forecasts in existing DB.

No emulator, no re-run of legacy simulations, no certified-model
precondition. The published immutable betting ZIP contains the same
existing data plus the newly fitted Betting V2 table.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data/statmaker/app_ready"
MANIFEST = BASE / "update_manifest.json"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def zip_exact(source: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=8) as archive:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            relative = path.relative_to(source).as_posix()
            item = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            archive.writestr(item, path.read_bytes(),
                             compress_type=zipfile.ZIP_DEFLATED, compresslevel=8)


def main():
    manifest = json.loads(MANIFEST.read_text())
    betting = next(x for x in manifest["artifacts"] if x["id"] == "app_ready_betting_bundle")
    original = ROOT / betting["path"]
    base_sha = betting["sha256"]
    if not original.is_file():
        raise SystemExit("Existing App-Ready betting bundle is unavailable")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        extracted = scratch / "bundle"
        extracted.mkdir()
        with zipfile.ZipFile(original) as z:
            z.extractall(extracted)
        db = extracted / "databases/statmaker_prepared_betting.db"
        internal = extracted / "bundle_manifest.json"
        if not db.is_file() or not internal.is_file():
            raise SystemExit("Existing ZIP lacks prepared betting DB or its manifest")
        exact_feeds = scratch / "uefa_exact_feeds"
        exact_feeds.mkdir()
        # Three normalized repository snapshots; zero provider requests.
        subprocess.run(["git","fetch","--no-tags","origin",
                        "build/uefa-qualifier-feed-20260720"],cwd=ROOT,check=True)
        quote_ref = subprocess.check_output(["git","rev-parse","FETCH_HEAD"],cwd=ROOT,text=True).strip()
        for competition in ("champions_league","europa_league","conference_league"):
            relative = f"odds/odds_api_io/{competition}_odds.json"
            payload = subprocess.check_output(["git","show",f"{quote_ref}:{relative}"],cwd=ROOT)
            json.loads(payload)
            (exact_feeds / (competition+"_odds.json")).write_bytes(payload)
        subprocess.run(["python",str(ROOT/"scripts/materialize_betting_v2_direct.py"),str(db),
                        "--uefa-exact-feed-dir",str(exact_feeds)],cwd=ROOT,check=True)
        import sqlite3
        with sqlite3.connect(db) as con:
            count = int(con.execute("SELECT count(*) FROM prepared_betting_v2_forecasts").fetchone()[0])
            fixtures = int(con.execute(
                "SELECT COUNT(DISTINCT competition_id || '|' || snapshot_version || '|' || match_key)"
                " FROM prepared_betting_v2_forecasts"
            ).fetchone()[0])
            quote_count = int(con.execute("SELECT count(*) FROM prepared_betting_v2_quotes").fetchone()[0])
            scopes = dict(con.execute(
                "SELECT competition_id,count(*) FROM prepared_betting_v2_forecasts GROUP BY competition_id"
            ).fetchall())
            eligible_markets = con.execute(
                "SELECT competition_id,market,count(*) FROM prepared_betting_v2_forecasts "
                "WHERE odd>1.8 AND odd<3 AND win_probability>=0.6 "
                "AND win_probability*odd+push_probability>1 "
                "GROUP BY competition_id,market ORDER BY competition_id,market"
            ).fetchall()
            print("BETTING_V2_FORECAST_SCOPES", json.dumps(scopes, sort_keys=True))
            print("BETTING_V2_RAW_QUALIFYING_MARKETS", json.dumps(eligible_markets))
            check = con.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise SystemExit("Prepared SQLite cannot be published: quick_check failed")
        listing = json.loads(internal.read_text())
        entry = next((x for x in listing.get("files", [])
                      if x.get("path") == "databases/statmaker_prepared_betting.db"), None)
        if entry is None:
            raise SystemExit("Prepared SQLite entry missing from internal ZIP manifest")
        entry["sha256"] = digest(db)
        entry["bytes"] = db.stat().st_size
        internal.write_text(json.dumps(listing, ensure_ascii=False, indent=2) + "\n")
        provisional = scratch / "bundle.zip"
        zip_exact(extracted, provisional)
        sha = digest(provisional)
        filename = f"app_ready_betting_bundle-{sha}.zip"
        output = BASE / filename
        shutil.copy2(provisional, output)
    betting.update(
        path=f"data/statmaker/app_ready/{filename}",
        url=f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/main/data/statmaker/app_ready/{filename}",
        sha256=sha, bytes=output.stat().st_size, generatedAt=now
    )
    metadata = manifest.setdefault("metadata", {})
    metadata["bettingV2DirectModel"] = "betting-v2-opponent-adjusted-count-v2"
    metadata["bettingV2DirectBaseSha256"] = base_sha
    metadata["bettingV2DirectUefaQuoteRef"] = quote_ref
    metadata["bettingV2DirectRows"] = count
    metadata["bettingV2DirectFixtures"] = fixtures
    metadata["bettingV2DirectQuoteCount"] = quote_count
    metadata["bettingV2DirectQuoteSource"] = "period-verified-quotes-with-bookmaker-and-source-hash-in-same-immutable-bundle"
    metadata["bettingV2DirectScopes"] = scopes
    metadata["bettingV2DirectSource"] = "deduplicated-competitive-club-graph-uefa-qualifying-bridges-opponent-adjusted-counts-lagged-elo-and-xg"
    metadata["bettingV2DirectCertificateRequired"] = False
    manifest["generatedAt"] = now
    manifest["contentVersion"] = hashlib.sha256(
        (str(manifest["contentVersion"]) + "|V2|" + sha + "|" + now).encode()
    ).hexdigest()
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print("BETTING_V2_HOST_PUBLISH_READY", "rows", count, "fixtures", fixtures, "bundle", filename)


if __name__ == "__main__":
    main()

