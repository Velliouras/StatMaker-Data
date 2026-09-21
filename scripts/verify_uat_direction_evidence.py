#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("usage: verify_uat_direction_evidence.py <prepared-db>")

db = Path(sys.argv[1])
if not db.is_file() or db.stat().st_size <= 16:
    raise SystemExit(f"missing prepared DB: {db}")

con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
try:
    quick = con.execute("PRAGMA quick_check").fetchone()
    if not quick or quick[0] != "ok":
        raise SystemExit(f"prepared DB quick_check failed: {quick}")

    rows = con.execute(
        """
        SELECT selection_name, selection_team, selection_line,
               identity_selection_side, identity_sub_market_key,
               bm_hits, bm_sample, historical_outcomes_bits,
               evidence_home_team, evidence_home_hits, evidence_home_sample,
               evidence_home_outcomes_bits,
               evidence_away_team, evidence_away_hits, evidence_away_sample,
               evidence_away_outcomes_bits
        FROM prepared_selections
        WHERE competition_id='domestic'
          AND ABS(COALESCE(selection_line,-999)-3.5) < 0.001
          AND LOWER(COALESCE(selection_team,''))='seattle sounders'
          AND identity_selection_side IN ('OVER','UNDER')
        ORDER BY identity_selection_side, selection_name
        """
    ).fetchall()

    if not rows:
        raise SystemExit("Seattle Sounders team-total 3.5 regression fixture is missing")

    target = None
    under_by_key = {}
    for row in rows:
        (
            name, team, line, side, sub_market, bm_hits, bm_sample, history_bits,
            home_team, home_hits, home_sample, home_bits,
            away_team, away_hits, away_sample, away_bits,
        ) = row
        if sub_market not in {"HOME_TEAM_TOTAL", "AWAY_TEAM_TOTAL", "TEAM_TOTAL"}:
            continue
        print(
            "UAT_DIRECTION_FIXTURE",
            f"side={side}",
            f"name={name}",
            f"bm={bm_hits}/{bm_sample}",
            f"home={home_team}:{home_hits}/{home_sample}",
            f"away={away_team}:{away_hits}/{away_sample}",
            f"history={history_bits}",
        )
        key=(str(home_team or ""), str(away_team or ""), float(line or 0.0))
        if side == "UNDER":
            under_by_key[key] = row
        if (
            side == "OVER"
            and str(home_team or "").lower() == "seattle sounders"
            and str(away_team or "").lower() == "real salt lake"
        ):
            target = row

    if target is None:
        raise SystemExit("Seattle Sounders vs Real Salt Lake Over 3.5 regression row is missing")

    (
        name, team, line, side, sub_market, bm_hits, bm_sample, history_bits,
        home_team, home_hits, home_sample, home_bits,
        away_team, away_hits, away_sample, away_bits,
    ) = target

    # This is the exact regression that previously displayed 19/20 + 19/20 = 38/40.
    # The canonical history for this checkpoint is 1/20 + 1/20 = 2/40.
    if (int(home_hits or 0), int(home_sample or 0)) != (1, 20):
        raise SystemExit(f"Seattle Over 3.5 evidence is wrong: {home_hits}/{home_sample}")
    if (int(away_hits or 0), int(away_sample or 0)) != (1, 20):
        raise SystemExit(f"Real Salt Lake conceded Over 3.5 evidence is wrong: {away_hits}/{away_sample}")
    if (int(bm_hits or 0), int(bm_sample or 0)) != (2, 40):
        raise SystemExit(f"Combined Seattle/RSL Over 3.5 evidence is wrong: {bm_hits}/{bm_sample}")

    # If the feed also contains the opposite side for this exact fixture/line, enforce the
    # half-line complement invariant. The opposite selection is not required to exist.
    opposite = under_by_key.get((str(home_team or ""), str(away_team or ""), float(line or 0.0)))
    if opposite is not None:
        def complement(a: str, b: str, label: str) -> None:
            a = str(a or "")
            b = str(b or "")
            if not a or not b or len(a) != len(b):
                raise SystemExit(f"{label} outcome bits missing/incompatible: {a!r} vs {b!r}")
            if any(x == y for x, y in zip(a, b)):
                raise SystemExit(f"{label} Over/Under bits are not complementary: over={a} under={b}")
        complement(history_bits, opposite[7], "combined")
        complement(home_bits, opposite[11], "home evidence")
        complement(away_bits, opposite[15], "away evidence")

    print(
        "APP_READY_UAT_SEATTLE_DIRECTION_OK",
        "over_home=1/20",
        "over_away=1/20",
        "combined=2/40",
    )
finally:
    con.close()
