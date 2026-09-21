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

    by_side = {}
    for row in rows:
        (
            name, team, line, side, sub_market, bm_hits, bm_sample, history_bits,
            home_team, home_hits, home_sample, home_bits,
            away_team, away_hits, away_sample, away_bits,
        ) = row
        if sub_market not in {"HOME_TEAM_TOTAL", "AWAY_TEAM_TOTAL", "TEAM_TOTAL"}:
            continue
        by_side.setdefault(side, row)
        print(
            "UAT_DIRECTION_FIXTURE",
            f"side={side}",
            f"name={name}",
            f"bm={bm_hits}/{bm_sample}",
            f"home={home_team}:{home_hits}/{home_sample}",
            f"away={away_team}:{away_hits}/{away_sample}",
            f"history={history_bits}",
        )

    over = by_side.get("OVER")
    under = by_side.get("UNDER")
    if over is None or under is None:
        raise SystemExit(f"Seattle 3.5 requires both OVER and UNDER rows; found={sorted(by_side)}")

    def complement(a: str, b: str, label: str) -> None:
        a = str(a or "")
        b = str(b or "")
        if not a or not b or len(a) != len(b):
            raise SystemExit(f"{label} outcome bits missing/incompatible: {a!r} vs {b!r}")
        if any(x == y for x, y in zip(a, b)):
            raise SystemExit(f"{label} Over/Under bits are not complementary: over={a} under={b}")

    # Half-line totals cannot push; Over and Under must be exact complements.
    complement(over[7], under[7], "combined")
    complement(over[11], under[11], "home evidence")
    complement(over[15], under[15], "away evidence")

    over_home_hits = int(over[9] or 0)
    over_home_sample = int(over[10] or 0)
    over_away_hits = int(over[13] or 0)
    over_away_sample = int(over[14] or 0)
    if over_home_sample <= 0 or over_away_sample <= 0:
        raise SystemExit("Seattle regression evidence samples are empty")
    if over_home_hits >= over_home_sample / 2 or over_away_hits >= over_away_sample / 2:
        raise SystemExit(
            "Seattle Over 3.5 still looks inverted: "
            f"home={over_home_hits}/{over_home_sample} away={over_away_hits}/{over_away_sample}"
        )

    print(
        "APP_READY_UAT_SEATTLE_DIRECTION_OK",
        f"over_home={over_home_hits}/{over_home_sample}",
        f"over_away={over_away_hits}/{over_away_sample}",
        f"under_home={int(under[9] or 0)}/{int(under[10] or 0)}",
        f"under_away={int(under[13] or 0)}/{int(under[14] or 0)}",
    )
finally:
    con.close()
