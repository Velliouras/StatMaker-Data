#!/usr/bin/env python3
"""Validate prepared recommendation identity without conflating source and local dates.

The runtime candidate key is the immutable source fixture identity date|home|away. The candidate
local_date is an Athens/UI date and may legitimately differ from the source fixture date for late
kickoffs outside Europe. This validator therefore requires an exact candidate->selection->match
join, exact runtime-key/payload agreement, and a nonblank local_date, but never equality between
the two date domains. Zero provider/API calls are made.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import canonical_team_identity


def validate(path: Path) -> int:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        eligible = int(db.execute(
            "SELECT COUNT(*) FROM prepared_pattern_candidates WHERE recommendation_eligible=1"
        ).fetchone()[0])
        rows = db.execute(
            """
            SELECT c.competition_id, c.snapshot_version, c.selection_key,
                   c.match_key, c.local_date, s.match_key, m.payload
            FROM prepared_pattern_candidates c
            JOIN prepared_selections s
              ON s.competition_id=c.competition_id
             AND s.snapshot_version=c.snapshot_version
             AND s.selection_key=c.selection_key
            JOIN prepared_matches m
              ON m.competition_id=s.competition_id
             AND m.snapshot_version=s.snapshot_version
             AND m.match_key=s.match_key
            WHERE c.recommendation_eligible=1
            """
        ).fetchall()
    finally:
        db.close()

    if len(rows) != eligible:
        raise SystemExit(
            f"PREPARED_CANDIDATE_IDENTITY_JOIN_FAILED eligible={eligible} joined={len(rows)}"
        )

    bad = []
    for competition_id, snapshot_version, selection_key, candidate_key, local_date, prepared_key, raw in rows:
        try:
            payload = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError):
            bad.append(f"{competition_id}:{selection_key}:invalid_payload")
            continue
        if not isinstance(payload, dict):
            bad.append(f"{competition_id}:{selection_key}:payload_not_object")
            continue
        candidate_key = str(candidate_key or "").strip()
        local_date = str(local_date or "").strip()[:10]
        prepared_key = str(prepared_key or "").strip()
        if not prepared_key:
            bad.append(f"{competition_id}:{selection_key}:blank_prepared_match_key")
        elif not local_date:
            bad.append(f"{competition_id}:{selection_key}:blank_local_date")
        elif not canonical_team_identity.runtime_key_matches_payload(candidate_key, payload):
            bad.append(
                f"{competition_id}:{snapshot_version}:{selection_key}:runtime_key_mismatch:"
                f"candidate={candidate_key!r}"
            )

    if bad:
        raise SystemExit(
            f"PREPARED_CANDIDATE_IDENTITY_FAILED count={len(bad)} examples={' | '.join(bad[:10])}"
        )
    print("PREPARED_CANDIDATE_IDENTITY_OK", f"eligible={eligible}")
    return eligible


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prepared_db")
    args = parser.parse_args()
    validate(Path(args.prepared_db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
