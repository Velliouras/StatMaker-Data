#!/usr/bin/env python3
"""Fail-closed, cache-only Betting V2 shadow publisher.

This is NOT the production App-Ready publisher. It only produces an isolated
diagnostic manifest under reports/betting_v2. It cannot publish picks, update
PROD manifests, start Actions or make provider API calls.

Run:
 python scripts/betting_v2/publish_shadow.py --repository-root . \
   --output reports/betting_v2/shadow_manifest.json

Optional research reports can be passed with --model-report and
--priced-report. Their metrics are retained verbatim and are NEVER silently
converted to certified forecasts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from typing import Any

from audit_data import audit

CONTRACT = "statmaker-betting-v2-shadow-v1"
RESERVED_PREFIXES = (
    "data/statmaker/app_ready/",
    "data/statmaker/canonical_recommendation_ledger.json",
    "data/statmaker/update_manifest.json",
    "odds/",
    ".github/",
)
FORBIDDEN_ROOTS = ("app_ready", "odds", ".github")
# Both mandatory by policy: no import of API-Football client; no network.
PROVIDER_REQUEST_BUDGET = 0
CERTIFIED_FORECASTS = 0


def _safe_output(root: Path, output: Path) -> Path:
    resolved_root = root.resolve()
    resolved = (resolved_root / output).resolve() if not output.is_absolute() else output.resolve()
    try:
        rel = resolved.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ValueError("Refusing to write shadow output outside repository root") from exc
    if any(rel == prefix.rstrip("/") or rel.startswith(prefix)
           for prefix in RESERVED_PREFIXES):
        raise ValueError("Refusing to write into PRODUCTION data, odds or workflows")
    if not rel.startswith("reports/betting_v2/"):
        raise ValueError("Shadow output must remain inside reports/betting_v2/")
    if resolved.suffix != ".json":
        raise ValueError("Shadow output must be a JSON file")
    return resolved


def _json_file(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Missing or invalid input JSON: {path}") from exc
    if not isinstance(result, dict):
        raise ValueError("JSON research report must be an object")
    return result


def _checked_input(root: Path, raw: Path | None) -> tuple[dict | None, dict]:
    if raw is None:
        return None, {"provided": False}
    path = (root / raw).resolve() if not raw.is_absolute() else raw.resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Research input must live inside Data repository")
    if path.stat().st_size > 8_000_000:
        raise ValueError("Refusing oversized research input")
    data = _json_file(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return data, {
        "provided": True, "path": path.relative_to(root.resolve()).as_posix(),
        "sha256": digest, "contract": data.get("contract"),
        "certified": data.get("certified") is True,
    }


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".v2-shadow-",
        suffix=".tmp", delete=False
    ) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary_path = Path(tmp.name)
    os.replace(temporary_path, path)


def publish(root: Path, output: Path,
            model_report: Path | None = None,
            priced_report: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    dest = _safe_output(root, output)
    data_index = root / "data/statmaker/domestic_enriched/index.json"
    if not data_index.is_file():
        raise ValueError("Missing canonical cached domestic_enriched index")
    data_index_hash = hashlib.sha256(data_index.read_bytes()).hexdigest()
    model, model_provenance = _checked_input(root, model_report)
    priced, priced_provenance = _checked_input(root, priced_report)

    # Historical coverage only, read-only. No fixture/network fetches.
    coverage = audit(root)
    coverage_totals = coverage.get("totals") or {}
    coverage_groups = coverage.get("coverage") or {}
    errors = coverage.get("sourceErrors") or {}
    blocking = [
        "NO_VERIFIED_MODEL_CERTIFICATE",
        "NO_INDEPENDENT_OUT_OF_TIME_VALUE_CERTIFICATION",
        "NO_ADVERSE_LINEUP_SCENARIO_CERTIFICATION",
        "NO_GENERATION_BOUND_QUOTE_AND_EXACT_SELECTION_CERTIFICATES",
        "NO_APPROVED_PROD_APP_READY_IMPORT_CONTRACT",
    ]
    if model is None:
        blocking.append("MISSING_MODEL_RESEARCH")
    elif model.get("notCertified") is not True:
        blocking.append("UNRECOGNIZED_MODEL_RESEARCH_CONTRACT")
    if priced is None:
        blocking.append("MISSING_PRICED_HOLDOUT_RESEARCH")
    elif priced.get("certified") is True or priced.get("notValidForPromotion") is False:
        blocking.append("PRICED_REPORT_CLAIMS_UNVERIFIED_CERTIFICATION")
    if errors:
        blocking.append("INCOMPLETE_SOURCE_ARCHIVE")
    if not coverage_totals.get("completed"):
        blocking.append("NO_COMPLETED_SOURCE_GAMES")

    # Deliberately immutable: this script can NEVER issue even one real pick.
    manifest: dict[str, Any] = {
        "contract": CONTRACT, "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "SHADOW_DIAGNOSTICS_ONLY",
        "publishTarget": dest.relative_to(root).as_posix(),
        "productionAppReadyModified": False,
        "androidProdChanged": False,
        "liveRecommendationsPublished": CERTIFIED_FORECASTS,
        "certifiedForecasts": [],
        "certificationStatus": "BLOCKED",
        "blockingReasons": blocking,
        "api": {
            "provider": "API-Football",
            "callsMadeByThisPublisher": PROVIDER_REQUEST_BUDGET,
            "extraCallsAllowed": PROVIDER_REQUEST_BUDGET,
            "remainingDailyQuota": None,
            "remainingQuotaIsUnknown": True,
            "existingGuardDefaultReserve": 1500,
        },
        "source": {
            "domesticEnrichedIndex": "data/statmaker/domestic_enriched/index.json",
            "indexSha256": data_index_hash,
            "completedArchivedFixturesAudited": coverage_totals.get("completed", 0),
            "matchesWithBothObservedXg": coverage_groups.get("completed_with_both_xg", 0),
            "sourceErrors": errors,
            "leaguesAudited": len(coverage.get("leagues") or []),
        },
        "researchReports": {
            "model": model_provenance,
            "pricedHoldout": priced_provenance,
        },
        "researchSnapshotMetrics": {
            "model": {
                "eligiblePrematchRows": (model or {}).get("eligiblePrematchRows"),
                "holdoutFixtures": ((model or {}).get("untouchedHoldout") or {}).get("n"),
            },
            "priced": {
                "quotes": ((priced or {}).get("holdout") or {}).get("exactQuotes"),
                "nonCorrelatedHoldoutROI": (
                    (priced or {}).get("oneGoalsScenarioPerFixture") or {}
                ).get("roi"),
            }
        }
    }
    _atomic_write(dest, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path,
                        default=Path("reports/betting_v2/shadow_manifest.json"))
    parser.add_argument("--model-report", type=Path, default=None)
    parser.add_argument("--priced-report", type=Path, default=None)
    args = parser.parse_args()
    published = publish(args.repository_root, args.output,
                        args.model_report, args.priced_report)
    print(json.dumps({
        "contract": published["contract"], "mode": published["mode"],
        "publishedRecommendations": published["liveRecommendationsPublished"],
        "apiRequests": published["api"]["callsMadeByThisPublisher"],
        "blockingReasons": published["blockingReasons"],
        "path": published["publishTarget"]
    }, ensure_ascii=False))
    print("NO PROD artifacts, Actions or provider APIs touched.")


if __name__ == "__main__":
    main()
