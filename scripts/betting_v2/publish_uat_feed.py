#!/usr/bin/env python3
"""Publish real, uncertified V2 candidates to a UAT-only feed. No provider calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

FEED_PATH = Path("data/statmaker/uat/betting_v2")


def publish(root: Path, seed: Path | None = None) -> Path:
    if seed is not None:
        payload = seed.read_bytes()
    else:
        manifest = json.loads((root / "data/statmaker/app_ready/update_manifest.json").read_bytes())
        artifact = next(a for a in manifest["artifacts"] if a["id"] == "app_ready_betting_bundle")
        bundle = root / artifact["path"]
        generation = manifest["metadata"]["preparedPatternGenerationId"]
        with tempfile.TemporaryDirectory(prefix="statmaker-v2-uat-") as temp:
            output = Path(temp) / "candidates.json"
            subprocess.run([
                sys.executable, "-m", "scripts.betting_v2.run_real_prod_diagnostic",
                "--repository-root", str(root), "--app-ready", str(bundle),
                "--generation-id", generation, "--output", str(output)
            ], cwd=root, check=True)
            report = json.loads(output.read_bytes())
        # Missing/ambiguous bookmaker binding cannot become a displayed quote.
        report["candidates"] = [
            row for row in report["candidates"]
            if row.get("bookmakerBinding") == "EXACT_UNIQUE_BUNDLED_QUOTE"
            and row.get("bookmaker")
        ]
        report["candidateCount"] = len(report["candidates"])
        payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")

    report = json.loads(payload)
    if (report["status"] != "REAL_PROD_INPUT_DIAGNOSTIC"
            or report["apiCalls"] != 0 or report["prodWrites"] != 0
            or not report["probabilitiesNotEmpiricallyCertified"]):
        raise ValueError("Not an offline, uncertified real-input report")
    if len(report["candidates"]) != report["candidateCount"]:
        raise ValueError("Candidate count mismatch")
    for row in report["candidates"]:
        if (row["certified"] or row["bookmakerBinding"] != "EXACT_UNIQUE_BUNDLED_QUOTE"
                or not row["bookmaker"]):
            raise ValueError("Invalid UAT quote")

    sha = hashlib.sha256(payload).hexdigest()
    directory = root / FEED_PATH
    directory.mkdir(parents=True, exist_ok=True)
    artifact = f"candidates-{sha}.json"
    target = directory / artifact
    if not target.exists():
        target.write_bytes(payload)
    index = {
        "schemaVersion": 1, "channel": "uat", "artifact": artifact, "sha256": sha,
        "generationId": report["generationId"], "generatedAt": report["generatedAt"],
        "candidateCount": report["candidateCount"], "status": "UNVERIFIED",
        "sourceBundleSha256": report["sourceBundle"]["verifiedSha256"],
        "apiCalls": 0, "prodWrites": 0
    }
    pending = directory / "latest.pending"
    pending.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    pending.replace(directory / "latest.json")
    print(f"UAT_V2_FEED {artifact} candidates={report['candidateCount']} apiCalls=0 prodWrites=0")
    return directory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--seed", type=Path, help="Publish the supplied existing real snapshot without inference")
    args = parser.parse_args()
    publish(args.repository_root.resolve(), args.seed)


if __name__ == "__main__":
    main()
