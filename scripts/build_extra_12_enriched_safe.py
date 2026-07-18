#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

import build_statmaker_domestic_enriched as build

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "api_football_enrichment_leagues.json"
INDEX = ROOT / "data" / "statmaker" / "domestic_enriched" / "index.json"
REPORT = ROOT / "reports" / "domestic_31_extra_enriched_safe.json"
EXTRA_CODES = {"SRB","BGR","SVN","HUN","CZE","SVK","ISL","LVA","LTU","EST","FIN2","NOR2"}
BASE_19 = {"ARG","BRA","IRL","USA","CHN","NOR","BRA2","SWE2","FIN","SWE","MEX","ROM","DNK","POL","RUS","SWZ","AUT2","AUT","SC0"}


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    cfg = load(CONFIG, {})
    selected = [x for x in cfg.get("leagues", []) or [] if str(x.get("leagueCode") or "") in EXTRA_CODES]
    if {str(x.get("leagueCode") or "") for x in selected} != EXTRA_CODES:
        raise SystemExit("Extra 12 enrichment config is incomplete")

    original_index = load(INDEX, {})
    original_rows = list(original_index.get("leagues", []) or [])
    original_by_code = {str(x.get("league_code") or ""): copy.deepcopy(x) for x in original_rows}
    missing_base = sorted(BASE_19 - set(original_by_code))
    if missing_base:
        raise SystemExit(f"Refusing to modify index: missing existing base leagues {missing_base}")

    new_rows: List[Dict[str, Any]] = []
    for league in selected:
        artifact, row = build.build_league_artifact(league, min_fixtures=15, min_coverage=0.65)
        output_path = build.output_path_for(league)
        build.write_json(output_path, artifact)
        new_rows.append(row)

    merged_by_code = {str(x.get("league_code") or ""): x for x in original_rows}
    for row in new_rows:
        merged_by_code[str(row.get("league_code") or "")] = row

    for code in BASE_19:
        if merged_by_code.get(code) != original_by_code.get(code):
            raise SystemExit(f"Guard failed: existing enriched index row changed: {code}")

    merged_rows = sorted(merged_by_code.values(), key=lambda r: (str(r.get("country")), str(r.get("league")), str(r.get("league_code"))))
    output_index = dict(original_index)
    output_index["generated_at"] = build.now_utc()
    output_index["league_count"] = len(merged_rows)
    output_index["leagues"] = merged_rows
    save(INDEX, output_index)
    save(REPORT, {
        "mode": "extra-12-only-safe",
        "base19Preserved": sorted(BASE_19),
        "extraRows": new_rows,
        "finalIndexLeagueCount": len(merged_rows),
    })
    print(json.dumps({"finalIndexLeagueCount": len(merged_rows), "extraRows": new_rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
