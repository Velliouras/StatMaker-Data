#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


EXPECTED_SCHEMA = 11
EXPECTED_RULES = "pattern-policy-v2-final-read-model-v5-performance-shadow-v1"
EXPECTED_STATMAKER_COMMIT = "561e152bc8302bb8240131cefc65b5350522c180"
FORBIDDEN_TARGET_MARKERS = (
    "d06364ab2625815aeafcb48ae93d6a328f7d6ac5",
    "pattern-policy-v2-final-read-model-v6-probability-parity-v1",
    "54e9bd4e28b29a0eb6313f4d16da4c99f27490b9",
)
COMPETITIONS = {
    "domestic",
    "champions_league",
    "europa_league",
    "conference_league",
}


class Report:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.errors: list[str] = []
        self.info: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def note(self, message: str) -> None:
        self.info.append(message)

    def finish(self) -> int:
        print(f"APP_READY_AGGREGATE_{self.mode.upper()}_REPORT")
        for message in self.errors:
            print("ERROR:", message)
        for message in self.info:
            print("INFO:", message)
        print(
            "APP_READY_AGGREGATE_SUMMARY",
            f"mode={self.mode}",
            f"errors={len(self.errors)}",
            f"info={len(self.info)}",
        )
        return 1 if self.errors else 0


