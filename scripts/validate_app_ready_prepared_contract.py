#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path


EXPECTED_SCHEMA = 11
PROD_RULES = "pattern-policy-v2-final-read-model-v5-performance-shadow-v1"
PROD_STATMAKER_COMMIT = "561e152bc8302bb8240131cefc65b5350522c180"
UAT_SOURCE = os.environ.get("APP_READY_UAT_SOURCE", "false").lower() == "true"
if UAT_SOURCE:
    EXPECTED_RULES = os.environ.get("APP_READY_PATTERN_RULES_FINGERPRINT", "").strip()
    EXPECTED_STATMAKER_COMMIT = os.environ.get("APP_READY_STATMAKER_COMMIT", "").strip()
    if not EXPECTED_RULES or not EXPECTED_STATMAKER_COMMIT:
        raise SystemExit("UAT prepared contract requires rules fingerprint and StatMaker commit")
else:
    EXPECTED_RULES = PROD_RULES
    EXPECTED_STATMAKER_COMMIT = PROD_STATMAKER_COMMIT
EXPECTED_COMPETITIONS = {
    "domestic",
    "champions_league",
    "europa_league",
    "conference_league",
}
REQUIRED_CHECKPOINT_FILES = (
    "databases/statmaker.db",
    "databases/statmaker_prepared_betting.db",
    "files/domestic_normalized_stats_v2.bin",
    "files/statmaker_stats_snapshots/champions_league.bin",
    "files/statmaker_stats_snapshots/europa_league.bin",
    "files/statmaker_stats_snapshots/conference_league.bin",
    "files/app_ready_odds/domestic.json",
    "files/app_ready_odds/champions_league.json",
    "files/app_ready_odds/europa_league.json",
    "files/app_ready_odds/conference_league.json",
    "shared_prefs/statmaker_prepared_data_versions.xml",
    "shared_prefs/statmaker_data_manifests.xml",
    "shared_prefs/statmaker_uefa_support_history.xml",
)
REQUIRED_SEED_FILES = (
    "databases/statmaker_prepared_betting.db",
    "files/app_ready_odds/domestic.json",
    "files/app_ready_odds/champions_league.json",
    "files/app_ready_odds/europa_league.json",
    "files/app_ready_odds/conference_league.json",
)


