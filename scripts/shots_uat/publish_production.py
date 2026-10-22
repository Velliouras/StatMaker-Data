"""Publish the already issued model feed for production without recomputing picks."""
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path("data/statmaker/uat/shots_v1/p55")
TARGET = Path("data/statmaker/betting_v1")


def publish(root=ROOT):
    source = root / SOURCE
    index = json.loads((source / "latest.json").read_text())
    if index.get("schemaVersion") != 1 or index.get("channel") != "uat":
        raise ValueError("Invalid source channel")
    artifact = index.get("artifact", "")
    sha = index.get("sha256", "")
    if not re.fullmatch(r"candidates-[a-f0-9]{64}\.json", artifact) or artifact != f"candidates-{sha}.json":
        raise ValueError("Invalid source artifact")
    content = (source / artifact).read_bytes()
    if hashlib.sha256(content).hexdigest() != sha:
        raise ValueError("Source SHA mismatch")
    payload = json.loads(content)
    if (payload.get("schemaVersion") != 1 or payload.get("status") != "UAT_SHOTS_MODEL"
            or payload.get("apiCalls") != 0 or payload.get("prodWrites") != 0):
        raise ValueError("Invalid model contract")
    if any(payload.get(k) != index.get(k) for k in ("generationId", "generatedAt")):
        raise ValueError("Source generation mismatch")
    issued = payload.get("issued", []) + payload.get("marketIssued", [])
    by_id = {row["selectionId"]: row for row in issued}
    if len(by_id) != len(issued):
        raise ValueError("Duplicate issue identity")
    for row in issued:
        if (not re.fullmatch(r"[a-f0-9]{40}", row.get("publicationCommit", ""))
                or datetime.fromisoformat(row["publishedAt"]) >= datetime.fromisoformat(row["kickoff"])):
            raise ValueError("Issue lacks pre-kickoff publication")
    for key, count_key in (("candidates", "candidateCount"), ("marketCandidates", "marketCandidateCount")):
        rows = payload.get(key, [])
        if len(rows) != payload.get(count_key, 0) or any(by_id.get(r["selectionId"]) != r for r in rows):
            raise ValueError("Candidates are not exact published issues")
    target = root / TARGET
    target.mkdir(parents=True, exist_ok=True)
    destination = target / artifact
    if destination.exists() and destination.read_bytes() != content:
        raise ValueError("Existing production artifact changed")
    staged = target / "candidates.pending"
    staged.write_bytes(content)
    os.replace(staged, destination)
    production = dict(index, channel="prod", sourceChannel="uat",
                      sourceArtifact=str(SOURCE / artifact))
    staged_index = target / "latest.pending"
    staged_index.write_text(json.dumps(production, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    os.replace(staged_index, target / "latest.json")
    return production


if __name__ == "__main__":
    result = publish()
    print("PRODUCTION_SINGLES_PUBLISHED", result["generationId"], result["sha256"])
