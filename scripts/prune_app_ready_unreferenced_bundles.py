#!/usr/bin/env python3
"""Bound published App-Ready ZIPs to current and immediately previous manifests.

The app requires the immutable ZIP URLs in its manifest. Never delete those
referenced bundles, nor the preceding manifest's files still potentially
being downloaded by existing clients. Only remove unreferenced ZIPs from
the current Git tree; this does not rewrite or shrink Git history.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

BUNDLE = re.compile(r"app_ready_(?:stats|betting)_bundle-[0-9a-f]{64}\.zip\Z")
IDS = {"app_ready_stats_bundle", "app_ready_betting_bundle"}


def referenced(path: Path, required: bool) -> set[str]:
    if not path.is_file():
        if required:
            raise ValueError(f"Missing active manifest: {path}")
        return set()
    root = json.loads(path.read_text(encoding="utf-8"))
    artifacts = root.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError(f"Malformed App-Ready manifest: {path}")
    found: dict[str, str] = {}
    for a in artifacts:
        if not isinstance(a, dict) or a.get("id") not in IDS:
            continue
        value = str(a.get("path") or "")
        name = Path(value).name
        if not BUNDLE.fullmatch(name) or value != "data/statmaker/app_ready/" + name:
            raise ValueError(f"Unsafe App-Ready bundle reference: {value}")
        found[a["id"]] = name
    if required and set(found) != IDS:
        raise ValueError(f"Missing required App-Ready bundles in {path}")
    return set(found.values())


def prune(folder: Path, previous: Path | None) -> dict[str, int]:
    current = referenced(folder / "update_manifest.json", required=True)
    older = referenced(previous, required=False) if previous is not None else set()
    keep = current | older
    for name in current:
        if not (folder / name).is_file():
            raise ValueError(f"Active App-Ready bundle missing: {name}")
    removed = 0
    reclaimed = 0
    for file in folder.iterdir():
        if file.is_file() and BUNDLE.fullmatch(file.name) and file.name not in keep:
            reclaimed += file.stat().st_size
            file.unlink()
            removed += 1
    return {"removedBundles": removed, "removedBytesFromWorkingTree": reclaimed,
            "retainedBundles": len(keep)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path,
                        default=Path("data/statmaker/app_ready"))
    parser.add_argument("--previous-manifest", type=Path, required=True)
    args = parser.parse_args()
    print("APP_READY_RETENTION", json.dumps(
        prune(args.directory, args.previous_manifest), sort_keys=True))


if __name__ == "__main__":
    main()