class ContractError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_metadata(path: Path, kind: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid {kind} metadata {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"invalid {kind} metadata root: {path}")
    if kind == "seed":
        payload = payload.get("metadata") or {}
        if not isinstance(payload, dict):
            raise ContractError(f"invalid seed manifest metadata: {path}")
    return payload


def require_files(root: Path, kind: str) -> None:
    required = REQUIRED_CHECKPOINT_FILES if kind == "checkpoint" else REQUIRED_SEED_FILES
    missing = [rel for rel in required if not (root / rel).is_file() or (root / rel).stat().st_size <= 0]
    if missing:
        raise ContractError("missing required files: " + ", ".join(missing))


def inspect_database(root: Path) -> tuple[int, int, set[str]]:
    db_path = root / "databases/statmaker_prepared_betting.db"
    if not db_path.is_file() or db_path.stat().st_size <= 16:
        raise ContractError(f"missing/empty prepared database: {db_path}")
    with db_path.open("rb") as handle:
        header = handle.read(16)
    if header != b"SQLite format 3\x00":
        raise ContractError(f"invalid prepared SQLite header: {db_path}")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise ContractError(f"prepared database quick_check failed: {quick}")
        schema = int(con.execute("PRAGMA user_version").fetchone()[0])
        if schema != EXPECTED_SCHEMA:
            raise ContractError(f"prepared schema {schema} != {EXPECTED_SCHEMA}")

        tables = {
            str(row[0])
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required_tables = {"prepared_snapshot_meta", "prepared_selections"}
        missing_tables = sorted(required_tables - tables)
        if missing_tables:
            raise ContractError("prepared database missing tables: " + ", ".join(missing_tables))

        ready = {
            str(row[0])
            for row in con.execute(
                "SELECT competition_id FROM prepared_snapshot_meta WHERE state='ready'"
            )
        }
        if ready != EXPECTED_COMPETITIONS:
            raise ContractError(
                f"READY snapshots {sorted(ready)} != {sorted(EXPECTED_COMPETITIONS)}"
            )

        generation_rules: set[str] = set()
        if "prepared_pattern_generation" in tables:
            generation_rules = {
                str(row[0])
                for row in con.execute(
                    """
                    SELECT DISTINCT rules_fingerprint
                    FROM prepared_pattern_generation
                    WHERE state='ready' AND TRIM(COALESCE(rules_fingerprint,''))<>''
                    """
                )
            }
            unexpected = generation_rules - {EXPECTED_RULES}
            if unexpected:
                raise ContractError(
                    "incompatible READY generation rules: " + ", ".join(sorted(unexpected))
                )
        return schema, len(ready), generation_rules
    finally:
        con.close()


def validate(root: Path, metadata_path: Path, kind: str) -> None:
    require_files(root, kind)
    metadata = load_metadata(metadata_path, kind)
    schema, ready_count, generation_rules = inspect_database(root)

    metadata_schema = int(metadata.get("preparedBettingSchemaVersion") or 0)
    if metadata_schema != EXPECTED_SCHEMA:
        raise ContractError(
            f"metadata prepared schema {metadata_schema} != {EXPECTED_SCHEMA}"
        )
    metadata_rules = str(metadata.get("preparedPatternRulesFingerprint") or "")
    if metadata_rules != EXPECTED_RULES:
        raise ContractError(
            f"metadata rules {metadata_rules or '<missing>'} != {EXPECTED_RULES}"
        )

    if kind == "checkpoint":
        metadata_commit = str(metadata.get("statmakerCommit") or "")
        if metadata_commit != EXPECTED_STATMAKER_COMMIT:
            raise ContractError(
                "metadata StatMaker commit "
                f"{metadata_commit or '<missing>'} != {EXPECTED_STATMAKER_COMMIT}"
            )
        metadata_ready = int(metadata.get("preparedReadyCount") or 0)
        if metadata_ready != ready_count:
            raise ContractError(
                f"metadata preparedReadyCount {metadata_ready} != DB READY count {ready_count}"
            )
        expected_sha = str(metadata.get("preparedDbSha256") or "")
        if not expected_sha:
            raise ContractError("checkpoint metadata has no preparedDbSha256")
        actual_sha = sha256_file(root / "databases/statmaker_prepared_betting.db")
        if actual_sha != expected_sha:
            raise ContractError(
                f"prepared DB SHA256 {actual_sha} != checkpoint {expected_sha}"
            )
    else:
        metadata_commit = str(metadata.get("statmakerCommit") or "")
        if metadata_commit and metadata_commit != EXPECTED_STATMAKER_COMMIT:
            raise ContractError(
                f"seed StatMaker commit {metadata_commit} != {EXPECTED_STATMAKER_COMMIT}"
            )

    print(
        "APP_READY_PREPARED_CONTRACT_OK",
        f"kind={kind}",
        f"schema={schema}",
        f"rules={EXPECTED_RULES}",
        f"statmaker={EXPECTED_STATMAKER_COMMIT[:12]}",
        f"ready={ready_count}",
        "generation_rules=" + (",".join(sorted(generation_rules)) or "none"),
    )


def create_fixture(root: Path, schema: int, rules: str, commit: str) -> Path:
    for rel in REQUIRED_CHECKPOINT_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith("statmaker_prepared_betting.db"):
            continue
        path.write_bytes(b"fixture")

    db_path = root / "databases/statmaker_prepared_betting.db"
    con = sqlite3.connect(db_path)
    try:
        con.execute(f"PRAGMA user_version={schema}")
        con.execute(
            "CREATE TABLE prepared_snapshot_meta (competition_id TEXT, state TEXT)"
        )
        con.execute("CREATE TABLE prepared_selections (selection_key TEXT)")
        con.execute(
            "CREATE TABLE prepared_pattern_generation "
            "(rules_fingerprint TEXT, state TEXT)"
        )
        con.executemany(
            "INSERT INTO prepared_snapshot_meta VALUES (?, 'ready')",
            [(competition,) for competition in sorted(EXPECTED_COMPETITIONS)],
        )
        con.execute(
            "INSERT INTO prepared_pattern_generation VALUES (?, 'ready')",
            (rules,),
        )
        con.commit()
    finally:
        con.close()

    metadata = {
        "preparedBettingSchemaVersion": schema,
        "preparedPatternRulesFingerprint": rules,
        "statmakerCommit": commit,
        "preparedReadyCount": len(EXPECTED_COMPETITIONS),
        "preparedDbSha256": sha256_file(db_path),
    }
    metadata_path = root / "checkpoint.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return metadata_path


def self_check() -> None:
    with tempfile.TemporaryDirectory(prefix="app-ready-contract-") as tmp:
        root = Path(tmp) / "valid"
        metadata = create_fixture(
            root,
            EXPECTED_SCHEMA,
            EXPECTED_RULES,
            EXPECTED_STATMAKER_COMMIT,
        )
        validate(root, metadata, "checkpoint")

        invalid_cases = (
            (12, EXPECTED_RULES, EXPECTED_STATMAKER_COMMIT, "schema12"),
            (
                EXPECTED_SCHEMA,
                "pattern-policy-v2-final-read-model-v6-probability-parity-v1",
                EXPECTED_STATMAKER_COMMIT,
                "v6-rules",
            ),
            (EXPECTED_SCHEMA, EXPECTED_RULES, "d06364ab", "wrong-engine"),
        )
        for index, (schema, rules, commit, label) in enumerate(invalid_cases):
            invalid_root = Path(tmp) / f"invalid-{index}"
            invalid_metadata = create_fixture(invalid_root, schema, rules, commit)
            try:
                validate(invalid_root, invalid_metadata, "checkpoint")
            except ContractError:
                print("APP_READY_PREPARED_CONTRACT_REJECT_OK", label)
            else:
                raise SystemExit(f"self-check failed to reject {label}")
    print("APP_READY_PREPARED_CONTRACT_SELF_CHECK_OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?")
    parser.add_argument("--metadata")
    parser.add_argument("--kind", choices=("checkpoint", "seed"), default="checkpoint")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    if args.self_check:
        self_check()
        return 0
    if not args.root or not args.metadata:
        parser.error("root and --metadata are required unless --self-check is used")
    try:
        validate(Path(args.root), Path(args.metadata), args.kind)
    except ContractError as exc:
        print("APP_READY_PREPARED_CONTRACT_REJECT", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
