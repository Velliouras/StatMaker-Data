#!/usr/bin/env python3
"""Materialize the shared broad/default-Singles ledger alongside the precision PROD ledger.

This script is intentionally additive:
- data/statmaker/canonical_recommendation_ledger.json remains the one shared main feed;
- existing `entries` keep the current PROD precision contract unchanged;
- `broadEntries` preserve the Saturday-style default Singles contract:
  recommendation_eligible + Strong Value + odd >= 1.50 + one MAIN pick per match,
  with permanently retired Asian/Handicap markets excluded.

Older PROD APKs ignore the additional fields. The UAT rollback explicitly opts into broadEntries.
No provider/API call is made here.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import materialize_canonical_recommendation_ledger as base

BROAD_SOURCE = "canonical-shared-main-broad-default-singles-no-retired-v1"
ROLLBACK_HISTORY_END = dt.date(2026, 9, 13)


def broad_final_candidates(db: sqlite3.Connection, generation_id: str):
    retired_sql = ",".join("?" for _ in base.RETIRED_SUB_MARKET_KEYS)
    retired_args = tuple(sorted(base.RETIRED_SUB_MARKET_KEYS))
    source = base.rows(
        db,
        "SELECT c.* FROM prepared_pattern_candidates c "
        "JOIN prepared_selections s "
        "ON s.competition_id=c.competition_id "
        "AND s.snapshot_version=c.snapshot_version "
        "AND s.selection_key=c.selection_key "
        "WHERE c.generation_id=? "
        "AND c.recommendation_eligible=1 "
        "AND UPPER(TRIM(COALESCE(c.value_tier,'')))='STRONG_VALUE' "
        "AND c.selection_odd>=1.50 "
        f"AND COALESCE(s.identity_sub_market_key,'') NOT IN ({retired_sql}) "
        "ORDER BY c.evidence_score DESC,c.source_order ASC",
        (generation_id, *retired_args),
    )

    exact = {}
    for row in source:
        key = (
            str(row.get("competition_id") or ""),
            str(row.get("match_key") or ""),
            str(row.get("exact_recommendation_key") or ""),
        )
        if key not in exact or base.num(row.get("selection_score")) > base.num(exact[key].get("selection_score")):
            exact[key] = row

    best = {}
    for row in exact.values():
        key = (str(row.get("competition_id") or ""), str(row.get("match_key") or ""))
        rank = (
            base.num(row.get("selection_score")),
            base.num(row.get("strict_hit_rate")),
            base.intval(row.get("strict_sample")),
            base.num(row.get("selection_odd")),
        )
        old = best.get(key)
        old_rank = None if old is None else (
            base.num(old.get("selection_score")),
            base.num(old.get("strict_hit_rate")),
            base.intval(old.get("strict_sample")),
            base.num(old.get("selection_odd")),
        )
        if old is None or rank > old_rank:
            best[key] = row
    return list(best.values())


def extract_broad(bundle: Path, target: str | None = None):
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "db.sqlite"
        try:
            with zipfile.ZipFile(bundle) as archive:
                with archive.open("databases/statmaker_prepared_betting.db") as source, db_path.open("wb") as target_file:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        target_file.write(chunk)
        except Exception:
            return []

        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rejected_identity = 0
        try:
            generation = base.first(
                db,
                "SELECT * FROM prepared_pattern_generation WHERE state='ready' ORDER BY built_at_ms DESC LIMIT 1",
            )
            if not generation:
                return []
            generation_id = str(generation.get("generation_id") or "")
            built_at_ms = base.intval(generation.get("built_at_ms"))
            output = []

            for candidate in broad_final_candidates(db, generation_id):
                day = str(candidate.get("local_date") or "")[:10]
                if target and day != target:
                    continue
                competition_id = str(candidate.get("competition_id") or "")
                snapshot_version = str(candidate.get("snapshot_version") or "")
                selection_key = str(candidate.get("selection_key") or "")

                selection = base.first(
                    db,
                    "SELECT * FROM prepared_selections "
                    "WHERE competition_id=? AND snapshot_version=? AND selection_key=? LIMIT 1",
                    (competition_id, snapshot_version, selection_key),
                )
                if not selection:
                    continue
                prepared_match_key = str(selection.get("match_key") or "")
                match_row = base.first(
                    db,
                    "SELECT payload FROM prepared_matches "
                    "WHERE competition_id=? AND snapshot_version=? AND match_key=? LIMIT 1",
                    (competition_id, snapshot_version, prepared_match_key),
                )
                if not match_row:
                    continue
                try:
                    match = json.loads(str(match_row.get("payload") or "{}"))
                except Exception:
                    continue

                candidate_match_key = str(candidate.get("match_key") or "").strip()
                payload_match_key = base.runtime_match_key(match)
                if not candidate_match_key or candidate_match_key != payload_match_key:
                    rejected_identity += 1
                    continue

                day = day or str(match.get("date") or "")[:10]
                if target and day != target:
                    continue
                kickoff = base.kickoff_ms(match)
                if kickoff is not None and built_at_ms >= kickoff - base.SAFETY_MS:
                    continue
                if kickoff is None:
                    generated_day = (
                        dt.datetime.fromtimestamp(
                            built_at_ms / 1000,
                            tz=dt.timezone.utc,
                        ).astimezone(base.ATHENS).date().isoformat()
                        if built_at_ms
                        else ""
                    )
                    if not day or day <= generated_day:
                        continue

                sub_market = str(selection.get("identity_sub_market_key") or "")
                if sub_market in base.RETIRED_SUB_MARKET_KEYS:
                    continue
                home_names = list(base.live._names_from_match_payload(match, "home"))
                away_names = list(base.live._names_from_match_payload(match, "away"))
                if not home_names or not away_names:
                    continue
                identity_probe = {
                    "homeNames": home_names,
                    "awayNames": away_names,
                    "homeTeam": str(match.get("homeTeam") or ""),
                    "awayTeam": str(match.get("awayTeam") or ""),
                }
                if not base.valid_fixture_identity(identity_probe):
                    continue

                opponent_probability = base.nullable(selection.get("opponent_model_probability"))
                posterior_probability = base.nullable(selection.get("bm_posterior_probability"))
                model_probability = (
                    opponent_probability
                    if opponent_probability is not None
                    else posterior_probability
                )
                prediction_source = (
                    "OPPONENT_ADJUSTED"
                    if opponent_probability is not None
                    else "BOOKMAKER_POSTERIOR"
                )

                output.append({
                    "generationId": generation_id,
                    "generationBuiltAtMs": built_at_ms,
                    "competitionId": competition_id,
                    "snapshotVersion": snapshot_version,
                    "selectionKey": selection_key,
                    "matchKey": candidate_match_key,
                    "localDate": day,
                    "leagueCode": str(candidate.get("league_code") or match.get("leagueCode") or "").upper(),
                    "competition": str(match.get("competition") or ""),
                    "season": str(match.get("season") or ""),
                    "homeTeam": str(match.get("homeTeam") or ""),
                    "awayTeam": str(match.get("awayTeam") or ""),
                    "apiFixtureId": base.live._fixture_id_from_match_payload(match),
                    "kickoffEpochMillis": kickoff,
                    "homeNames": home_names,
                    "awayNames": away_names,
                    "market": str(selection.get("selection_market") or ""),
                    "selection": str(selection.get("selection_name") or ""),
                    "team": selection.get("selection_team"),
                    "line": base.nullable(selection.get("selection_line")),
                    "odd": base.nullable(selection.get("selection_odd")),
                    "broadGroup": selection.get("identity_broad_group"),
                    "family": selection.get("identity_family"),
                    "subMarketKey": sub_market,
                    "teamSide": selection.get("identity_team_side"),
                    "selectionSide": selection.get("identity_selection_side"),
                    "selectionToken": selection.get("identity_selection_token"),
                    "marketProbability": base.nullable(selection.get("bm_market_probability")),
                    "modelProbability": model_probability,
                    "reliability": base.nullable(selection.get("bm_sample_reliability")),
                    "valueTier": base.tier(candidate.get("value_tier")),
                    "opponentAdjustedRequired": bool(base.intval(selection.get("opponent_adjusted_required"))),
                    "baseModelProbability": base.nullable(selection.get("opponent_base_model_probability")),
                    "withoutFavoriteProbability": base.nullable(selection.get("opponent_without_favorite_probability")),
                    "withoutXgProbability": base.nullable(selection.get("opponent_without_xg_probability")),
                    "withoutFatigueProbability": base.nullable(selection.get("opponent_without_fatigue_probability")),
                    "withoutInjuriesProbability": base.nullable(selection.get("opponent_without_injuries_probability")),
                    "withoutLineupProbability": base.nullable(selection.get("opponent_without_lineup_probability")),
                    "withoutFormationProbability": base.nullable(selection.get("opponent_without_formation_probability")),
                    "withoutSquadTurnoverProbability": base.nullable(selection.get("opponent_without_squad_turnover_probability")),
                    "modifierProfile": selection.get("opponent_modifier_profile"),
                    "predictionSource": prediction_source,
                    "requiredKind": base.live.SUBMARKET_REQUIREMENT.get(sub_market, "unsupported"),
                })

            if rejected_identity:
                print(
                    "BROAD_CANONICAL_LEDGER_IDENTITY_REJECTED",
                    f"bundle={bundle.name}",
                    f"rows={rejected_identity}",
                )
            return output
        except sqlite3.Error:
            return []
        finally:
            db.close()


def legacy_broad_rows(high_day: dt.date):
    root, commit = base.latest_legacy_ledger()
    if not root:
        return [], "", []
    output = []
    high = high_day.isoformat()
    for raw in root.get("entries", []) or []:
        if not isinstance(raw, dict) or not base.valid_fixture_identity(raw):
            continue
        day = str(raw.get("localDate") or "")[:10]
        if not day or day > high or base.row_is_retired(raw):
            continue
        if str(raw.get("valueTier") or "").strip().upper().replace("_", " ") != "STRONG VALUE":
            continue
        if base.num(raw.get("odd"), 0.0) < 1.50:
            continue
        output.append(dict(raw))
    print(
        "BROAD_CANONICAL_LEDGER_LEGACY_MIGRATION",
        f"commit={commit[:12]}",
        f"sourceRows={len(root.get('entries', []) or [])}",
        f"broadRows={len(output)}",
        f"high={high}",
    )
    authoritative_dates = [
        str(value)[:10]
        for value in root.get("backfilledDates", []) or []
        if str(value).strip() and str(value)[:10] <= high
    ]
    return output, commit, authoritative_dates


def main() -> int:
    today = dt.datetime.now(dt.timezone.utc).astimezone(base.ATHENS).date()
    low = today - dt.timedelta(days=base.RETENTION)
    high = today + dt.timedelta(days=14)
    root = base.load(base.LEDGER, {})
    if not isinstance(root, dict) or base.intval(root.get("schemaVersion")) < 4:
        raise SystemExit("BROAD_CANONICAL_LEDGER_INVALID_BASE")

    invalidated = base.invalidated_match_keys(low, high)
    existing = [
        dict(row)
        for row in root.get("broadEntries", []) or []
        if isinstance(row, dict)
        and low.isoformat() <= str(row.get("localDate") or "")[:10] <= high.isoformat()
        and base.valid_fixture_identity(row)
        and not base.row_is_retired(row)
    ]

    current = []
    bundles = base.current_bundles()
    for bundle in bundles:
        current.extend(extract_broad(bundle))

    legacy, legacy_commit, legacy_authoritative_dates = legacy_broad_rows(high)

    # Dates through the rollback boundary are immutable historical truth. They were restored
    # from the approved Saturday baseline into broadEntries on main and must never be repopulated
    # from later precision-era ledgers/materializations. New live recommendations start strictly
    # after the boundary.
    rollback_end = ROLLBACK_HISTORY_END.isoformat()
    legacy = [
        row for row in legacy
        if str(row.get("localDate") or "")[:10] > rollback_end
    ]
    current = [
        row for row in current
        if str(row.get("localDate") or "")[:10] > rollback_end
    ]
    merged = [
        row for row in [*existing, *legacy, *current]
        if (
            str(row.get("localDate") or "")[:10] <= rollback_end
            or str(row.get("matchKey") or "").strip() not in invalidated
        )
    ]
    entries = [
        row
        for row in base.merge(merged)
        if low.isoformat() <= str(row.get("localDate") or "")[:10] <= high.isoformat()
    ]
    entries = sorted(
        entries,
        key=lambda row: (str(row.get("localDate") or ""), str(row.get("matchKey") or "")),
    )
    authoritative_dates = sorted({
        *[
            day
            for day in legacy_authoritative_dates
            if low.isoformat() <= day <= today.isoformat()
        ],
        *[
            str(row.get("localDate") or "")[:10]
            for row in entries
            if str(row.get("localDate") or "")[:10] <= today.isoformat()
        ],
    })

    semantic = dict(root)
    semantic.pop("generatedAt", None)
    semantic["broadSource"] = BROAD_SOURCE
    semantic["broadLegacyMigrationCommit"] = legacy_commit
    semantic["broadBackfilledDates"] = authoritative_dates
    semantic["broadEntries"] = entries

    before = dict(root)
    before.pop("generatedAt", None)
    changed = semantic != before
    if changed:
        payload = {
            "generatedAt": dt.datetime.now(dt.timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            **semantic,
        }
        temp = base.LEDGER.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(base.LEDGER)

    counts = {}
    for row in entries:
        day = str(row.get("localDate") or "")[:10]
        counts[day] = counts.get(day, 0) + 1
    print(
        "broad-canonical-ledger",
        f"currentBundles={len(bundles)}",
        f"currentRows={len(base.merge(current))}",
        f"legacyRows={len(legacy)}",
        f"ledgerRows={len(entries)}",
        f"changed={changed}",
        f"dateCounts={json.dumps(counts, sort_keys=True)}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
