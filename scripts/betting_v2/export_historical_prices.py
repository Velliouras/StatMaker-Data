#!/usr/bin/env python3
"""Export historical LEGACY-PREPARED selections from archived App-Ready DBs.

IMPORTANT: These rows are NOT an independently verified unfiltered bookmaker
price universe and their snapshot cutoff is NOT the time each bookmaker
quote was actually observed. They cannot certify ROI/EV/STRONG.

This research tool is read-only. It requires a full local StatMaker-Data git
history and a working git executable. It does not run any GitHub Actions.

Example:
 python research/betting_v2/export_historical_prices.py \
   --data-root ../StatMaker-Data --from-date 2026-10-01 \
   --to-date 2026-10-07 --output /tmp/v2-prices.jsonl

Snapshot rule: most recent manifest commit ON/BEFORE 11:00 Europe/Athens.
Never substitute a newer snapshot when none exists. Historical price != live quote.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from zoneinfo import ZoneInfo
import zipfile

from fixture_lookup import CachedFixtureLookup

ATHENS = ZoneInfo("Europe/Athens")
MANIFEST = "data/statmaker/app_ready/update_manifest.json"
MAX_BUNDLE_AGE = timedelta(hours=24)


def verified_archive_timestamp(artifact: dict, cutoff: datetime) -> datetime | None:
    """Reject unknown, future-dated or stale ZIP generations. Not a quote timestamp."""
    raw = artifact.get("generatedAt")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        generated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if generated.tzinfo is None:
        return None
    age = cutoff.astimezone(timezone.utc) - generated.astimezone(timezone.utc)
    return generated if timedelta(0) <= age <= MAX_BUNDLE_AGE else None


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def snapshot_for(root: Path, day: date) -> tuple[str, dict] | None:
    cutoff = datetime.combine(day, time(11, 0), ATHENS)
    sha = git(root, "log", "-1", "--format=%H",
              "--until=" + cutoff.isoformat(), "--", MANIFEST).decode().strip()
    if not sha:
        return None
    manifest = json.loads(git(root, "show", f"{sha}:{MANIFEST}"))
    if manifest.get("profile") != "app_ready":
        raise ValueError("Unexpected manifest profile")
    generated = manifest.get("generatedAt")
    if generated:
        generated_at = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
        if generated_at.tzinfo is None or generated_at > cutoff.astimezone(timezone.utc):
            raise ValueError("Manifest was generated after as-of pricing cutoff")
    return sha, manifest


def datetime_kickoff(payload: dict) -> datetime | None:
    # A timezone-free kickoff alone is never sufficient for historical pricing.
    for field in ("kickoffUtc", "kickoff_utc", "kickoffAt", "kickoff_at", "kickoff"):
        raw = payload.get(field)
        if isinstance(raw, str) and raw:
            try:
                value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if value.tzinfo is not None:
                    return value.astimezone(timezone.utc)
            except ValueError:
                pass
    # Source contract: timezone-free ISO-8601 kickoff timestamps mean UTC,
    # not Athens. Reject a time-only value as its timezone cannot be verified.
    return None


def source_fixture_id(payload: dict) -> str | None:
    # Payload versions have used both flat and nested API-Football identifiers.
    for key in ("apiFixtureId", "fixtureId", "fixture_id", "api_fixture_id",
                "providerFixtureId"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    nested = payload.get("fixture")
    if isinstance(nested, dict) and nested.get("id") is not None:
        return str(nested["id"])
    squad = payload.get("squadContext")
    if isinstance(squad, dict) and squad.get("apiFootballFixtureId") is not None:
        return str(squad["apiFootballFixtureId"])
    return None


def export_date(
    root: Path, target: date, out, seen: set[str],
    resolver: CachedFixtureLookup | None = None,
) -> dict:
    selected = snapshot_for(root, target)
    if selected is None:
        return {"date": target.isoformat(), "status": "NO_PRE_CUTOFF_MANIFEST"}
    commit, manifest = selected
    artifact = next((x for x in manifest.get("artifacts", [])
                     if x.get("id") == "app_ready_betting_bundle"), None)
    if artifact is None:
        return {"date": target.isoformat(), "status": "NO_BETTING_BUNDLE"}
    cutoff = datetime.combine(target, time(11, 0), ATHENS)
    archive_generated_at = verified_archive_timestamp(artifact, cutoff)
    if archive_generated_at is None:
        return {"date": target.isoformat(),
                "status": "MISSING_FUTURE_OR_STALE_ARCHIVED_BUNDLE_TIMESTAMP",
                "commit": commit}
    try:
        content = git(root, "show", f"{commit}:{artifact['path']}")
    except subprocess.CalledProcessError:
        return {"date": target.isoformat(), "status": "ARCHIVED_BUNDLE_UNAVAILABLE",
                "commit": commit}
    # The normal App-Ready artifact already has an integrity fingerprint.
    from hashlib import sha256
    if sha256(content).hexdigest() != artifact.get("sha256"):
        raise ValueError("Archived App-Ready ZIP checksum mismatch")
    with zipfile.ZipFile(BytesIO(content)) as zipped:
        db_bytes = zipped.read("databases/statmaker_prepared_betting.db")
    resolver = resolver or CachedFixtureLookup(root)
    output_count = 0
    ignored = 0
    identity_rejections: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="sm-v2-archive-") as td:
        db = Path(td) / "snapshot.db"
        db.write_bytes(db_bytes)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(prepared_selections)")}
            needed = {"competition_id", "snapshot_version", "selection_key", "match_key",
                      "selection_odd", "identity_sub_market_key", "identity_selection_side",
                      "identity_team_side", "identity_line", "selection_team"}
            if not cols.issuperset(needed):
                raise ValueError(f"Missing prepared_selections columns: {needed - cols}")
            matches = {(
                r["competition_id"], r["snapshot_version"], r["match_key"]
            ): json.loads(r["payload"]) for r in con.execute(
                "SELECT competition_id, snapshot_version, match_key, payload FROM prepared_matches"
            )}
            for selection in con.execute(
                """SELECT competition_id, snapshot_version, selection_key, match_key,
                          selection_odd, identity_sub_market_key, identity_selection_side,
                          identity_team_side, identity_line, selection_team
                   FROM prepared_selections"""
            ):
                m = matches.get((selection["competition_id"], selection["snapshot_version"],
                                 selection["match_key"]))
                if not m:
                    ignored += 1
                    continue
                kickoff = datetime_kickoff(m)
                if kickoff is None or kickoff <= cutoff.astimezone(timezone.utc):
                    ignored += 1
                    continue
                # The UI date is in Athens, whereas source date may be UTC.
                if kickoff.astimezone(ATHENS).date() != target:
                    continue
                try:
                    odd = float(selection["selection_odd"])
                except (TypeError, ValueError):
                    ignored += 1
                    continue
                if not 1.8 < odd < 3.0:
                    continue
                key = "|".join([target.isoformat(), selection["competition_id"],
                                selection["selection_key"]])
                if key in seen:
                    continue
                fixture_id, match_status = resolver.resolve(
                    m, kickoff, source_fixture_id(m)
                )
                if fixture_id is None:
                    ignored += 1
                    identity_rejections[match_status] = (
                        identity_rejections.get(match_status, 0) + 1
                    )
                    continue
                seen.add(key)
                record = {
                    "date": target.isoformat(), "sourceCommit": commit,
                    "generationContentVersion": manifest.get("contentVersion"),
                    "archiveBundleGeneratedAt": archive_generated_at.isoformat(),
                    "quoteCutoff": cutoff.isoformat(),
                    "quoteCutoffIsObservationTimestamp": False,
                    "priceObservationTimestampVerified": False,
                    "priceUniverse": "LEGACY_PREPARED_SELECTIONS",
                    "kickoffUTC": kickoff.isoformat(),
                    "competitionId": selection["competition_id"],
                    "snapshotVersion": selection["snapshot_version"],
                    "selectionKey": selection["selection_key"],
                    "matchKey": selection["match_key"],
                    "fixtureId": fixture_id,
                    "identityResolution": match_status,
                    "homeTeam": m.get("homeTeam"), "awayTeam": m.get("awayTeam"),
                    "leagueCode": m.get("leagueCode"),
                    "market": selection["identity_sub_market_key"],
                    "direction": selection["identity_selection_side"],
                    "teamSide": selection["identity_team_side"],
                    "line": selection["identity_line"],
                    "team": selection["selection_team"],
                    "odd": odd,
                }
                out.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                output_count += 1
        finally:
            con.close()
    return {
        "date": target.isoformat(), "status": "OK", "commit": commit,
        "exportedQuotes": output_count, "skippedUnsafeOrUnmatched": ignored,
        "priceUniverse": "LEGACY_PREPARED_SELECTIONS",
        "independentlyVerifiedUnfilteredOffers": False,
        "archiveBundleGeneratedAt": archive_generated_at.isoformat(),
        "identityRejections": identity_rejections
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--from-date", type=date.fromisoformat, required=True)
    ap.add_argument("--to-date", type=date.fromisoformat, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    if args.to_date < args.from_date:
        ap.error("to-date must be >= from-date")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    statuses = []
    resolver = CachedFixtureLookup(args.data_root)
    with args.output.open("w", encoding="utf-8") as out:
        day = args.from_date
        while day <= args.to_date:
            status = export_date(args.data_root, day, out, seen, resolver)
            print(status, flush=True)
            statuses.append(status)
            day += timedelta(days=1)
    audit = args.output.with_suffix(".audit.json")
    audit.write_text(json.dumps(statuses, indent=2) + "\n", encoding="utf-8")
    print("Historical quotes:", args.output)
    print("No model output, no backtest, no certification produced.")


if __name__ == "__main__":
    main()
