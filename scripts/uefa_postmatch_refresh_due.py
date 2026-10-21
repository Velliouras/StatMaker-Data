#!/usr/bin/env python3
"""Quota-free UEFA post-match refresh gate for current 2026–27 read models.

Only published fixture schedules and already published verified data determine
whether a provider refresh is necessary.  A completed match is never inferred:
the producer must verify the actual API-Football results and goal events.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

COMPLETED = {"FT", "AET", "PEN"}
CANCELLED = {"CANC", "ABD", "AWD", "WO", "PST"}
LEAGUES = ("CL", "EL", "UECL")
COOLDOWN = timedelta(hours=2)
LOOKBACK = timedelta(hours=72)
FINALIZATION_DELAY = timedelta(hours=2)


def parse_date(value):
    try:
        x = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return x.astimezone(timezone.utc) if x.tzinfo else None
    except (ValueError, TypeError):
        return None


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def due(root: Path, target: str, at: datetime | None = None):
    now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tables_dir = root / "data/statmaker/league_tables"
    docs = {code: load(tables_dir / f"{code}.json") for code in LEAGUES}
    if any(doc.get("contract") != "statmaker-league-tables-v1" for doc in docs.values()):
        return True, "missing-uefa-tables"

    recent = []
    for code, doc in docs.items():
        for row in doc.get("fixtures") or []:
            if not isinstance(row, dict):
                continue
            date = parse_date(row.get("date_utc"))
            if not date or not now - LOOKBACK <= date <= now - FINALIZATION_DELAY:
                continue
            if str(row.get("status") or "") in CANCELLED:
                continue
            fid = row.get("fixture_id")
            if fid is None:
                continue
            recent.append((code, int(fid), str(row.get("status") or ""), date))

    if not recent:
        return False, "no-completed-kickoff-due"

    if target == "stats":
        source = load(root / "data/statmaker/uefa_current_stats.json")
        if source.get("schemaVersion") != 1 or not source.get("competitions"):
            return True, "missing-uefa-current-stats"
        completed = {
            ({"CONF": "UECL"}.get(str(c.get("leagueCode")), str(c.get("leagueCode"))), int(m.get("fixtureId")))
            for c in source.get("competitions") or [] if isinstance(c, dict)
            for m in c.get("fixtures") or [] if isinstance(m, dict) and m.get("fixtureId") is not None
            and m.get("status") in COMPLETED
        }
        missing = [(code, fid) for code, fid, _, _ in recent if (code, fid) not in completed]
        generated = parse_date(source.get("generatedAt"))
        if not missing:
            return False, "recent-final-fixtures-already-in-current-stats"
    elif target == "tables":
        missing = []
        for code, fid, status, date in recent:
            if status not in COMPLETED:
                missing.append((code, fid))
        for code, doc in docs.items():
            completed = sum(f.get("status") in COMPLETED for f in doc.get("fixtures") or [])
            played = sum(int(s.get("played") or 0) for s in doc.get("standings") or [])
            sc = doc.get("scorers") or {}
            if completed and (played < completed * 2 or
                              int(sc.get("verifiedFixtures") or 0) < completed):
                missing.append((code, "standings-or-scorers"))
        stamps = [parse_date(doc.get("generatedAtUTC")) for doc in docs.values()]
        generated = min(stamps) if all(stamps) else None
        if not missing:
            return False, "recent-uefa-results-standings-scorers-complete"
    else:
        raise ValueError("target must be stats or tables")

    if generated is not None and now - generated < COOLDOWN:
        return False, "refresh-cooldown-2h"
    return True, "incomplete-uefa-fixtures:" + ",".join(str(x[0]) for x in missing[:8])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["stats", "tables"], required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    needed, reason = due(args.root, args.target)
    print("refresh_needed=" + str(needed).lower())
    print("refresh_reason=" + reason)


if __name__ == "__main__":
    main()
