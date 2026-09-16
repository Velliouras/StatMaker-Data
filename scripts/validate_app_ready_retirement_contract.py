#!/usr/bin/env python3
import argparse
import sqlite3
from pathlib import Path

ASIAN_COUNTRIES = (
    "CHINA",
    "JAPAN",
    "SAUDI ARABIA",
    "UNITED ARAB EMIRATES",
    "SOUTH KOREA",
    "KOREA REPUBLIC",
    "REPUBLIC OF KOREA",
)


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def scalar(con: sqlite3.Connection, sql: str, params=()):
    row = con.execute(sql, params).fetchone()
    return row[0] if row else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    args = parser.parse_args()

    db = Path(args.database)
    if not db.is_file() or db.stat().st_size <= 0:
        raise SystemExit(f"Missing prepared database: {db}")

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        quick = scalar(con, "PRAGMA quick_check")
        if quick != "ok":
            raise SystemExit(f"Prepared database quick_check failed: {quick}")

        required = {
            "prepared_snapshot_meta",
            "prepared_matches",
            "prepared_selections",
            "prepared_pattern_candidates",
        }
        missing = sorted(name for name in required if not table_exists(con, name))
        if missing:
            raise SystemExit(f"Prepared database missing tables: {missing}")

        placeholders = ",".join("?" for _ in ASIAN_COUNTRIES)
        asian_candidates = int(scalar(
            con,
            f"""
            SELECT COUNT(*)
            FROM prepared_pattern_candidates
            WHERE UPPER(TRIM(continent))='ASIA'
               OR UPPER(TRIM(country)) IN ({placeholders})
            """,
            ASIAN_COUNTRIES,
        ) or 0)
        if asian_candidates:
            raise SystemExit(
                f"Generated recommendations contain retired Asian scope rows: {asian_candidates}"
            )

        retired_candidate_markets = int(scalar(
            con,
            """
            SELECT COUNT(*)
            FROM prepared_pattern_candidates
            WHERE UPPER(REPLACE(market_family,' ','_')) LIKE '%ASIAN%'
               OR UPPER(REPLACE(market_family,' ','_')) LIKE '%HANDICAP%'
            """,
        ) or 0)
        if retired_candidate_markets:
            raise SystemExit(
                "Generated recommendations contain retired Asian/handicap market rows: "
                f"{retired_candidate_markets}"
            )

        retired_selection_markets = int(scalar(
            con,
            """
            SELECT COUNT(*)
            FROM prepared_selections
            WHERE UPPER(REPLACE(selection_market,' ','_')) LIKE '%ASIAN%'
               OR UPPER(REPLACE(selection_market,' ','_')) LIKE '%HANDICAP%'
               OR UPPER(REPLACE(market_key,' ','_')) LIKE '%ASIAN%'
               OR UPPER(REPLACE(market_key,' ','_')) LIKE '%HANDICAP%'
            """,
        ) or 0)
        if retired_selection_markets:
            raise SystemExit(
                "Prepared selections contain retired Asian/handicap market rows: "
                f"{retired_selection_markets}"
            )

        domestic_ready = con.execute(
            """
            SELECT snapshot_version, match_count, selection_count
            FROM prepared_snapshot_meta
            WHERE competition_id='domestic' AND state='ready'
            ORDER BY built_at_ms DESC
            LIMIT 1
            """
        ).fetchone()
        if not domestic_ready:
            raise SystemExit("No READY Domestic prepared snapshot")
        snapshot_version, match_count, selection_count = domestic_ready
        if int(match_count or 0) <= 0 or int(selection_count or 0) <= 0:
            raise SystemExit(
                f"Domestic prepared snapshot is empty: matches={match_count} selections={selection_count}"
            )

        dates = con.execute(
            """
            SELECT MIN(local_date), MAX(local_date), COUNT(*)
            FROM prepared_matches
            WHERE competition_id='domestic' AND snapshot_version=?
            """,
            (snapshot_version,),
        ).fetchone()
        date_min, date_max, row_count = dates or ("", "", 0)

        print(
            "APP_READY_RETIREMENT_POSTFLIGHT_OK",
            f"domestic_matches={int(match_count)}",
            f"domestic_selections={int(selection_count)}",
            f"date_min={date_min or ''}",
            f"date_max={date_max or ''}",
            f"match_rows={int(row_count or 0)}",
        )
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
