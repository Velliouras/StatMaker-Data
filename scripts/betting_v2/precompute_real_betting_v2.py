#!/usr/bin/env python3
"""Precompute REAL forward Betting V2 UAT suggestions from frozen models + bookmaker quotes.

Input is the published, actual-data shadow preview produced from archived
pre-kickoff xG/ELO model forecasts and the canonical Odds-API.io bookmaker feed.
No mock events, provider calls, invented odds, retroactive results, or Android
production writes. Only independently matched API-Football IDs participate.
This producer publishes ONLY independently certified STRONG HIGH / CONFIRMED /
DEVELOPING. Uncertified raw model odds stay diagnostics, never recommendations.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from math import isfinite
from hashlib import sha256
from pathlib import Path
import json

from fixture_lookup import as_utc

SOURCE = Path("reports/betting_v2/pilot_forward_shadow_preview.json")
DESTINATION = Path("reports/betting_v2/betting_v2_precompute.json")
HISTORY = Path("reports/betting_v2/precompute_history")
CONTRACT = "statmaker-v2-uat-real-precompute-v1"
MARKETS = frozenset({"1X2", "DOUBLE_CHANCE", "MATCH_GOALS", "TEAM_TOTAL_GOALS"})
MIN_ODD = 1.80
MAX_ODD = 3.00
MIN_PROBABILITY = 0.60
MIN_INDICATIVE_EV = 0.0
MAX_BYTES = 3_000_000


def candidate(match: dict, *, now: datetime, rejects: Counter) -> dict | None:
    kickoff = as_utc(match.get("kickoffUTC"))
    forecast_at = as_utc(match.get("forecastComputedAtUTC"))
    if kickoff is None or forecast_at is None or not forecast_at < kickoff or kickoff <= now:
        rejects["NOT_UPCOMING_PREMATCH_FORECAST"] += 1
        return None
    if not match.get("independentApiFootballFixtureVerified") or not (
        str(match.get("independentApiFootballFixtureId") or "").strip()
    ):
        rejects["MISSING_INDEPENDENT_FIXTURE_ID"] += 1
        return None
    if match.get("certifiedStrong") is not False:
        rejects["UNSAFE_CERTIFICATION_FLAG"] += 1
        return None
    if (str(match.get("model") or "") not in
            ("XG_PRIMARY", "ELO_GOALS_FALLBACK_NO_XG")):
        rejects["UNKNOWN_MODEL"] += 1
        return None

    # Diagnostics from actual prices are allowed, but are NEVER a certified
    # Strong recommendation. Raw model p is not a conservative calibrated p.
    admissible = []
    for offer in match.get("marketEvidence") or []:
        if not isinstance(offer, dict) or offer.get("market") not in MARKETS:
            continue
        try:
            odd = float(offer["odds"])
            raw_probability = float(offer["modelProbability"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if not (isfinite(odd) and isfinite(raw_probability) and
                MIN_ODD < odd < MAX_ODD and raw_probability >= MIN_PROBABILITY):
            continue
        admissible.append(offer)
    if not admissible:
        rejects["NO_STRICT_ODDS_RAW_PROBABILITY_SCREEN"] += 1
        return None

    # This snapshot deliberately has no independently signed/versioned
    # calibration + adverse-case evidence; no blanket SOLID workaround.
    # A future certificate producer must prove these fields from an immutable
    # independent holdout and original bookmaker timestamps before this
    # producer may issue any Strong grades.
    certified = match.get("independentConservativeForecastCertificate")
    if not isinstance(certified, dict):
        rejects["MISSING_INDEPENDENT_CONSERVATIVE_CALIBRATION"] += 1
        return None

    # Mere claims or booleans in an untrusted preview are not proof. Until an
    # independent audit importer verifies source hashes, holdout, scenario,
    # selected quote and issuer identity, fail closed even if JSON advertises
    # a superficially complete certificate.
    rejects["CERTIFICATE_IMPORTER_NOT_YET_VERIFIED"] += 1
    return None

def compute(document: dict, *, now: datetime) -> dict:
    if now.utcoffset() is None:
        raise ValueError("Precompute timestamp must be timezone aware")
    now = now.astimezone(timezone.utc)
    if (document.get("contract") != "statmaker-v2-uat-forecast-preview-v1"
        or document.get("certificationStatus") != "BLOCKED"
        or document.get("researchOnly") is not True
        or document.get("certifiedStrong") != 0
        or document.get("bookmakerOddsCertified") is not False
        or document.get("EV_Certified") is not False
        or not isinstance(document.get("matches"), list)):
        raise ValueError("Missing real-data research-only source contract")
    rejects = Counter()
    rows = []
    matches = []
    seen = set()
    for original in document["matches"]:
        if not isinstance(original, dict):
            continue
        match = dict(original)
        identity = (str(match.get("leagueCode") or ""),
                    str(match.get("providerEventId") or ""))
        if identity in seen:
            rejects["DUPLICATE_FIXTURE"] += 1
            continue
        seen.add(identity)
        selected = candidate(match, now=now, rejects=rejects)
        match["precomputedSelection"] = None
        if selected:
            # Only an independently verified HIGH/CONFIRMED/DEVELOPING
            # certificate may eventually be emitted. No SOLID at any layer.
            require_tier = selected.get("grade")
            if require_tier not in ("HIGH", "CONFIRMED", "DEVELOPING"):
                raise ValueError("Non-STRONG tier rejected by V2 producer")
            rows.append(selected)
            match["precomputedSelection"] = selected
        matches.append(match)
    rows.sort(key=lambda x: (-x["qualityScore"], x["kickoffUTC"],
                             x["leagueCode"], x["providerEventId"]))
    return {
        "contract": CONTRACT,
        "generatedAtUTC": now.isoformat(),
        "forecastComputedAtUTC": document.get("forecastComputedAtUTC"),
        "sourceArchive": document.get("sourceArchive"),
        "modelVersion": "statmaker-v2-shadow-goals-xg-elo",
        "dataSource": "REAL_ARCHIVED_PREMATCH_XG_ELO_AND_CANONICAL_BOOKMAKER_ODDS",
        "researchOnly": True,
        "readOnly": True,
        "certificationStatus": "BLOCKED",
        "certifiedStrong": 0,
        "bookmakerOddsCertified": False,
        "EV_Certified": False,
        "ROI_Certified": False,
        "apiCalls": 0,
        "thresholds": {
            "minOddExclusive": MIN_ODD, "maxOddExclusive": MAX_ODD,
            "minConservativeWinProbability": MIN_PROBABILITY,
            "minAdverseConservativeWinProbability": MIN_PROBABILITY,
            "conservativeEVStrictlyPositive": True,
            "allowedTiers": ["HIGH", "CONFIRMED", "DEVELOPING"],
            "independentFixtureIdRequired": True,
            "oneSelectionPerFixture": True,
        },
        "suggestionCount": len(rows),
        "realMatchedFixtureCount": sum(bool(m.get("marketEvidence")) for m in matches),
        "independentlyVerifiedFixtureCount": sum(
            bool(m.get("independentApiFootballFixtureVerified")) for m in matches
        ),
        "rejected": dict(sorted(rejects.items())),
        "suggestions": rows,
        "matches": matches,
    }


def write_output(root: Path, result: dict) -> tuple[Path, Path | None]:
    """Publish current real picks and append-only picks-only as-of ledger."""
    destination = root / DESTINATION
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                      allow_nan=False) + "\n", encoding="utf-8")
    history_file = None
    if result["suggestionCount"]:
        evidence = {
            "contract": "statmaker-v2-uat-picks-ledger-v1",
            "observedAtUTC": result["generatedAtUTC"],
            "sourceArchive": result["sourceArchive"],
            "researchOnly": True,
            "certifiedStrong": 0,
            "bookmakerPriceUpdatedAtVerified": False,
            "selectionCount": result["suggestionCount"],
            "suggestions": result["suggestions"],
        }
        content = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False) + "\n"
        digest = sha256(content.encode("utf-8")).hexdigest()[:12]
        stamp = datetime.fromisoformat(
            result["generatedAtUTC"].replace("Z", "+00:00")
        ).strftime("%Y%m%dT%H%M%S%fZ")
        folder = root / HISTORY
        folder.mkdir(parents=True, exist_ok=True)
        history_file = folder / f"{stamp}_{digest}.json"
        with history_file.open("x", encoding="utf-8") as out:
            out.write(content)
    return destination, history_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    args = parser.parse_args()
    root = args.repository_root.resolve()
    path = root / SOURCE
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Real-data source preview is missing or oversized")
    document = json.loads(path.read_text(encoding="utf-8"))
    output = compute(document, now=datetime.now(timezone.utc))
    _, immutable_history = write_output(root, output)
    print(json.dumps({
        "contract": CONTRACT,
        "realPrematchUatSuggestions": output["suggestionCount"],
        "realMatchesWithBookmakerOdds": output["realMatchedFixtureCount"],
        "independentlyVerifiedFixtures": output["independentlyVerifiedFixtureCount"],
        "rejected": output["rejected"],
        "apiCalls": 0, "certifiedStrong": 0,
        "immutableHistory": (immutable_history.relative_to(root).as_posix()
                             if immutable_history else None),
    }))


if __name__ == "__main__":
    main()
