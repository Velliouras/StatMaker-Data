#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "domestic_leagues.json"
ALIASES = ROOT / "mappings" / "domestic_team_aliases.json"
AUDIT = ROOT / "reports" / "domestic_mapping_audit.json"
REPORT = ROOT / "reports" / "domestic_mapping_apply.json"

# Verified against current Odds-API.io provider catalog/audit. Ambiguous leagues are
# deliberately omitted until a unique provider competition is confirmed.
VERIFIED_SLUGS = {
    "ARG": "argentina-primera-lpf-clausura",
    "BRA": "brazil-brasileiro-serie-a",
    "IRL": "ireland-premier-division",
    "USA": "usa-mls",
    "CHN": "china-chinese-super-league",
    "NOR": "norway-eliteserien",
    "BRA2": "brazil-brasileiro-serie-b",
    "SWE2": "sweden-superettan",
    "FIN": "finland-veikkausliiga",
    "SWE": "sweden-allsvenskan",
    "MEX": "mexico-liga-mx-apertura",
    "ROM": "romania-superliga",
    "DNK": "denmark-superligaen",
    "POL": "poland-ekstraklasa",
    "RUS": "russia-premier-league",
    "SWZ": "switzerland-super-league",
    "AUT2": "austria-2-liga",
    "AUT": "austria-bundesliga",
    "SC0": "scotland-premiership",
    "SRB": "serbia-super-liga",
    "BGR": "bulgaria-parva-liga",
    "SVN": "slovenia-prvaliga",
    "CZE": "czechia-1-liga",
    "SVK": "slovakia-superliga",
    "LVA": "latvia-virsliga",
    "LTU": "lithuania-a-lyga",
    "FIN2": "finland-ykkosliiga",
}

# Team aliases from an audit are only safe when that audit used the verified league.
TEAM_ALIAS_ALLOWED_CODES = set(VERIFIED_SLUGS) - {"ARG", "BRA"}


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    config = load(CONFIG, {})
    aliases_payload = load(ALIASES, {"version": 1, "aliases": {}})
    audit = load(AUDIT, {})

    league_changes = []
    for row in config.get("leagues", []) or []:
        code = str(row.get("leagueCode") or "")
        slug = VERIFIED_SLUGS.get(code)
        if not slug:
            continue
        before = row.get("providerLeagueSlug")
        if before != slug:
            row["providerLeagueSlug"] = slug
            league_changes.append({"leagueCode": code, "before": before, "after": slug})

    alias_root: Dict[str, Dict[str, list]] = aliases_payload.setdefault("aliases", {})
    alias_additions = []
    audit_slug_by_code = {
        str(row.get("leagueCode") or ""): str((row.get("bestCandidate") or {}).get("slug") or "")
        for row in audit.get("leagueMappings", []) or []
    }

    for league in audit.get("teamMappings", []) or []:
        code = str(league.get("leagueCode") or "")
        if code not in TEAM_ALIAS_ALLOWED_CODES:
            continue
        expected_slug = VERIFIED_SLUGS.get(code)
        audited_slug = str(league.get("providerLeagueSlug") or "")
        if audited_slug != expected_slug or audit_slug_by_code.get(code) != expected_slug:
            continue
        bucket = alias_root.setdefault(code, {})
        for item in league.get("mapped", []) or []:
            if item.get("status") not in {"exact-normalized", "high-confidence-suggestion"}:
                continue
            provider = str(item.get("providerTeam") or "").strip()
            canonical = str(item.get("canonicalTeam") or "").strip()
            if not provider or not canonical or provider == canonical:
                continue
            values = bucket.setdefault(canonical, [])
            if provider not in values:
                values.append(provider)
                alias_additions.append({
                    "leagueCode": code,
                    "canonicalTeam": canonical,
                    "providerAlias": provider,
                    "source": item.get("status"),
                })

    if league_changes:
        config["version"] = max(int(config.get("version") or 1), 6)
    if alias_additions:
        aliases_payload["version"] = max(int(aliases_payload.get("version") or 1), 6)

    save(CONFIG, config)
    save(ALIASES, aliases_payload)
    save(REPORT, {
        "mode": "verified-domestic-mapping-apply",
        "bettingEngineTouched": False,
        "oddsFeedTouched": False,
        "verifiedLeagueSlugCount": len(VERIFIED_SLUGS),
        "leagueChanges": league_changes,
        "teamAliasAdditions": alias_additions,
        "deliberatelyUnresolvedLeagueCodes": ["HUN", "ISL", "EST", "NOR2"],
    })
    print(json.dumps({
        "leagueChanges": len(league_changes),
        "teamAliasAdditions": len(alias_additions),
        "unresolved": ["HUN", "ISL", "EST", "NOR2"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
