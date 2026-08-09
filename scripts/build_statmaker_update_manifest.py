#!/usr/bin/env python3
"""Build deterministic StatMaker update manifests without extra API calls.

Two profiles match the two live data branches:
- main: Domestic odds, history, readiness, support, aliases and logos.
- uefa: Champions League, Europa League and Conference League odds.

For the large normalized Domestic statistics artifact, the main manifest also carries
an optional bounded old-hash -> new-hash delta chain. Existing installs can update
their compact local snapshot without downloading/parsing the full multi-megabyte JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_REPOSITORY = "Velliouras/StatMaker-Data"
NORMALIZED_STATS_ARTIFACT_ID = "domestic_normalized_fixture_stats"
NORMALIZED_STATS_TRANSITION_RELATIVE_PATH = ".git/statmaker_normalized_stats_transition.json"
MAX_NORMALIZED_STATS_TRANSITIONS = 8
MAX_NORMALIZED_STATS_DELTA_BYTES = 1_500_000


@dataclass(frozen=True)
class ArtifactSpec:
    artifact_id: str
    path: str
    group: str
    required: bool = False


MAIN_ARTIFACT_SPECS: tuple[ArtifactSpec, ...] = (
    ArtifactSpec("domestic_odds", "odds/odds_api_io/domestic_odds.json", "odds", required=True),
    ArtifactSpec("domestic_enriched_index", "data/statmaker/domestic_enriched/index.json", "history"),
    ArtifactSpec(
        NORMALIZED_STATS_ARTIFACT_ID,
        "data/api_football/domestic_normalized_fixture_stats.json",
        "history",
    ),
    ArtifactSpec("domestic_proposal_readiness", "data/statmaker/domestic_proposal_readiness.json", "readiness"),
    ArtifactSpec("uefa_support_history", "data/statmaker/uefa_support_history.json", "support"),
    ArtifactSpec("uefa_team_support_history", "data/statmaker/uefa_team_support_history.json", "support"),
    ArtifactSpec("uefa_team_logos", "data/statmaker/uefa_team_logos.json", "visual"),
    ArtifactSpec("domestic_team_aliases", "mappings/domestic_team_aliases.json", "identity"),
)

UEFA_ARTIFACT_SPECS: tuple[ArtifactSpec, ...] = (
    ArtifactSpec("champions_league_odds", "odds/odds_api_io/champions_league_odds.json", "odds", required=True),
    ArtifactSpec("europa_league_odds", "odds/odds_api_io/europa_league_odds.json", "odds", required=True),
    ArtifactSpec("conference_league_odds", "odds/odds_api_io/conference_league_odds.json", "odds", required=True),
)

PROFILES = {
    "main": MAIN_ARTIFACT_SPECS,
    "uefa": UEFA_ARTIFACT_SPECS,
}

_TIMESTAMP_KEYS = (
    "generatedAt",
    "generated_at",
    "updatedAt",
    "updated_at",
    "lastUpdated",
    "last_updated",
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _timestamp(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in _TIMESTAMP_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in _TIMESTAMP_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_url(repository: str, branch: str, relative_path: str) -> str:
    return f"https://raw.githubusercontent.com/{repository}/{branch}/{relative_path}"


def build_manifest(
    root: Path,
    branch: str,
    repository: str = DEFAULT_REPOSITORY,
    specs: Iterable[ArtifactSpec] = MAIN_ARTIFACT_SPECS,
    profile: str = "main",
) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    missing_required: list[str] = []

    for spec in specs:
        path = root / spec.path
        if not path.is_file():
            if spec.required:
                missing_required.append(spec.path)
            continue
        payload = _read_json(path)
        artifacts.append(
            {
                "id": spec.artifact_id,
                "group": spec.group,
                "path": spec.path,
                "url": _raw_url(repository, branch, spec.path),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
                "generatedAt": _timestamp(payload),
            }
        )

    if missing_required:
        raise FileNotFoundError("Missing required artifacts: " + ", ".join(sorted(missing_required)))

    artifacts.sort(key=lambda item: item["id"])
    version_payload = json.dumps(
        [
            {
                "id": item["id"],
                "sha256": item["sha256"],
                "bytes": item["bytes"],
                "generatedAt": item["generatedAt"],
            }
            for item in artifacts
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    content_version = hashlib.sha256(version_payload).hexdigest()
    generated_candidates = sorted(
        (item["generatedAt"] for item in artifacts if item["generatedAt"]),
        reverse=True,
    )

    return {
        "schemaVersion": 2,
        "profile": profile,
        "repository": repository,
        "branch": branch,
        "contentVersion": content_version,
        "generatedAt": generated_candidates[0] if generated_candidates else "",
        "artifactCount": len(artifacts),
        "artifacts": artifacts,
    }


def _artifact_hash(manifest: dict[str, Any], artifact_id: str) -> str:
    for item in manifest.get("artifacts", []) or []:
        if isinstance(item, dict) and item.get("id") == artifact_id:
            return str(item.get("sha256") or "").strip()
    return ""


def _valid_transition(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    from_hash = str(value.get("fromSha256") or "").strip()
    to_hash = str(value.get("toSha256") or "").strip()
    if not from_hash or not to_hash or from_hash == to_hash:
        return None
    removed = value.get("removedFixtures")
    upserts = value.get("upserts")
    if not isinstance(removed, list) or not isinstance(upserts, list):
        return None
    return {
        "fromSha256": from_hash,
        "toSha256": to_hash,
        "removedFixtures": removed,
        "upserts": upserts,
    }


def _previous_transitions(previous_manifest: Any) -> list[dict[str, Any]]:
    if not isinstance(previous_manifest, dict):
        return []
    root = previous_manifest.get("normalizedStatsDelta")
    if not isinstance(root, dict):
        return []
    if root.get("targetArtifactId") != NORMALIZED_STATS_ARTIFACT_ID:
        return []
    return [
        valid for value in (root.get("transitions") or [])
        if (valid := _valid_transition(value)) is not None
    ]


def _connected_suffix(transitions: list[dict[str, Any]], target_hash: str) -> list[dict[str, Any]]:
    if not target_hash:
        return []
    by_to: dict[str, dict[str, Any]] = {}
    for transition in transitions:
        by_to[transition["toSha256"]] = transition
    result: list[dict[str, Any]] = []
    current = target_hash
    seen: set[str] = set()
    while current not in seen and current in by_to:
        seen.add(current)
        transition = by_to[current]
        result.append(transition)
        current = transition["fromSha256"]
    result.reverse()
    return result[-MAX_NORMALIZED_STATS_TRANSITIONS:]


def _bounded_transitions(transitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bounded = list(transitions[-MAX_NORMALIZED_STATS_TRANSITIONS:])
    while bounded:
        payload = json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) <= MAX_NORMALIZED_STATS_DELTA_BYTES:
            return bounded
        bounded.pop(0)
    return []


def attach_normalized_stats_delta(
    root: Path,
    manifest: dict[str, Any],
    previous_manifest: Any,
) -> None:
    if manifest.get("profile") != "main":
        return
    target_hash = _artifact_hash(manifest, NORMALIZED_STATS_ARTIFACT_ID)
    if not target_hash:
        return

    candidates = _previous_transitions(previous_manifest)
    transition_path = root / NORMALIZED_STATS_TRANSITION_RELATIVE_PATH
    if transition_path.is_file():
        try:
            current = _valid_transition(_read_json(transition_path))
        except (OSError, ValueError, json.JSONDecodeError):
            current = None
        if current is not None and current["toSha256"] == target_hash:
            candidates.append(current)

    chain = _bounded_transitions(_connected_suffix(candidates, target_hash))
    if chain:
        manifest["normalizedStatsDelta"] = {
            "schemaVersion": 1,
            "targetArtifactId": NORMALIZED_STATS_ARTIFACT_ID,
            "targetSha256": target_hash,
            "transitions": chain,
        }


def write_manifest(path: Path, manifest: dict[str, Any]) -> bool:
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    if previous == rendered:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="main")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--output", default="data/statmaker/update_manifest.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    output = root / args.output
    previous_manifest = _read_json(output) if output.is_file() else {}
    specs = PROFILES[args.profile]
    manifest = build_manifest(
        root=root,
        branch=args.branch,
        repository=args.repository,
        specs=specs,
        profile=args.profile,
    )
    attach_normalized_stats_delta(root, manifest, previous_manifest)
    changed = write_manifest(output, manifest)
    transition_path = root / NORMALIZED_STATS_TRANSITION_RELATIVE_PATH
    transition_path.unlink(missing_ok=True)
    print(
        f"StatMaker {args.profile} update manifest {'updated' if changed else 'unchanged'}: "
        f"{manifest['artifactCount']} artifacts, version={manifest['contentVersion'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
