"""Shared, source-only PROD producer contract. Importing this module does no IO."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct

EVIDENCE_CONTRACT = "statmaker-betting-v2-evidence-v1"
CERTIFICATE_CONTRACT = "statmaker-betting-v2-certification-v1"
FORECAST_CONTRACT = "statmaker-betting-v2-forecast-v1"
PRODUCER_VERSION = "matchup-prod-v1"
MODES = ("XG_PRIMARY", "ELO_GOALS_FALLBACK_NO_XG")
TIERS = ("HIGH", "CONFIRMED", "DEVELOPING")


class Blocked(ValueError):
    pass


def require(condition, reason: str) -> None:
    if not condition:
        raise Blocked(reason)


def number(value) -> float:
    require(not isinstance(value, bool), "BOOLEAN_NUMBER")
    try:
        parsed = float(value)
    except (ValueError, TypeError):
        raise Blocked("MISSING_NUMBER")
    require(math.isfinite(parsed), "NONFINITE_NUMBER")
    return parsed


def milliseconds(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        require(value > 0, "INVALID_TIMESTAMP")
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise Blocked("MISSING_ABSOLUTE_TIMESTAMP")
    require(parsed.tzinfo is not None, "NAIVE_TIMESTAMP")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def canonical(document) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(document) -> str:
    return hashlib.sha256(canonical(document).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        result = hashlib.file_digest(stream, "sha256")
    return result.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def safe_child(root: Path, relative: str) -> Path:
    child = (root / relative).resolve()
    require(child.is_relative_to(root.resolve()) and not child.is_symlink(), "UNSAFE_SOURCE_PATH")
    return child


def save_new(path: Path, document) -> None:
    """Immutable run artifacts; never overwrite an earlier evaluation or a PROD file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical(document) + "\n")


def selection_id(row: dict) -> str:
    """Byte-for-byte Kotlin bettingV2ExactSelectionId, including signed Double bits."""
    line = row.get("line")
    bits = "" if line is None else str(struct.unpack(">q", struct.pack(">d", number(line)))[0])
    return "\u0001".join((row["competitionId"], row["fixtureId"], row["market"].strip(),
                          row["selection"].strip(), (row.get("team") or "").strip(), bits))


def evidence(**fields) -> dict:
    return {"contract": EVIDENCE_CONTRACT, "researchOnly": False, **fields}


class Documents:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def add(self, document: dict) -> str:
        require(document.get("contract") == EVIDENCE_CONTRACT, "EVIDENCE_CONTRACT_MISMATCH")
        key = digest(document)
        self.rows[key] = document
        return key


def verify_plan(plan: dict) -> None:
    require(plan.get("contract") == "statmaker-betting-v2-evaluation-plan-v1", "UNKNOWN_EVALUATION_PLAN")
    times = [milliseconds(plan[key]) for key in ("trainingStartMs", "trainingEndMs",
             "calibrationStartMs", "calibrationEndMs", "policyFreezeAtMs",
             "holdoutStartMs", "holdoutEndMs", "evaluateAtMs", "validUntilMs")]
    require(times[0] <= times[1] < times[2] <= times[3] <= times[4] < times[5]
            <= times[6] <= times[7] < times[8], "LEAKING_SPLIT_PLAN")
    require(times[2] <= plan["calibrationFitEndMs"] < plan["calibrationPolicyStartMs"]
            <= times[3], "NO_INDEPENDENT_CALIBRATION_ERROR_PERIOD")
    require(milliseconds(plan["planObservedAtMs"]) <= times[2], "PLAN_NOT_PRECOMMITTED_BEFORE_CALIBRATION")
    require(plan["mode"] in MODES and bool(plan["parameterCandidates"]), "NO_MODEL_SEARCH_PLAN")
    require(plan["minimumTrainCases"] > 0 and plan["minimumCalibrationCases"] > 0
            and plan["minimumHoldoutCases"] > 0, "NO_EMPIRICAL_SAMPLE_PLAN")
    for key in ("minimumCalibrationBinCases", "minimumHoldoutBinCases", "minimumHoldoutStrongCases",
                "minimumIndependentFixtures", "minimumIndependentCalibrationFixtures",
                "minimumIndependentHoldoutBinFixtures", "calibrationBinCount", "maximumDistributionCount",
                "bootstrapReplicates"):
        require(isinstance(plan[key], int) and not isinstance(plan[key], bool) and plan[key] > 0,
                "INVALID_PRECOMMITTED_SAMPLE_OR_NUMERICAL_PLAN")
    require(isinstance(plan["bootstrapSeed"], int) and 0 <= plan["bootstrapSeed"], "MISSING_REPRODUCIBLE_BOOTSTRAP_PLAN")
    require(0 <= number(plan["maximumHoldoutCalibrationError"]) < 1
            and 1.8 <= number(plan["oddsBandFrom"]) < number(plan["oddsBandTo"]) <= 3,
            "INVALID_COMPARABLE_COHORT_PLAN")
    require(plan["contextColumns"] == ["homeUnavailablePlayers", "awayUnavailablePlayers"],
            "UNKNOWN_GROUNDED_CONTEXT_SCHEMA")
    for candidate in plan["parameterCandidates"]:
        for key in ("recentWindow", "longWindow", "venueWindow", "minimumHistory", "maximumFeatureAgeMs", "maximumFitIterations"):
            require(isinstance(candidate[key], int) and candidate[key] > 0, "INVALID_MODEL_SEARCH_PARAMETER")
        require(candidate["minimumHistory"] <= min(candidate[k] for k in ("recentWindow", "longWindow", "venueWindow"))
                and candidate["recentWindow"] <= candidate["longWindow"]
                and number(candidate["eloScale"]) > 0 and number(candidate["eloK"]) > 0
                and number(candidate["regularization"]) >= 0, "INVALID_MODEL_SEARCH_PARAMETER")
    require(0 < number(plan["confidenceAlpha"]) < 1, "INVALID_CONFIDENCE_PLAN")
    require(0 < number(plan["tailMassTolerance"]) < 1, "INVALID_DISTRIBUTION_PLAN")
    require(0 < number(plan["correlationThreshold"]) <= 1, "NO_CORRELATION_PLAN")
    require(set(plan["tierQualityQuantiles"]) == set(TIERS), "NO_TIER_SEARCH_PLAN")
    require(0 <= plan["tierQualityQuantiles"]["DEVELOPING"]
            < plan["tierQualityQuantiles"]["CONFIRMED"]
            < plan["tierQualityQuantiles"]["HIGH"] <= 1, "INVALID_TIER_SEARCH_PLAN")
    require(bool(plan["walkForwardFolds"]), "NO_WALK_FORWARD_FOLDS")
    for fold in plan["walkForwardFolds"]:
        require(times[0] <= fold["fitStartMs"] <= fold["fitEndMs"] < fold["testStartMs"]
                <= fold["testEndMs"] <= times[1], "LEAKING_MODEL_SELECTION_FOLD")