def read_text(path: Path, report: Report) -> str:
    if not path.is_file():
        report.error(f"missing file: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def require_markers(text: str, markers: tuple[str, ...], label: str, report: Report) -> None:
    missing = [marker for marker in markers if marker not in text]
    if missing:
        report.error(f"{label} missing markers: {missing}")


def validate_source(root: Path, private_root: Path | None, report: Report) -> None:
    stage = read_text(root / "scripts/stage_app_ready_producer.sh", report)
    workflow = read_text(root / ".github/workflows/app-ready-artifact-publisher.yml", report)
    runner = read_text(root / "scripts/run_app_ready_emulator.sh", report)
    materializer = read_text(root / "scripts/materialize_app_ready_pattern_candidates.py", report)
    builder = read_text(root / "scripts/build_app_ready_from_device.py", report)
    provider_validator = read_text(
        root / "scripts/validate_domestic_cache_provider_identity.py", report
    )

    active_contract_files = {
        "stage": stage,
        "workflow": workflow,
        "runner": runner,
        "materializer": materializer,
        "builder": builder,
    }
    for label, text in active_contract_files.items():
        leaked = [marker for marker in FORBIDDEN_TARGET_MARKERS if marker in text]
        if leaked:
            report.error(f"{label} contains post-v6 target markers: {leaked}")

    require_markers(
        stage,
        (EXPECTED_STATMAKER_COMMIT, EXPECTED_RULES, 'PREPARED_SCHEMA="11"'),
        "producer stage",
        report,
    )
    require_markers(
        workflow,
        (
            "bash \"$GITHUB_WORKSPACE/scripts/stage_app_ready_producer.sh\"",
            "APP_READY_PREPARED_SCHEMA_VERSION: \"11\"",
            f'APP_READY_PATTERN_RULES_FINGERPRINT: "{EXPECTED_RULES}"',
            f'APP_READY_STATMAKER_COMMIT: "{EXPECTED_STATMAKER_COMMIT}"',
        ),
        "publisher workflow",
        report,
    )
    require_markers(
        runner,
        (
            "validate_app_ready_prepared_contract.py",
            "--kind checkpoint",
            "--kind seed",
            'if user_version != 11:',
            '"preparedBettingSchemaVersion":user_version',
            '"preparedPatternRulesFingerprint":expected_rules',
            '"statmakerCommit":expected_statmaker_commit',
        ),
        "emulator runner",
        report,
    )
    require_markers(
        materializer,
        (
            f'RULES_FINGERPRINT = "{EXPECTED_RULES}"',
            'if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 11:',
            "edge = posterior - market_probability",
            "expected_value = posterior * odd - 1.0",
            "def policy_decision(match, posterior, maturity, eligible):",
        ),
        "host materializer",
        report,
    )
    require_markers(
        builder,
        (
            f'EXPECTED_RULES = "{EXPECTED_RULES}"',
            "if version != 11:",
            "APP_READY_PRE_V6_POSTFLIGHT_OK",
        ),
        "bundle builder",
        report,
    )

    retirement_call = 'run(sys.executable, str(retirement), "--root", ".")'
    engine_patch_call = 'python "$GITHUB_WORKSPACE/scripts/patch_app_ready_producer.py"'
    if retirement_call not in provider_validator:
        report.error("provider preflight does not retire Asian/Handicap inputs")
    if workflow.find("Validate canonical Domestic provider identity") > workflow.find(
        "Stage off-device legacy builder"
    ):
        report.error("input retirement is not ordered before producer staging")
    if engine_patch_call not in stage:
        report.error("producer stage does not invoke the pinned pre-v6 patch")

    trigger_section = ""
    try:
        trigger_section = workflow.split("  push:", 1)[1].split("  schedule:", 1)[0]
    except IndexError:
        report.error("could not isolate heavy publisher push trigger")
    forbidden_trigger_paths = (
        "scripts/stage_app_ready_producer.sh",
        "scripts/run_app_ready_emulator.sh",
        "scripts/validate_app_ready_prepared_contract.py",
        "scripts/materialize_app_ready_pattern_candidates.py",
    )
    leaked_triggers = [path for path in forbidden_trigger_paths if path in trigger_section]
    if leaked_triggers:
        report.error(f"pipeline code still self-triggers heavy publisher: {leaked_triggers}")

    if private_root:
        models = read_text(
            private_root
            / "app/src/main/java/com/statmaker/app/PreparedPatternRecommendationModels.kt",
            report,
        )
        store = read_text(
            private_root
            / "app/src/main/java/com/statmaker/app/PreparedBettingSnapshotStore.kt",
            report,
        )
        require_markers(models, (EXPECTED_RULES,), "staged recommendation models", report)
        require_markers(
            store,
            ("private const val DATABASE_VERSION = 11",),
            "staged prepared store",
            report,
        )
        if "private const val DATABASE_VERSION = 12" in store:
            report.error("staged producer contains schema 12")

    for rel in (
        "data/statmaker/update_manifest.json",
        "odds/odds_api_io/domestic_odds.json",
        "mappings/domestic_team_aliases.json",
    ):
        path = root / rel
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("root is not an object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            report.error(f"invalid canonical JSON {rel}: {exc}")

    if not report.errors:
        report.note(
            "contract=pre-v6 schema=11 rules=" + EXPECTED_RULES
        )
        report.note("old posterior/value-tier materializer semantics present")
        report.note("Asian/Handicap retirement ordered before producer")
        report.note("checkpoint and published seed compatibility gates active")


def validate_checkpoint(root: Path, metadata: Path, report: Report) -> None:
    command = [
        sys.executable,
        str(Path(__file__).with_name("validate_app_ready_prepared_contract.py")),
        str(root),
        "--metadata",
        str(metadata),
        "--kind",
        "checkpoint",
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    output = " | ".join(
        line.strip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    )
    if completed.returncode:
        report.error(output or "checkpoint contract validation failed")
    else:
        report.note(output or "checkpoint contract valid")


def validate_generated(root: Path, report: Report) -> None:
    db_path = root / "databases/statmaker_prepared_betting.db"
    if not db_path.is_file() or db_path.stat().st_size <= 16:
        report.error(f"missing generated prepared DB: {db_path}")
        return
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        quick = con.execute("PRAGMA quick_check").fetchone()
        schema = int(con.execute("PRAGMA user_version").fetchone()[0])
        ready = {
            str(row[0])
            for row in con.execute(
                "SELECT competition_id FROM prepared_snapshot_meta WHERE state='ready'"
            )
        }
        rules = {
            str(row[0])
            for row in con.execute(
                "SELECT DISTINCT rules_fingerprint FROM prepared_pattern_generation WHERE state='ready'"
            )
        }
    except sqlite3.Error as exc:
        report.error(f"invalid generated prepared DB: {exc}")
        return
    finally:
        con.close()
    if not quick or quick[0] != "ok":
        report.error(f"generated DB quick_check failed: {quick}")
    if schema != EXPECTED_SCHEMA:
        report.error(f"generated schema {schema} != {EXPECTED_SCHEMA}")
    if ready != COMPETITIONS:
        report.error(f"generated READY snapshots {sorted(ready)} != {sorted(COMPETITIONS)}")
    if rules != {EXPECTED_RULES}:
        report.error(f"generated rules {sorted(rules)} != {[EXPECTED_RULES]}")
    if not report.errors:
        report.note(f"generated schema={schema} rules={EXPECTED_RULES} ready=4")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("source", "checkpoint", "generated"), required=True)
    parser.add_argument("--repository-root", default=".")
    parser.add_argument("--private-root")
    parser.add_argument("--checkpoint-root")
    parser.add_argument("--checkpoint-metadata")
    parser.add_argument("--generated-root")
    args = parser.parse_args()

    root = Path(args.repository_root).resolve()
    report = Report(args.mode)
    if args.mode == "source":
        private = Path(args.private_root).resolve() if args.private_root else None
        validate_source(root, private, report)
    elif args.mode == "checkpoint":
        if not args.checkpoint_root or not args.checkpoint_metadata:
            report.error("--checkpoint-root and --checkpoint-metadata are required")
        else:
            validate_checkpoint(
                Path(args.checkpoint_root).resolve(),
                Path(args.checkpoint_metadata).resolve(),
                report,
            )
    else:
        if not args.generated_root:
            report.error("--generated-root is required")
        else:
            validate_generated(Path(args.generated_root).resolve(), report)
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
