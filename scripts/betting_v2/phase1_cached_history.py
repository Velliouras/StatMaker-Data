"""Phase 1: offline API-Football history/identity bridge for Betting V2.

Combines the existing domestic_enriched completed history with *cached* provider
fixtures (including older seasons not referenced in the enriched index). Uses
verified API league/team/fixture IDs when available; never generates odds, makes
HTTP calls, guesses aliases, or uses future results as features.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

from fixture_lookup import as_utc, canonical_name

STAT_FIELDS = ("HS", "AS", "HST", "AST", "HC", "AC", "HY", "AY", "HxG", "AxG")
ROOT = Path("data/api_football/fixture_stats")
FINAL = frozenset({"FT"})
MAX_CACHE_BYTES = 55_000_000
# This provider uses ROU; published Romanian domestic scope uses ROM.
VERIFIED_LEAGUE_CODE_ALIASES = {"ROU": "ROM"}


def team_key(league_id: str, team_id: object) -> str:
    return f"api-team:{league_id}:{team_id}"


def _id(value: object) -> str | None:
    s = str(value or "").strip()
    return s if s and s != "None" else None


def _teams(row: dict, scheduled: dict | None) -> tuple[str, str] | None:
    home = _id((scheduled or {}).get("home_team_id") or row.get("home_team_id"))
    away = _id((scheduled or {}).get("away_team_id") or row.get("away_team_id"))
    if home and away and home != away:
        return home, away
    # Historic raw rows can lack IDs. Verify two team-name/ID matches in the
    # statistics response rather than assuming its entries are ordered.
    matches: dict[str, set[str]] = defaultdict(set)
    for item in row.get("raw_statistics") or []:
        if not isinstance(item, dict):
            continue
        team = item.get("team") or {}
        if isinstance(team, dict) and _id(team.get("id")):
            matches[canonical_name(team.get("name"))].add(str(team["id"]))
    home_ids = matches.get(canonical_name(row.get("home_team")), set())
    away_ids = matches.get(canonical_name(row.get("away_team")), set())
    if len(home_ids) == len(away_ids) == 1 and home_ids != away_ids:
        return next(iter(home_ids)), next(iter(away_ids))
    return None


def _score(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return int(n) if n >= 0 and n.is_integer() else None
    except (TypeError, ValueError, OverflowError):
        return None


class CachedHistoryBridge:
    """Safe overlay of provider-ID features; local diagnostics only."""

    def __init__(self, root: Path, history: list[dict], groups: dict):
        self.root = root.resolve()
        self.stats = Counter()
        self.groups = groups
        self.source_paths: list[dict] = []
        self.schedule: dict[str, list[dict]] = defaultdict(list)
        self.raw_history: dict[tuple[str, str], dict] = {}
        self.history = list(history)
        self.aliases: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self._load_verified_aliases()
        self._build()

    def _load_verified_aliases(self) -> None:
        """Use existing explicit domestic team aliases, never heuristic fuzziness."""
        path = self.root / "mappings/domestic_team_aliases.json"
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        for code, teams in (data.get("aliases") or {}).items():
            for canonical, aliases in teams.items():
                canonical_token = canonical_name(canonical)
                for name in [canonical] + list(aliases or []):
                    self.aliases[str(code)][canonical_name(name)].add(canonical_token)
        # Reuse the existing *verified* cross-provider dictionary, including
        # older Chinese team names not present in the public alias file.
        from scripts.canonical_team_identity import VERIFIED_CROSS_PROVIDER_ALIASES
        for code, teams in VERIFIED_CROSS_PROVIDER_ALIASES.items():
            for canonical, aliases in teams.items():
                canonical_token = canonical_name(canonical)
                for name in [canonical] + list(aliases):
                    self.aliases[code][canonical_name(name)].add(canonical_token)

    def normalize_team(self, code: str, value: object) -> str:
        key = canonical_name(value)
        owners = self.aliases.get(code, {}).get(key, set())
        return next(iter(owners)) if len(owners) == 1 else key

    def normalize_league(self, code: object) -> str:
        raw = str(code or "").strip()
        return VERIFIED_LEAGUE_CODE_ALIASES.get(raw, raw)

    def _build(self) -> None:
        index_file = self.root / "data/statmaker/domestic_enriched/index.json"
        index = json.loads(index_file.read_text(encoding="utf-8"))
        if index.get("schema_version") != 3:
            raise ValueError("Unknown domestic enriched index")
        by_league_id: dict[str, set[str]] = defaultdict(set)
        expected_paths: dict[str, tuple[str, str, str]] = {}
        for item in index.get("leagues", []):
            lid = _id(item.get("api_football_league_id"))
            code = _id(item.get("league_code"))
            league = str(item.get("league") or "")
            season = _id(item.get("api_football_season"))
            rel = Path(str(item.get("cache_path") or ""))
            if not (lid and code and league and season) or rel.is_absolute() or ".." in rel.parts:
                self.stats["INVALID_INDEX_SCOPE"] += 1
                continue
            group = f"{code}|{league}"
            by_league_id[lid].add(group)
            expected_paths[rel.as_posix()] = (lid, season, group)
        for path in sorted((self.root / ROOT).glob("**/fixture_stats.json")):
            if not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
                self.stats["MISSING_OR_OVERSIZED_CACHE"] += 1
                continue
            rel = path.relative_to(self.root).as_posix()
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                self.stats["BAD_CACHE_JSON"] += 1
                continue
            if not isinstance(doc, dict) or doc.get("provider") != "api-football":
                self.stats["WRONG_PROVIDER"] += 1
                continue
            league_id, season = _id(doc.get("league_id")), _id(doc.get("season"))
            if not league_id or not season or path.parent.name != season:
                self.stats["BAD_PROVIDER_SCOPE"] += 1
                continue
            indexed = expected_paths.get(rel)
            if indexed:
                if (league_id, season) != indexed[:2]:
                    self.stats["CACHE_INDEX_MISMATCH"] += 1
                    continue
                group = indexed[2]
            else:
                possibilities = by_league_id.get(league_id, set())
                if len(possibilities) != 1:
                    self.stats["UNMAPPED_OLDER_LEAGUE"] += 1
                    continue
                group = next(iter(possibilities))
                self.stats["ADDITIONAL_CACHED_SEASONS"] += 1
            code = group.split("|", 1)[0]
            source_summary={"cachePath": rel, "leagueId": league_id,
                "leagueCode": code, "season": season, "group": group,
                "indexed": bool(indexed), "completedRows": 0, "scoredRows": 0,
                "rowsWithTeamIds": 0, "metricFieldCoverage": {k: 0 for k in STAT_FIELDS}}
            self.source_paths.append(source_summary)
            scheduled = {}
            for row in doc.get("schedule_fixtures") or []:
                if not isinstance(row, dict) or not _id(row.get("fixture_id")):
                    continue
                source = row.get("source_league") or {}
                if str(source.get("id")) != league_id or str(source.get("season")) != season:
                    self.stats["BAD_FIXTURE_PROVIDER_SCOPE"] += 1
                    continue
                scheduled[str(row["fixture_id"])] = row
                if str(row.get("status") or "").upper() != "NS":
                    continue
                if not as_utc(row.get("date")):
                    continue
                pair = _teams(row, row)
                self.schedule[code].append({
                    "group": group, "leagueId": league_id, "season": season,
                    "fixtureId": str(row["fixture_id"]), "kickoff": as_utc(row["date"]),
                    "home": self.normalize_team(code, row.get("home_team")),
                    "away": self.normalize_team(code, row.get("away_team")),
                    "ids": pair, "cachePath": rel,
                })
            for row in doc.get("fixtures") or []:
                if not isinstance(row, dict) or str(row.get("status") or "").upper() not in FINAL:
                    continue
                source = row.get("source_league") or {}
                if str(source.get("id")) != league_id or str(source.get("season")) != season:
                    self.stats["BAD_HISTORICAL_PROVIDER_SCOPE"] += 1
                    continue
                fid = _id(row.get("fixture_id"))
                if not fid:
                    continue
                when = as_utc(row.get("date"))
                if when is None:
                    continue
                key = (group, fid)
                team_ids = _teams(row, scheduled.get(fid))
                hg, ag = _score(row.get("home_goals")), _score(row.get("away_goals"))
                if (hg is None or ag is None) and scheduled.get(fid):
                    hg = _score(scheduled[fid].get("home_goals"))
                    ag = _score(scheduled[fid].get("away_goals"))
                normalized = row.get("normalized_stats") or {}
                if not isinstance(normalized, dict):
                    normalized = {}
                source_summary["completedRows"] += 1
                if hg is not None and ag is not None:
                    source_summary["scoredRows"] += 1
                if team_ids:
                    source_summary["rowsWithTeamIds"] += 1
                for field in STAT_FIELDS:
                    if normalized.get(field) is not None:
                        source_summary["metricFieldCoverage"][field] += 1
                # Only a provider-recorded actual half-time score is used.
                # Do not derive a fictional period breakdown from the FT total.
                hthg = _score(row.get("hthg"))
                htag = _score(row.get("htag"))
                score = row.get("score") or {}
                halftime = score.get("halftime") or {}
                if hthg is None:
                    hthg = _score(halftime.get("home"))
                if htag is None:
                    htag = _score(halftime.get("away"))
                if (hthg is None or htag is None) and scheduled.get(fid):
                    previous = scheduled[fid]
                    hthg = _score(previous.get("hthg"))
                    htag = _score(previous.get("htag"))
                    if hthg is None or htag is None:
                        halftime = (previous.get("score") or {}).get("halftime") or {}
                        hthg = _score(halftime.get("home"))
                        htag = _score(halftime.get("away"))
                actual_stats = {k: normalized.get(k) for k in STAT_FIELDS}
                if (hg is not None and ag is not None and
                        hthg is not None and htag is not None and
                        0 <= hthg <= hg and 0 <= htag <= ag):
                    actual_stats.update(HTHG=hthg, HTAG=htag,
                                        H2HG=hg-hthg, H2AG=ag-htag)
                    self.stats["VERIFIED_CACHED_PERIOD_LABELS"] += 1
                historical = {
                    "group": group, "fixture_id": fid, "date": when,
                    "home": canonical_name(row.get("home_team")),
                    "away": canonical_name(row.get("away_team")),
                    "ids": team_ids, "leagueId": league_id,
                    "hg": hg, "ag": ag,
                    "stats": actual_stats,
                    "cachePath": rel,
                }
                old = self.raw_history.get(key)
                if old is not None and old != historical:
                    self.stats["DUPLICATE_RAW_ID"] += 1
                    continue
                self.raw_history[key] = historical
        self._overlay_history()
        from phase1_uefa_history import import_uefa_history
        self.uefa_history, self.uefa_evidence = import_uefa_history(
            self.root, self.groups, self.aliases)
        self.history.extend(self.uefa_history)
        self.available_teams.update(
            (m["group"], m["home"]) for m in self.uefa_history)
        self.available_teams.update(
            (m["group"], m["away"]) for m in self.uefa_history)

    def _overlay_history(self) -> None:
        """Join missing IDs only through a UNIQUE existing API team-ID claim.

        A name that has conflicting provider IDs is never repaired by guessing.
        Both teams must independently resolve before a historical row is
        rekeyed. This also avoids treating one real team as two model teams
        merely because some cached historical fixtures omit API IDs.
        """
        owner_claims: dict[tuple[str, str], set[str]] = defaultdict(set)
        for raw in self.raw_history.values():
            if not raw["ids"]:
                continue
            code = raw["group"].split("|", 1)[0]
            for side, tid in (("home", raw["ids"][0]), ("away", raw["ids"][1])):
                label = self.normalize_team(code, raw[side])
                if label:
                    owner_claims[raw["group"], label].add(team_key(raw["leagueId"], tid))

        def unique(group: str, label: object) -> str | None:
            code = group.split("|", 1)[0]
            owners = owner_claims.get((group, self.normalize_team(code, label)), set())
            return next(iter(owners)) if len(owners) == 1 else None

        existing: set[tuple[str, str]] = set()
        updated = []
        for row in self.history:
            key = (row["group"], str(row["fixture_id"]))
            existing.add(key)
            raw = self.raw_history.get(key)
            if raw and raw["ids"]:
                home_key = team_key(raw["leagueId"], raw["ids"][0])
                away_key = team_key(raw["leagueId"], raw["ids"][1])
                evidence = "EXACT_CACHED_API_FIXTURE_ID"
            else:
                home_key = unique(row["group"], row["home"])
                away_key = unique(row["group"], row["away"])
                evidence = "UNIQUE_VERIFIED_LEAGUE_TEAM_ALIAS"
            if home_key and away_key and home_key != away_key:
                row = dict(row)
                row["home"], row["away"] = home_key, away_key
                row["providerHomeId"], row["providerAwayId"] = (
                    home_key.rsplit(":", 1)[1], away_key.rsplit(":", 1)[1])
                row["phase1TeamIdentityEvidence"] = evidence
                self.stats["ENRICHED_ROWS_WITH_VERIFIED_API_IDS" if raw and raw["ids"]
                           else "ENRICHED_ROWS_REKEYED_BY_UNIQUE_VERIFIED_ALIAS"] += 1
            else:
                self.stats["ENRICHED_ROWS_WITHOUT_VERIFIED_API_IDS"] += 1
            updated.append(row)

        for key, raw in self.raw_history.items():
            if key in existing:
                continue
            if raw["hg"] is None or raw["ag"] is None:
                self.stats["OLD_CACHE_MISSING_SCORE"] += 1
                continue
            if raw["ids"]:
                home_key, away_key = (
                    team_key(raw["leagueId"], raw["ids"][0]),
                    team_key(raw["leagueId"], raw["ids"][1]))
                evidence = "EXACT_CACHED_API_FIXTURE_ID"
            else:
                home_key = unique(raw["group"], raw["home"])
                away_key = unique(raw["group"], raw["away"])
                evidence = "UNIQUE_VERIFIED_LEAGUE_TEAM_ALIAS"
            if not home_key or not away_key or home_key == away_key:
                self.stats["OLD_CACHE_MISSING_OR_AMBIGUOUS_IDENTITY"] += 1
                continue
            updated.append({
                "group": raw["group"], "fixture_id": raw["fixture_id"],
                "date": raw["date"], "home": home_key, "away": away_key,
                "hg": raw["hg"], "ag": raw["ag"], "stats": raw["stats"],
                "providerHomeId": home_key.rsplit(":", 1)[1],
                "providerAwayId": away_key.rsplit(":", 1)[1],
                "phase1TeamIdentityEvidence": evidence,
            })
            self.stats["ADDED_SCORED_OLD_CACHE_ROWS"] += 1
            if evidence == "UNIQUE_VERIFIED_LEAGUE_TEAM_ALIAS":
                self.stats["ADDED_OLD_CACHED_ROWS_REPAIRED_BY_ALIAS"] += 1
        self.history = updated
        self.available_teams = {(h["group"], h["home"]) for h in updated} | {
            (h["group"], h["away"]) for h in updated}

    def resolve(self, item: dict) -> tuple[tuple[str, str, str] | None, str, dict]:
        """Resolve a bookmaker event to exactly one future API fixture, never fuzzy."""
        code = self.normalize_league(item.get("leagueCode"))
        kickoff = as_utc(item.get("kickoff"))
        details = {"leagueCode": code, "fixtureId": str(item.get("fixtureId") or ""),
                   "home": item.get("homeTeam"), "away": item.get("awayTeam")}
        if not code or kickoff is None:
            return None, "MISSING_LEAGUE_OR_KICKOFF", details
        homes = {self.normalize_team(code, item.get(k)) for k in
                 ("providerHomeTeam", "canonicalHomeTeam", "homeTeam")}
        aways = {self.normalize_team(code, item.get(k)) for k in
                 ("providerAwayTeam", "canonicalAwayTeam", "awayTeam")}
        homes.discard("")
        aways.discard("")
        choices = {}
        for row in self.schedule.get(code, []):
            if row["home"] not in homes or row["away"] not in aways:
                continue
            if abs((row["kickoff"] - kickoff).total_seconds()) > 900:
                continue
            choices[(row["leagueId"], row["fixtureId"])] = row
        details["matchingScheduleFixtures"] = len(choices)
        if not choices:
            return None, "NO_EXACT_CACHED_SCHEDULE_MATCH", details
        if len(choices) != 1:
            return None, "AMBIGUOUS_SCHEDULE_IDENTITY", details
        match = next(iter(choices.values()))
        details.update({"apiFootballLeagueId": match["leagueId"],
                        "apiFootballFixtureId": match["fixtureId"],
                        "apiFootballSeason": match["season"],
                        "cachePath": match["cachePath"],
                        "apiHomeId": match["ids"][0] if match["ids"] else None,
                        "apiAwayId": match["ids"][1] if match["ids"] else None})
        if match["ids"] is None:
            return None, "SCHEDULE_WITHOUT_TEAM_IDS", details
        home = team_key(match["leagueId"], match["ids"][0])
        away = team_key(match["leagueId"], match["ids"][1])
        if not any((match["group"], side) in self.available_teams for side in (home, away)):
            return None, "NO_PRIOR_HISTORY_FOR_PROVIDER_TEAM_IDS", details
        return (match["group"], home, away), "EXACT_API_TEAM_IDS", details

    def coverage(self) -> dict:
        return {
            "sources": self.source_paths,
            "counters": dict(self.stats),
            "cachedScheduledFixtures": sum(map(len, self.schedule.values())),
            "cachedHistoricalFixtures": len(self.raw_history),
            "mergedHistoricalRows": len(self.history),
            "uefaHistory": self.uefa_evidence,
        }
