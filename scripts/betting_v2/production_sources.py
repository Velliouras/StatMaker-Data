"""Full canonical SQLite inventory + real point-in-time cached inputs. Zero HTTP clients."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import sqlite3
import re
from pathlib import Path

from .production_contract import (Blocked, digest, evidence, file_digest, milliseconds,
                                 number, read_json, require, safe_child, selection_id)

METRIC_FIELDS = {
    "GOALS_FT": ("home_goals", "away_goals"),
    "GOALS_1H": ("HTHG", "HTAG"),
    "GOALS_2H": ("H2HG", "H2AG"),
    "CORNERS": ("HC", "AC"), "SHOTS": ("HS", "AS"),
    "SOT": ("HST", "AST"), "YELLOW_CARDS": ("HY", "AY"),
}
TOTAL_KEYS = {
    "MATCH_GOALS": ("GOALS_FT", "FULL_TIME_MATCH_TOTAL"),
    "FIRST_HALF_GOALS": ("GOALS_1H", "FIRST_HALF_MATCH_TOTAL"),
    "SECOND_HALF_GOALS": ("GOALS_2H", "SECOND_HALF_MATCH_TOTAL"),
    "MATCH_CORNERS": ("CORNERS", "MATCH_CORNERS_TOTAL"),
    "MATCH_SHOTS": ("SHOTS", "MATCH_SHOTS_TOTAL"),
    "MATCH_SHOTS_ON_TARGET": ("SOT", "MATCH_SOT_TOTAL"),
    "MATCH_YELLOW_CARDS": ("YELLOW_CARDS", "MATCH_YELLOW_CARDS_TOTAL"),
    "MATCH_CARDS": ("CARDS", "MATCH_CARDS_TOTAL"),
}
TEAM_KEYS = {
    "TEAM_GOALS": ("GOALS_FT", "HOME_TEAM_TOTAL", "AWAY_TEAM_TOTAL"),
    "TEAM_FIRST_HALF_GOALS": ("GOALS_1H", "HOME_TEAM_1H_TOTAL", "AWAY_TEAM_1H_TOTAL"),
    "TEAM_SECOND_HALF_GOALS": ("GOALS_2H", "HOME_TEAM_2H_TOTAL", "AWAY_TEAM_2H_TOTAL"),
    "TEAM_CORNERS": ("CORNERS", "HOME_TEAM_CORNERS", "AWAY_TEAM_CORNERS"),
    "TEAM_SHOTS": ("SHOTS", "HOME_TEAM_SHOTS", "AWAY_TEAM_SHOTS"),
    "TEAM_SHOTS_ON_TARGET": ("SOT", "HOME_TEAM_SOT", "AWAY_TEAM_SOT"),
    "TEAM_YELLOW_CARDS": ("YELLOW_CARDS", "HOME_TEAM_YELLOW_CARDS", "AWAY_TEAM_YELLOW_CARDS"),
    "TEAM_CARDS": ("CARDS", "HOME_TEAM_CARDS", "AWAY_TEAM_CARDS"),
}
RESULT_KEYS = {"1X2": "RESULT_1X2", "DOUBLE_CHANCE": "RESULT_DOUBLE_CHANCE",
               "DRAW_NO_BET": "RESULT_DNB", "BTTS": "BTTS",
               "HALF_TIME_1X2": "HT_RESULT_1X2",
               "HALF_TIME_DOUBLE_CHANCE": "HT_RESULT_DOUBLE_CHANCE"}
SIDES = {"1": "HOME", "X": "DRAW", "2": "AWAY", "1X": "HOME_OR_DRAW",
         "X2": "AWAY_OR_DRAW", "12": "HOME_OR_AWAY"}
MARKET_ALIASES = {
    "MATCH_RESULT": "1X2", "RESULT": "1X2", "FULL_TIME_RESULT": "1X2", "FT_RESULT": "1X2",
    "MATCH_WINNER": "1X2", "WINNER": "1X2", "FULL_TIME_RESULT_3WAY": "1X2", "HOME_DRAW_AWAY": "1X2",
    "THREE_WAY": "1X2", "THREE_WAY_RESULT": "1X2", "3WAY": "1X2", "MONEYLINE": "1X2",
    "DOUBLE_CHANCE_1X2": "DOUBLE_CHANCE", "DC": "DOUBLE_CHANCE", "DOUBLE_CHANCE_RESULT": "DOUBLE_CHANCE",
    "DOUBLE_CHANCE_12": "DOUBLE_CHANCE", "DOUBLECHANCE": "DOUBLE_CHANCE",
    "TEAM_TOTAL_GOALS": "TEAM_GOALS", "HALF_TIME_GOALS": "FIRST_HALF_GOALS",
    "MATCH_FIRST_HALF_GOALS": "FIRST_HALF_GOALS", "MATCH_HALF_TIME_GOALS": "FIRST_HALF_GOALS",
    "HT_1X2": "HALF_TIME_1X2", "FIRST_HALF_1X2": "HALF_TIME_1X2", "HALF_TIME_RESULT": "HALF_TIME_1X2",
    "FIRST_HALF_RESULT": "HALF_TIME_1X2", "HT_DOUBLE_CHANCE": "HALF_TIME_DOUBLE_CHANCE",
    "FIRST_HALF_DOUBLE_CHANCE": "HALF_TIME_DOUBLE_CHANCE", "TEAM_1H_GOALS": "TEAM_FIRST_HALF_GOALS",
    "TEAM_HALF_TIME_GOALS": "TEAM_FIRST_HALF_GOALS", "TEAM_FIRST_HALF_TOTAL": "TEAM_FIRST_HALF_GOALS",
    "MATCH_SECOND_HALF_GOALS": "SECOND_HALF_GOALS", "SECOND_HALF_TOTAL": "SECOND_HALF_GOALS",
    "TEAM_2H_GOALS": "TEAM_SECOND_HALF_GOALS", "TEAM_SECOND_HALF_TOTAL": "TEAM_SECOND_HALF_GOALS",
    "DNB": "DRAW_NO_BET", "DRAW_NO_BET_RESULT": "DRAW_NO_BET",
    "CORNERS": "MATCH_CORNERS", "CARDS": "MATCH_CARDS", "YELLOW_CARDS": "MATCH_YELLOW_CARDS",
    "TOTAL_YELLOW_CARDS": "MATCH_YELLOW_CARDS", "TEAM_CARDS_YELLOW": "TEAM_YELLOW_CARDS",
    "SHOTS": "TEAM_SHOTS", "SHOTS_ON_TARGET": "TEAM_SHOTS_ON_TARGET",
}


def market_identity(row: dict) -> dict:
    token = re.sub(r"[^A-Z0-9]+", "_", row["market"].strip().upper()).strip("_")
    market = MARKET_ALIASES.get(token, token)
    require("ASIAN" not in market and "HANDICAP" not in market, "RETIRED_MARKET")
    side = SIDES.get(row["selection"].strip().upper(), row["selection"].strip().upper())
    model = dict(row, side=side, teamSide=None)
    if market in RESULT_KEYS:
        model.update(family="GOALS_1H" if market.startswith("HALF_TIME") else "GOALS_FT",
                     marketModelKey=RESULT_KEYS[market])
    elif market in TOTAL_KEYS:
        family, key = TOTAL_KEYS[market]
        model.update(family=family, marketModelKey=key)
    elif market in TEAM_KEYS:
        family, home, away = TEAM_KEYS[market]
        names = [row.get("homeTeam"), row.get("canonicalHomeTeam"), row.get("providerHomeTeam")]
        other = [row.get("awayTeam"), row.get("canonicalAwayTeam"), row.get("providerAwayTeam")]
        target = str(row.get("team") or "").strip().casefold()
        home_match = bool(target) and any(target == str(v or "").strip().casefold() for v in names)
        away_match = bool(target) and any(target == str(v or "").strip().casefold() for v in other)
        require(home_match != away_match, "AMBIGUOUS_TEAM_TOTAL_IDENTITY")
        model.update(family=family, marketModelKey=home if home_match else away,
                     teamSide="HOME" if home_match else "AWAY")
    else:
        raise Blocked("NO_MARKET_MODEL_OR_SETTLEMENT_CONTRACT")
    if row.get("line") is not None:
        line = number(row["line"])
        require(line >= 0 and line * 2 == int(line * 2), "UNSUPPORTED_QUARTER_LINE")
    if market in TOTAL_KEYS or market in TEAM_KEYS:
        require(row.get("line") is not None and side in ("OVER", "UNDER"), "MISSING_COUNT_LINE_OR_SIDE")
    return model


def full_inventory(db_path: Path, generation: str) -> tuple[list[dict], Counter]:
    """No legacy prepared_selections join, price gate, date scope or result cap."""
    result, reasons = [], Counter()
    with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        versions = {r[0]: r[1] for r in db.execute(
            "SELECT competition_id,snapshot_version FROM prepared_snapshot_meta WHERE state='ready'")}
        rows = db.execute("""
            SELECT f.*, m.market,m.selection,m.team,m.line,m.odd,m.ordinal
            FROM prepared_fixture_matches f LEFT JOIN prepared_fixture_markets m
            ON f.competition_id=m.competition_id AND f.snapshot_version=m.snapshot_version
              AND f.match_key=m.match_key
            ORDER BY f.competition_id,f.match_key,m.ordinal
        """)
        for r in rows:
            item = dict(r)
            row = {
                "competitionId": r["competition_id"], "snapshotVersion": r["snapshot_version"],
                "generationId": generation, "fixtureId": r["id"], "fixtureKey": r["match_key"],
                "leagueCode": r["league_code"], "season": r["season"], "kickoff": r["kickoff"],
                "homeTeam": r["home_team"], "awayTeam": r["away_team"],
                "canonicalHomeTeam": r["canonical_home_team"], "canonicalAwayTeam": r["canonical_away_team"],
                "providerHomeTeam": r["provider_home_team"], "providerAwayTeam": r["provider_away_team"],
                "market": r["market"] or "", "selection": r["selection"] or "", "team": r["team"],
                "line": r["line"], "odd": r["odd"], "usableForStats": bool(r["usable_for_stats"]),
                "sourceRecordSha256": digest(item),
            }
            try:
                require(bool(row["market"]), "NO_QUOTED_MARKETS")
                require(versions.get(row["competitionId"]) == row["snapshotVersion"], "STALE_RAW_SNAPSHOT")
                require(bool(row["fixtureId"]) and row["fixtureId"] == row["fixtureKey"], "MISSING_IMMUTABLE_FIXTURE_ID")
                row["kickoffEpochMs"] = milliseconds(row["kickoff"])
                row["selectionId"] = selection_id(row)
                row.update(market_identity(row))
            except Blocked as error:
                row["blocker"] = str(error)
                reasons[str(error)] += 1
            result.append(row)
    seen = defaultdict(list)
    for row in result:
        if "selectionId" in row:
            seen[(row["competitionId"], row["snapshotVersion"], row["selectionId"])].append(row)
    for group in seen.values():
        if len(group) > 1:
            for row in group:
                row["blocker"] = "AMBIGUOUS_RAW_SELECTION"
                reasons[row["blocker"]] += 1
    return result, reasons


def capture_new_statistics(item: dict, raw_response: list) -> dict:
    """Call ONLY upon actual successful receipt, never while reading old caches."""
    observed = int(datetime.now(timezone.utc).timestamp() * 1000)
    identity = {k: item.get(k) for k in ("fixture_id", "date", "home_team_id", "away_team_id",
                "home_team", "away_team", "home_goals", "away_goals", "status", "source_league")}
    return {"contract": "statmaker-betting-v2-statistics-receipt-v1",
            "observedAtMs": observed, "provider": "api-football",
            "identitySha256": digest(identity), "rawStatisticsSha256": digest(raw_response),
            "normalizedSha256": digest(item["normalized_stats"]), "identity": identity}


def cached_history(root: Path) -> tuple[list[dict], list[dict], Counter]:
    """Use the actual API-Football caches named by canonical schema-3 enriched index."""
    index = read_json(root / "data/statmaker/domestic_enriched/index.json")
    require(index.get("schema_version") == 3, "UNKNOWN_DOMESTIC_SOURCE_SCHEMA")
    history, schedule, reasons = [], [], Counter()
    seen = set()
    for league in index["leagues"]:
        try:
            path = safe_child(root, league["cache_path"])
            cache = read_json(path)
            require(cache["provider"] == "api-football", "WRONG_PROVIDER_CACHE")
            require(str(cache["league_id"]) == str(league["api_football_league_id"])
                    and str(cache["season"]) == str(league["api_football_season"]), "CACHE_SCOPE_MISMATCH")
            sha = file_digest(path)
        except (Blocked, OSError, KeyError) as error:
            reasons[str(error)] += 1
            continue
        for raw in cache.get("schedule_fixtures", []) + cache.get("fixtures", []):
            try:
                require(raw.get("fixture_id") and raw.get("home_team_id") and raw.get("away_team_id"),
                        "MISSING_CANONICAL_API_TEAM_OR_FIXTURE_ID")
                item = dict(raw, leagueCode=league["league_code"], season=str(league["app_season"]),
                            apiSeason=str(cache["season"]), sourceSha256=sha,
                            kickoffEpochMs=milliseconds(raw["date"]))
                if raw.get("status") == "NS":
                    schedule.append(item)
                    continue
                require(raw.get("status") == "FT", "NO_REGULATION_TIME_FINAL_SCORE")
                key = (str(cache["league_id"]), str(raw["fixture_id"]))
                if key in seen:
                    continue
                seen.add(key)
                receipt = raw.get("betting_v2_provenance")
                require(isinstance(receipt, dict), "MISSING_POINT_IN_TIME_STATISTICS_RECEIPT")
                require(receipt["contract"] == "statmaker-betting-v2-statistics-receipt-v1"
                        and receipt["provider"] == "api-football", "UNKNOWN_STATISTICS_RECEIPT")
                require(digest(raw["normalized_stats"]) == receipt["normalizedSha256"]
                        and digest(raw["raw_statistics"]) == receipt["rawStatisticsSha256"]
                        and digest(receipt["identity"]) == receipt["identitySha256"], "STATISTICS_LINEAGE_MISMATCH")
                for field, value in receipt["identity"].items():
                    require(raw.get(field) == value, "STATISTICS_IDENTITY_CHANGED")
                item["availableAtMs"] = milliseconds(receipt["observedAtMs"])
                require(item["availableAtMs"] > item["kickoffEpochMs"], "POSTMATCH_STATS_WRONG_TIME")
                item["goals"] = [number(raw["home_goals"]), number(raw["away_goals"])]
                item["counts"] = {"GOALS_FT": item["goals"]}
                stats = raw["normalized_stats"]
                for family, fields in METRIC_FIELDS.items():
                    if family != "GOALS_FT" and all(stats.get(k) is not None for k in fields):
                        item["counts"][family] = [number(stats[k]) for k in fields]
                # xG absence is traced to the raw provider field, not a failed join.
                labels = {s.get("type") for team in raw["raw_statistics"] for s in team.get("statistics", [])}
                xg_sources = {str(team.get("team", {}).get("id")): next(
                    (s.get("value") for s in team.get("statistics", []) if s.get("type") == "expected_goals"),
                    None) for team in raw["raw_statistics"]}
                require(all(str(raw[k]) in xg_sources for k in ("home_team_id", "away_team_id")),
                        "INCOMPLETE_OR_WRONG_TEAM_STATISTICS_RESPONSE")
                item["providerXgUnavailable"] = all(xg_sources.get(str(raw[k])) is None
                                                   for k in ("home_team_id", "away_team_id"))
                if stats.get("HxG") is not None and stats.get("AxG") is not None:
                    require("expected_goals" in labels, "XG_WITHOUT_PROVIDER_FIELD")
                    item["xg"] = [number(stats["HxG"]), number(stats["AxG"])]
                    require(item["xg"] == [number(xg_sources.get(str(raw[k])))
                                           for k in ("home_team_id", "away_team_id")], "RAW_XG_IDENTITY_MISMATCH")
                history.append(item)
            except (Blocked, KeyError, TypeError) as error:
                reasons[str(error)] += 1
    return history, schedule, reasons


def resolve_fixture(row: dict, schedule: list[dict]) -> dict:
    matches = {}
    for item in schedule:
        if (item["leagueCode"] == row["leagueCode"] and item["season"] == row["season"]
                and item["kickoffEpochMs"] == row["kickoffEpochMs"]
                and str(item["home_team"]).strip().casefold() == str(row.get("canonicalHomeTeam") or row["homeTeam"]).strip().casefold()
                and str(item["away_team"]).strip().casefold() == str(row.get("canonicalAwayTeam") or row["awayTeam"]).strip().casefold()):
            matches[str(item["fixture_id"])] = item
    require(len(matches) == 1, "MISSING_OR_AMBIGUOUS_EXACT_FIXTURE_CROSSWALK")
    return next(iter(matches.values()))


def card_history(history: list[dict], contract: dict) -> list[dict]:
    """Derive only the exact documented bookmaker card contract, never a default weighting."""
    require(contract.get("contract") == "statmaker-verified-bookmaker-card-count-v1"
            and digest(contract["providerRule"]) == contract["providerRuleSha256"],
            "MISSING_VERIFIED_CARD_COUNT_SETTLEMENT_CONTRACT")
    yellow, red = number(contract["yellowWeight"]), number(contract["redWeight"])
    require(yellow == number(contract["providerRule"]["yellowWeight"])
            and red == number(contract["providerRule"]["redWeight"])
            and yellow >= 0 and red >= 0, "UNVERIFIED_CARD_WEIGHTING")
    result = []
    for row in history:
        counts = dict(row["counts"])
        stats = row["normalized_stats"]
        if all(stats.get(k) is not None for k in ("HY", "AY", "HR", "AR")):
            counts["CARDS"] = [number(stats["HY"])*yellow+number(stats["HR"])*red,
                               number(stats["AY"])*yellow+number(stats["AR"])*red]
        result.append(dict(row, counts=counts))
    return result


def quote_for(row: dict, attestations: list[dict], observed_now: int) -> dict:
    """Attestations require ORIGINAL provider fields/receipts, not inferred bundle time."""
    found = [q for q in attestations if q.get("selectionId") == row["selectionId"]
             and q.get("snapshotVersion") == row["snapshotVersion"]
             and q.get("generationId") == row["generationId"]]
    require(len(found) == 1, "MISSING_OR_AMBIGUOUS_ATTRIBUTABLE_QUOTE")
    q = found[0]
    require(q["competitionId"] == row["competitionId"] and q["fixtureId"] == row["fixtureId"],
            "QUOTE_IDENTITY_MISMATCH")
    require(q.get("independentUnfilteredBookmakerUniverseVerified") is True
            and q.get("priceObservationTimestampVerified") is True, "UNVERIFIED_QUOTE_PROVENANCE")
    original = q["providerRecord"]
    require(digest(original) == q["providerRecordSha256"], "QUOTE_SOURCE_HASH_MISMATCH")
    require(q["bookmaker"] == original["bookmaker"] and q["providerQuoteId"] == original["id"]
            and q["providerEventId"] == original["eventId"] == row["fixtureId"],
            "QUOTE_PROVIDER_IDENTITY_MISMATCH")
    require(q["observedAtMs"] == milliseconds(original["observedAt"])
            and q["validUntilMs"] == milliseconds(original["validUntil"]), "INVENTED_QUOTE_TIMES")
    require(q["odd"] == number(original["odd"]) == number(row["odd"]), "CHANGED_EXACT_PRICE")
    require(0 < q["observedAtMs"] <= q["clientReceivedAtMs"] <= observed_now
            < q["validUntilMs"] <= row["kickoffEpochMs"], "STALE_OR_POSTKICKOFF_QUOTE")
    require(q.get("providerMarketIdentity") == {k: row.get(k) for k in ("market", "selection", "team", "line")},
            "QUOTE_MARKET_BINDING_MISMATCH")
    universe = q["unfilteredUniverse"]
    require(digest(universe) == q["unfilteredUniverseSha256"]
            and universe["complete"] is True and universe["providerEventId"] == row["fixtureId"],
            "MISSING_COMPLETE_ORIGINAL_PROVIDER_PRICE_UNIVERSE")
    records = universe["providerQuoteRecords"]
    require(sum(record == original for record in records) == 1, "QUOTE_NOT_UNIQUE_IN_ORIGINAL_UNIVERSE")
    require(all(record.get("bookmaker") == q["bookmaker"] for record in records),
            "MIXED_BOOKMAKER_COMPARISON")
    reference = q["comparisonProviderQuoteIds"]
    comparable = [record for record in records if record["id"] in reference]
    require(len(comparable) == len(set(reference)) >= 2
            and len({record["id"] for record in comparable}) == len(comparable)
            and original in comparable, "MISSING_INDEPENDENT_COMPARISON_PRICES")
    require(all(milliseconds(record["observedAt"]) <= observed_now
                < milliseconds(record["validUntil"]) <= row["kickoffEpochMs"] for record in comparable),
            "STALE_COMPARISON_PRICES")
    # The caller must supply an exhaustive, mutually exclusive decisive-side price partition.
    require(q["comparisonPartitionVerified"] is True
            and universe["comparisonPartitions"].get(row["selectionId"]) == reference,
            "UNVERIFIED_NO_VIG_PARTITION")
    implied = [1/number(record["odd"]) for record in comparable]
    no_vig = (1/number(original["odd"]))/sum(implied)
    return dict(q, verifiedNoVigDecisiveWin=no_vig)
