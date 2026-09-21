#!/usr/bin/env python3
import argparse
import sqlite3
from pathlib import Path

def table_exists(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None

def scalar(con, sql, params=()):
    row=con.execute(sql,params).fetchone()
    return row[0] if row else None

def main():
    p=argparse.ArgumentParser(); p.add_argument("database"); args=p.parse_args()
    db=Path(args.database)
    if not db.is_file() or db.stat().st_size<=0: raise SystemExit(f"Missing prepared database: {db}")
    con=sqlite3.connect(f"file:{db}?mode=ro",uri=True)
    try:
        if scalar(con,"PRAGMA quick_check")!="ok": raise SystemExit("Prepared database quick_check failed")
        required={"prepared_snapshot_meta","prepared_matches","prepared_selections","prepared_pattern_candidates"}
        missing=sorted(x for x in required if not table_exists(con,x))
        if missing: raise SystemExit(f"Prepared database missing tables: {missing}")
        retired_candidates=int(scalar(con,"""
            SELECT COUNT(*) FROM prepared_pattern_candidates
            WHERE UPPER(REPLACE(market_family,' ','_')) LIKE '%ASIAN%'
               OR UPPER(REPLACE(market_family,' ','_')) LIKE '%HANDICAP%'
        """) or 0)
        if retired_candidates:
            raise SystemExit(f"Generated recommendations contain retired Asian/handicap market rows: {retired_candidates}")
        retired_selections=int(scalar(con,"""
            SELECT COUNT(*) FROM prepared_selections
            WHERE UPPER(REPLACE(selection_market,' ','_')) LIKE '%ASIAN%'
               OR UPPER(REPLACE(selection_market,' ','_')) LIKE '%HANDICAP%'
               OR UPPER(REPLACE(market_key,' ','_')) LIKE '%ASIAN%'
               OR UPPER(REPLACE(market_key,' ','_')) LIKE '%HANDICAP%'
        """) or 0)
        if retired_selections:
            raise SystemExit(f"Prepared selections contain retired Asian/handicap market rows: {retired_selections}")
        asian_candidates=int(scalar(con,"""
            SELECT COUNT(*) FROM prepared_pattern_candidates
            WHERE UPPER(TRIM(continent))='ASIA'
        """) or 0)
        ready=con.execute("""
            SELECT snapshot_version,match_count,selection_count
            FROM prepared_snapshot_meta WHERE competition_id='domestic' AND state='ready'
            ORDER BY built_at_ms DESC LIMIT 1
        """).fetchone()
        if not ready: raise SystemExit("No READY Domestic prepared snapshot")
        snapshot_version,match_count,selection_count=ready
        dates=con.execute("""
            SELECT MIN(local_date),MAX(local_date),COUNT(*) FROM prepared_matches
            WHERE competition_id='domestic' AND snapshot_version=?
        """,(snapshot_version,)).fetchone()
        print("APP_READY_RETIREMENT_POSTFLIGHT_OK","retired_markets=0",
              f"asian_candidates_preserved={asian_candidates}",
              f"domestic_matches={int(match_count)}",f"domestic_selections={int(selection_count)}",
              f"date_min={dates[0] or ''}",f"date_max={dates[1] or ''}",f"match_rows={int(dates[2] or 0)}")
        return 0
    finally:
        con.close()

if __name__=="__main__":
    raise SystemExit(main())
