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


def watchdog_tables_due(root, docs, now):
    """Retry verified result/scorer gaps even when older than the cron lookback."""
    current = load(root / "data/statmaker/uefa_current_stats.json")
    phases = {{"CONF": "UECL"}.get(c.get("leagueCode"), c.get("leagueCode")):
              str(c.get("phaseStart") or str(c.get("season")) + "-09-01")
              for c in current.get("competitions", [])}
    missing = []
    for code, doc in docs.items():
        completed = {int(f["fixture_id"]): f for f in doc.get("fixtures", [])
                     if f.get("status") in COMPLETED}
        fast = load(root / "data/statmaker/priority_refresh" / (code + ".json"))
        valid_fast = (fast.get("contract") == "statmaker-league-tables-v1" and
                      fast.get("leagueCode") == code and fast.get("season") == doc.get("season") and
                      fast.get("apiFootballLeagueId") == doc.get("apiFootballLeagueId"))
        if valid_fast:
            for f in fast.get("fixtures", []):
                stamp = parse_date(f.get("date_utc"))
                if (f.get("status") not in COMPLETED or stamp is None or
                        stamp > now or stamp.date().isoformat() < phases.get(code, str(doc.get("season")) + "-09-01")):
                    continue
                if any(type(f.get(k)) is not int or f[k] < 0 for k in ("fixture_id", "home_goals", "away_goals")):
                    continue
                old = completed.get(f["fixture_id"])
                if old is None or any(old.get(k) != f[k] for k in ("home_goals", "away_goals")):
                    missing.append(code)
        if int((doc.get("scorers") or {}).get("verifiedFixtures") or 0) < len(completed):
            missing.append(code)
    if not missing:
        return False, "uefa-scorer-events-complete"
    stamps = [parse_date(doc.get("generatedAtUTC")) for doc in docs.values()]
    if all(stamps) and all(int(stamp.timestamp()) // 1800 == int(now.timestamp()) // 1800 for stamp in stamps):
        return False, "uefa-scorer-half-hour-cooldown"
    return True, "incomplete-uefa-scorers:" + ",".join(sorted(set(missing)))


def due(root: Path, target: str, at: datetime | None = None, watchdog=False):
    now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tables_dir = root / "data/statmaker/league_tables"
    docs = {code: load(tables_dir / f"{code}.json") for code in LEAGUES}
    if any(doc.get("contract") != "statmaker-league-tables-v1" for doc in docs.values()):
        return True, "missing-uefa-tables"
    if watchdog and target == "tables":
        return watchdog_tables_due(root, docs, now)

    recent = []
    delay = timedelta(minutes=100) if target == "stats" else FINALIZATION_DELAY
    for code, doc in docs.items():
        for row in doc.get("fixtures") or []:
            if not isinstance(row, dict):
                continue
            date = parse_date(row.get("date_utc"))
            if not date or not now - LOOKBACK <= date <= now - delay:
                continue
            if str(row.get("status") or "") in CANCELLED:
                continue
            fid = row.get("fixture_id")
            if fid is None:
                continue
            recent.append((code, int(fid), str(row.get("status") or ""), date))

    if target == "stats":
        # The fast results producer often knows the final score before UEFA
        # League Tables has reconciled its older fixture snapshots.
        # Only trust final, verified season/fixture identities.
        for code, doc in docs.items():
            fast = load(root / "data/statmaker/priority_refresh" / (code + ".json"))
            if (fast.get("contract") != "statmaker-league-tables-v1"
                    or fast.get("leagueCode") != code
                    or fast.get("season") != doc.get("season")
                    or fast.get("apiFootballLeagueId") != doc.get("apiFootballLeagueId")):
                continue
            for row in fast.get("fixtures", []):
                if row.get("status") not in COMPLETED:
                    continue
                date = parse_date(row.get("date_utc"))
                if date is None or not now - LOOKBACK <= date <= now - delay:
                    continue
                if any(type(row.get(key)) is not int for key in ("fixture_id", "home_goals", "away_goals")):
                    continue
                recent.append((code, row["fixture_id"], row["status"], date))

    if not recent:
        return False, "no-completed-kickoff-due"

    # One final match can appear in both tables and priority snapshots.
    recent = list({(code, fid): (code, fid, status, date)
                   for code, fid, status, date in recent}.values())

    if target == "stats":
        source = load(root / "data/statmaker/uefa_current_stats.json")
        if source.get("schemaVersion") != 1 or not source.get("competitions"):
            return True, "missing-uefa-current-stats"
        completed = {
            ({"CONF": "UECL"}.get(str(c.get("leagueCode")), str(c.get("leagueCode"))), int(m.get("fixtureId")))
            for c in source.get("competitions") or [] if isinstance(c, dict)
            for m in c.get("fixtures") or [] if isinstance(m, dict) and m.get("fixtureId") is not None
            and m.get("status") in COMPLETED and m.get("statsFetched") is True
            and isinstance(m.get("normalizedStats"), dict)
            and any(value is not None for value in m["normalizedStats"].values())
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

    cooldown = timedelta(minutes=15) if target == "stats" else COOLDOWN
    if generated is not None and now - generated < cooldown:
        return False, "refresh-cooldown"
    return True, "incomplete-uefa-fixtures:" + ",".join(str(x[0]) for x in missing[:8])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["stats", "tables"], required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--watchdog", action="store_true")
    args = parser.parse_args()
    needed, reason = due(args.root, args.target, watchdog=args.watchdog)
    print("refresh_needed=" + str(needed).lower())
    print("refresh_reason=" + reason)


if __name__ == "__main__":
    main()
